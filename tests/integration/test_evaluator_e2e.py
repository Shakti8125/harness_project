"""PLAN.md Phase 4 Verify steps 2 and 3 over HTTP, plus the seams the phase wired.

Same shape as `test_memory_e2e.py`: the model behind the fault-injecting client is
`ScenarioStubLlm`, everything else is real -- the routes, the composition root's fault
routing, the four-stage orchestrator, the Evaluator over the fixtures' real logs and
diffs, the policy, and a `SqliteMemoryStore` on the per-test temp file. Every
fault-injected run also asserts how many times the model behind the fault was called,
because "a fault-injected run must not spend quota" is the property the faults exist for.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.deps import FIXTURES_ROOT, SECRET_PATTERNS, AppContext, build_secret_registry
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import RunRequest
from src.harness.faults import Fault
from src.harness.observability import Redactor, TraceRecorder
from src.integrations.cicd.agents.diagnostician import FABRICATED_QUOTE
from src.integrations.cicd.wiring import FAULT_FABRICATE_CITATION
from src.settings import get_settings
from tests.stubs import ScenarioStubLlm


def make_context(
    tmp_db_path: Path, *, fault: str | None = None, llm: Any | None = None
) -> AppContext:
    settings = get_settings().model_copy(update={"fault_inject": fault})
    return AppContext(
        settings=settings,
        recorder=TraceRecorder(
            db_path=tmp_db_path,
            redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
        ),
        context_manager=ContextManager(
            default_budget=ContextBudget(total_chars=settings.log_char_budget)
        ),
        llm=llm or ScenarioStubLlm(),
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 429 fault's jittered backoff is real time; the test has no use for it."""
    import src.harness.recovery as recovery

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(recovery.asyncio, "sleep", no_sleep)


