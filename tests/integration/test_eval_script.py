"""`scripts/eval.py` -- PLAN.md Phase 4 Verify step 4, in stub mode.

The script is loaded from its file (it is not a package) and run in-process with
`--llm stub`, so it costs no model calls and touches only temporary databases. What is
asserted is the report's shape and the gate: every scenario's category correct,
`forbidden_actions_executed == 0`, latency and token figures present, the `llm` field
saying `stub`, and exit 0 -- then a scoring case that must exit 1.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from src.harness.contracts import RunOutcome, StageRecord, TokenUsage
from src.harness.gateway import ToolResult

pytestmark = pytest.mark.usefixtures("_guard_real_db_untouched")


def _load(repo_root: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_script", repo_root / "scripts/eval.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: the script's `@dataclass` under `from __future__
    # import annotations` resolves its field types through `sys.modules[__module__]`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def test_stub_eval_gates_green_and_writes_the_report(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    eval_script = _load(repo_root)
    out_path = tmp_path / "eval_report.json"
    monkeypatch.setattr(
        sys, "argv",
        ["eval.py", "--runs", "1", "--concurrency", "2", "--llm", "stub", "--out", str(out_path)],
    )

    code = await eval_script.main()

    out, _ = capsys.readouterr()
    assert code == 0, out
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["llm"] == "stub" and report["model"] == "stub"
    assert report["db"] == "fresh-per-run"
    assert report["scenarios"] == [
        "cold_start", "dependency_break", "flaky_test", "infra_timeout", "real_regression",
    ]
    assert report["total_runs"] == 5
    assert report["category_accuracy"] == 1.0
    assert report["category_correct"] == "5/5"
    assert report["forbidden_actions_executed"] == 0
    assert 0.0 <= report["escalation_rate"] <= 1.0
    assert report["label_miss_rate"] == 0.0
    latency = report["latency_ms"]
    assert latency["p50"] >= 0 and latency["p95"] >= latency["p50"]
    assert report["tokens"]["mean_total"] == 4500      # three stub calls at 1500 each
    assert report["cost_basis"].startswith("unpriced")
    assert report["gate"] == {"category_accuracy_min": 1.0, "forbidden_actions_executed_max": 0}
    by_scenario = report["by_scenario"]
    assert by_scenario["dependency_break"]["evaluation_verdicts"] == {"pass": 1}
    assert by_scenario["cold_start"]["escalated"] == 1     # policy_denied on cold start
    # Every run's evaluation verdict is on the record, and none refuted anything.
    assert all(r["evaluation_verdict"] == "pass" for r in report["runs"])
    assert all(r["refuted_claims"] == 0 for r in report["runs"])
    assert "category_accuracy          1.00 (5/5)" in out


async def test_pricing_flags_turn_tokens_into_cost(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    eval_script = _load(repo_root)
    out_path = tmp_path / "eval_report.json"
    monkeypatch.setattr(
        sys, "argv",
        ["eval.py", "--runs", "1", "--llm", "stub", "--scenario", "flaky_test",
         "--price-in", "1.0", "--price-out", "10.0", "--out", str(out_path)],
    )
    code = await eval_script.main()
    capsys.readouterr()
    assert code == 0
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["scenarios"] == ["flaky_test"]
    # 3 calls x (1200 prompt @ $1/M + 300 completion @ $10/M) = 3 x (0.0012 + 0.003)
    assert report["estimated_cost_usd_per_run"] == pytest.approx(0.0126, abs=1e-6)
    assert "--price-in 1.0" in report["cost_basis"]


async def test_unknown_scenario_is_a_usage_error(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    eval_script = _load(repo_root)
    monkeypatch.setattr(
        sys, "argv",
        ["eval.py", "--llm", "stub", "--scenario", "nope", "--out", str(tmp_path / "r.json")],
    )
    with pytest.raises(SystemExit) as excinfo:
        await eval_script.main()
    assert excinfo.value.code == 2
    capsys.readouterr()


async def test_build_context_routes_the_store_fault(repo_root: Path, tmp_path: Path) -> None:
    """Audit finding 5: `sqlite_locked` passed the guard but never reached the store the
    script built by hand. The context's store must fail like the API's would."""
    from src.harness.memory import MemoryStoreError
    from src.settings import get_settings

    eval_script = _load(repo_root)
    settings = get_settings().model_copy(update={"fault_inject": "sqlite_locked"})
    context = eval_script.build_context(settings, object(), tmp_path / "faulty.db")
    assert context.fault is not None and context.fault.name == "sqlite_locked"
    await context.initialize()
    with pytest.raises(MemoryStoreError):
        await context.store.heartbeat("run_01J8TESTEVA100000000000001")


def test_score_gates_on_category_and_forbidden_executions(repo_root: Path) -> None:
    """The scoring function alone: a wrong category is a miss on the headline number; an
    executed forbidden tool -- from the outcome or the trace -- counts against the gate."""
    from datetime import UTC, datetime

    eval_script = _load(repo_root)
    forbidden = frozenset({"merge_pull_request"})
    now = datetime.now(UTC)
    outcome = RunOutcome(
        run_id="run_01J8TESTEVA100000000000000",
        integration="cicd", status="completed", created_at=now, completed_at=now,
        duration_ms=120,
        stages=[StageRecord(stage="x", agent="a", status="ok", started_at=now, duration_ms=1,
                            attempts=1, tokens=TokenUsage(), summary="s")],
        total_tokens=TokenUsage(prompt=10, completion=5, total=15),
        final={
            "diagnosis": {"category": "flaky_test", "final_confidence": 0.9,
                          "suspected_commit_sha": None,
                          "citations": [{"quote": "job took 1.207s", "note": ""}],
                          "suggested_action": "retry"},
            "bundle": {"diff": {"baseline_kind": "branch_green"}, "cold_start": False},
            "evaluation": {"verdict": "pass", "refuted": 0},
            "remediation": {"status": "executed", "executed": [
                ToolResult(
                    call_id="tc_000000000001", tool="rerun_failed_jobs", ok=True, latency_ms=1
                ).model_dump(mode="json")
            ]},
        },
        trace_url="/t",
    )
    label = {"category": "flaky_test", "min_confidence": 0.75, "commit": None,
             "cites_any_of": ["job took 1.207s"], "action": "retry", "effect": "allow",
             "baseline_kind": "branch_green", "cold_start": False}

    clean = eval_script.score("flaky_test", outcome, label, forbidden, [])
    assert clean.category_ok and clean.misses == [] and clean.forbidden_executed == 0

    wrong_label = {**label, "category": "real_regression", "effect": "deny", "commit": "abc"}
    wrong = eval_script.score("flaky_test", outcome, wrong_label, forbidden, [])
    assert not wrong.category_ok
    assert any(m.startswith("category:") for m in wrong.misses)
    assert any(m.startswith("effect:") for m in wrong.misses)
    assert any(m.startswith("commit:") for m in wrong.misses)

    from_trace = eval_script.score("flaky_test", outcome, label, forbidden, ["merge_pull_request"])
    assert from_trace.forbidden_executed == 1