def replay(client: TestClient, scenario: str, **params: Any) -> dict[str, Any]:
    response = client.post(f"/v1/replay/{scenario}", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------------------------
# Verify step 2: refuted evidence escalates and the Remediator never runs
# ---------------------------------------------------------------------------


def test_fabricated_citation_escalates_evidence_refuted_and_skips_the_remediator(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = ScenarioStubLlm()
    context = make_context(tmp_db_path, fault=FAULT_FABRICATE_CITATION, llm=stub)
    assert context.fault == Fault(FAULT_FABRICATE_CITATION)
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as client:
        body = replay(client, "real_regression")

    # PLAN.md's jq: {status, verdict, reason, stages}
    assert body["status"] == "escalated"
    assert body["final"]["evaluation"]["verdict"] == "fail"
    assert body["escalation"]["reason"] == "evidence_refuted"
    agents = [stage["agent"] for stage in body["stages"]]
    assert agents == ["investigator", "diagnostician", "evaluator", None]
    assert "remediator" not in agents
    assert [stage["stage"] for stage in body["stages"]] == [
        "investigate", "diagnose", "evaluate", "remediate",
    ]
    assert body["stages"][-1]["status"] == "gated"
    assert "remediation" not in body["final"]
    assert "1 of 1 claim(s) refuted" in body["escalation"]["message"]

    # The fabricated citation is what was checked, and it is refuted by name.
    evaluation = body["final"]["evaluation"]
    assert evaluation["refuted"] == 1 and evaluation["verified"] == 0
    assert evaluation["confidence_delta"] == pytest.approx(-0.15)
    verdict = evaluation["verdicts"][0]
    assert verdict["result"] == "refuted" and verdict["kind"] == "quote_exists"
    diagnosis = body["final"]["diagnosis"]
    assert diagnosis["citations"][0]["quote"] == FABRICATED_QUOTE
    # The penalty is on the diagnosis the API serves: 0.92 - 0.15 = 0.77.
    assert diagnosis["final_confidence"] == pytest.approx(0.77)
    assert [a["name"] for a in diagnosis["confidence_adjustments"]] == ["evidence_refuted"]
    # Two model calls (Investigator, Diagnostician); the Remediator never asked.
    assert len(stub.prompts) == 2
    assert not any("You are the Remediator" in p for p in stub.prompts)

    # The trace shows the refutation as its own span, under the evaluator's agent span.
    trace = client.get(f"/v1/runs/{body['run_id']}/trace").json()
    claim_spans = [s for s in trace["spans"] if s["name"] == "evaluation.claim"]
    assert len(claim_spans) == 1
    assert claim_spans[0]["attributes"]["result"] == "refuted"
    assert claim_spans[0]["component"] == "evaluator"

    # ...and the escalation is durable on the `db` channel with the record's delivery.
    with sqlite3.connect(tmp_db_path) as db:
        row = db.execute(
            "select reason, channel, delivery_error from escalation"
            " order by created_at desc limit 1"
        ).fetchone()
    assert row == ("evidence_refuted", "db", None)


def test_the_verdict_is_remembered_after_the_penalty_not_before(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatch decision 1, the one place this phase changes Phase 3's semantics: memory
    sees the post-penalty confidence, and a refuted verdict that falls under the gate is
    a sighting, not a verdict. 0.80 - 0.15 = 0.65 < 0.70."""
    context = make_context(
        tmp_db_path, fault=FAULT_FABRICATE_CITATION, llm=ScenarioStubLlm(self_confidence=0.80)
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as client:
        body = replay(client, "real_regression")

    assert body["status"] == "escalated"
    # Refutation is checked before the confidence gate: the reason is the refutation,
    # even though 0.65 would also have failed the gate.
    assert body["escalation"]["reason"] == "evidence_refuted"
    assert body["final"]["diagnosis"]["final_confidence"] == pytest.approx(0.65)
    with sqlite3.connect(tmp_db_path) as db:
        signature = db.execute(
            "select occurrences, last_verdict, verdict_counts from failure_signature"
        ).fetchall()
        observation = db.execute("select verdict, confidence from observation").fetchall()
    assert signature == [(1, None, "{}")], "a refuted, gated verdict counts as a sighting only"
    assert observation == [("real_regression", pytest.approx(0.65))]


def test_a_verified_diagnosis_is_remembered_with_its_bonus(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = make_context(tmp_db_path)
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as client:
        body = replay(client, "real_regression")
    assert body["final"]["evaluation"]["verdict"] == "pass"
    assert body["final"]["diagnosis"]["final_confidence"] == pytest.approx(0.97)
    with sqlite3.connect(tmp_db_path) as db:
        signature = db.execute(
            "select occurrences, last_verdict from failure_signature"
        ).fetchall()
        observation = db.execute("select confidence from observation").fetchall()
    assert signature == [(1, "real_regression")]
    assert observation == [(pytest.approx(0.97),)]


# ---------------------------------------------------------------------------
# Verify step 3: Recovery recovers
# ---------------------------------------------------------------------------


def test_llm_bad_json_2_recovers_with_three_attempts_and_one_real_call_per_agent(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = ScenarioStubLlm()
    context = make_context(tmp_db_path, fault="llm_bad_json:2", llm=stub)
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as client:
        body = replay(client, "flaky_test")

    # PLAN.md's jq: {status, attempts: .stages[1].attempts}
    assert body["status"] == "completed"
    assert body["stages"][1]["attempts"] == 3
    assert [s["attempts"] for s in body["stages"]] == [3, 3, 1, 3]
    assert [s["status"] for s in body["stages"]] == ["ok", "ok", "ok", "ok"]
    # Three real calls in total -- one per model-backed agent -- and six junk answers
    # that never left the process.
    assert len(stub.prompts) == 3
    assert body["final"]["remediation"]["status"] == "executed"
    assert body["degraded_components"] == []


def test_llm_bad_json_9_never_recovers_and_spends_nothing(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = ScenarioStubLlm()
    context = make_context(tmp_db_path, fault="llm_bad_json:9", llm=stub)
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as client:
        body = replay(client, "flaky_test")

    # PLAN.md's jq: {status, reason}
    assert body["status"] == "escalated"
    assert body["escalation"]["reason"] == "invalid_output"
    # The Investigator degrades on a content failure and continues; the Diagnostician
    # cannot, and the run ends there.
    assert [s["status"] for s in body["stages"]] == ["ok", "invalid_output"]
    assert body["stages"][0]["attempts"] == 3 and body["stages"][1]["attempts"] == 3
    assert "investigator_notes" in body["degraded_components"]
    assert stub.prompts == [], "nine junk answers cost zero model calls"

    # PLAN.md's sqlite3 line, as amended: the row is filed on the `db` channel.
    with sqlite3.connect(tmp_db_path) as db:
        row = db.execute(
            "select reason, channel from escalation order by created_at desc limit 1"
        ).fetchone()
    assert row == ("invalid_output", "db")
    # ...and the record itself names every channel the run delivered on.
    assert body["escalation"]["channels"] == ["log", "db"]


def test_llm_429_3_completes_on_the_fourth_attempt(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    stub = ScenarioStubLlm()
    context = make_context(tmp_db_path, fault="llm_429:3", llm=stub)
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as client:
        body = replay(client, "flaky_test")

    assert body["status"] == "completed"
    assert [s["attempts"] for s in body["stages"]] == [4, 4, 1, 4]
    assert len(stub.prompts) == 3

    trace = client.get(f"/v1/runs/{body['run_id']}/trace").json()
    attempts = [s for s in trace["spans"] if s["name"] == "llm.attempt"]
    assert len(attempts) == 12
    assert sum(1 for s in attempts if s.get("error")) == 9


def test_faults_are_per_run_not_per_process(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client is wrapped per run, so a second replay sees the fault again."""
    stub = ScenarioStubLlm()
    context = make_context(tmp_db_path, fault="llm_bad_json:1", llm=stub)
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as client:
        first = replay(client, "flaky_test")
        second = replay(client, "flaky_test")
    assert [s["attempts"] for s in first["stages"]] == [2, 2, 1, 2]
    assert [s["attempts"] for s in second["stages"]] == [2, 2, 1, 2]
    assert len(stub.prompts) == 6


# ---------------------------------------------------------------------------
# warn: an unverifiable citation downgrades the retry to an approval
# ---------------------------------------------------------------------------


class TwoCitationStub(ScenarioStubLlm):
    """The flaky diagnosis with a second, diff-side citation beside the log one.

    With the log missing the `test_in_log` claim is unverifiable and the `file_in_diff`
    claim (README.md is in `flaky_test`'s diff) verifies: 1 of 2 is exactly PLAN.md's
    0.5 line, so the verdict is `warn`, not `fail`. A single unverifiable citation would
    fail on the share rule (dispatch decision 4, taken literally) -- `test_evaluator.py`
    pins that reading; this test exercises the downgrade it leaves room for.
    """

    async def generate(self, req: Any) -> Any:
        response = await super().generate(req)
        if "You are the Diagnostician" not in req.prompt:
            return response
        payload = json.loads(response.text)
        payload["citations"].append(
            {"claim_kind": "file_in_diff", "locator": "diff:README.md",
             "quote": "README.md", "note": "unrelated to the scheduler"}
        )
        return response.model_copy(update={"text": json.dumps(payload)})


def test_warn_downgrades_an_allowed_retry_to_require_approval(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    """`flaky_test` with its log recording removed: the fetch fails, the run is degraded
    on `logs`, the stub's `test_in_log` citation is `unverifiable`, the verdict is `warn`,
    and the retry the policy allows is downgraded to `require_approval` -- the run ends
    `awaiting_approval` and the decision says it was downgraded from `allow`."""
    scenario = tmp_path / "flaky_no_logs"
    shutil.copytree(FIXTURES_ROOT / "flaky_test", scenario)
    shutil.rmtree(scenario / "logs")
    context = make_context(tmp_db_path, llm=TwoCitationStub())
    webhook = json.loads((scenario / "webhook.json").read_text(encoding="utf-8"))

    async def run() -> Any:
        await context.initialize()
        gateway = context.build_replay_gateway(scenario, "octo-org/harness-demo-repo")
        try:
            return await context.build_orchestrator_for(gateway).run(
                RunRequest(
                    integration="cicd", subject=webhook, idempotency_key="cicd:warn-test",
                    mode="replay", replay_fixture="flaky_no_logs",
                )
            )
        finally:
            await gateway.aclose()

    outcome = asyncio.run(run())
    assert "logs" in outcome.degraded_components
    evaluation = outcome.final["evaluation"]
    assert evaluation["verdict"] == "warn"
    assert [v["result"] for v in evaluation["verdicts"]] == ["unverifiable", "verified"]
    assert evaluation["confidence_delta"] == 0.0
    assert "1 of 2 claim(s) unverifiable" in evaluation["reason"]
    assert outcome.status == "awaiting_approval"
    remediation = outcome.final["remediation"]
    decision = remediation["decisions"][0]
    assert decision["rule_id"] == "retry-suspected-flaky"
    assert decision["effect"] == "require_approval"
    assert decision["downgraded_from"] == "allow"
    assert "verdict warn" in decision["reason"]
    assert remediation["executed"] == []


# ---------------------------------------------------------------------------
# The approval route re-judges under the run's stored verdict
# ---------------------------------------------------------------------------


def test_approval_re_evaluates_under_the_stored_evaluation_verdict(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatch decision 8: `_execute_approved` reads `final.evaluation.verdict` through
    the same helper the Remediator used in-run. Proven by moving the stored verdict: a
    plan suspended under `pass` and approved after the stored report says `fail` is
    denied by the policy (no rule admits `fail`), escalates `policy_denied`, and executes
    nothing -- the decision names the clause."""
    context = make_context(tmp_db_path)
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as client:
        body = replay(client, "real_regression")
        assert body["status"] == "awaiting_approval"
        assert body["final"]["evaluation"]["verdict"] == "pass"
        apr = body["final"]["remediation"]["pending_approval"]["approval_id"]

        async def flip_verdict() -> None:
            outcome = await context.store.get_run(body["run_id"])
            assert outcome is not None
            final = dict(outcome.final)
            evaluation = dict(final["evaluation"])  # type: ignore[arg-type]
            evaluation["verdict"] = "fail"
            final["evaluation"] = evaluation
            await context.store.save_run(outcome.model_copy(update={"final": final}))

        asyncio.run(flip_verdict())
        response = client.post(
            f"/v1/approvals/{apr}", json={"decision": "approve", "actor": "shakti"}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["state"] == "approved"
        assert payload["executed"] == []
        assert {d["effect"] for d in payload["decisions"]} == {"deny"}
        assert all("evaluation.verdict: 'fail'" in d["reason"] for d in payload["decisions"])

        run = client.get(f"/v1/runs/{body['run_id']}").json()
        assert run["status"] == "escalated"
        assert run["escalation"]["reason"] == "policy_denied"
        assert run["escalation"]["payload"]["approval_id"] == apr
        assert run["escalation"]["channels"] == ["log", "db"]


# ---------------------------------------------------------------------------
# The fourth fixture
# ---------------------------------------------------------------------------


def test_dependency_break_replays_to_its_label(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dependency_break`: one manifest change, one dependency bump, every claim kind the
    stub cites verified against the fixture's own log and compare recording, and the
    fix-PR plan held for approval."""
    context = make_context(tmp_db_path)
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as client:
        body = replay(client, "dependency_break")

    assert body["status"] == "awaiting_approval"
    bundle = body["final"]["bundle"]
    assert bundle["dependency_changes"] == [{
        "ecosystem": "pip", "manifest_path": "requirements.txt", "package": "pydantic",
        "from_version": "1.10.13", "to_version": "2.9.2", "source": "manifest_diff",
    }]
    assert bundle["diff"]["commit_shas"] == ["c3d9e1f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4"]
    assert [f["path"] for f in bundle["diff"]["files"]] == ["requirements.txt"]
    evaluation = body["final"]["evaluation"]
    assert evaluation["verdict"] == "pass" and evaluation["verified"] == 4
    assert [v["kind"] for v in evaluation["verdicts"]] == [
        "dependency_bump", "quote_exists", "file_in_diff", "commit_in_range",
    ]
    diagnosis = body["final"]["diagnosis"]
    assert diagnosis["category"] == "dependency_break"
    assert diagnosis["final_confidence"] >= 0.85
    assert diagnosis["suspected_commit_sha"] == "c3d9e1f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4"
    remediation = body["final"]["remediation"]
    assert remediation["status"] == "awaiting_approval"
    assert {d["rule_id"] for d in remediation["decisions"]} == {"open-fix-pr"}
