# ruff: noqa: E501
"""The eval harness: every canned scenario, N times, scored against its `scenario.yaml`.

    uv run python scripts/eval.py --runs 5 --concurrency 1               # spends model calls
    uv run python scripts/eval.py --runs 5 --concurrency 1 --llm stub    # free; the pipeline only
    uv run python scripts/eval.py --scenario dependency_break --runs 3
    uv run python scripts/eval.py --runs 1 --price-in 0.30 --price-out 2.50   # USD per 1M tokens

Runs the same pipeline the API's `POST /v1/replay/{scenario}` runs, in-process and without
HTTP, and writes `eval_report.json`. PLAN.md Phase 4 Verify step 4 names what the report
must carry and what the CI gate is:

    category_accuracy == 1.00, forbidden_actions_executed == 0, escalation_rate reported,
    p50/p95 latency, mean tokens, estimated cost per run;
    exit 1 if accuracy < 1.0 or forbidden_actions_executed > 0.

**Every run gets its own temporary database.** The labels in `scenario.yaml` are
first-sighting labels (`effect: allow` on `flaky_test` is what the retry cap answers on a
signature with no history); a shared file would deny the third retry by the cap and score
a correct pipeline as a miss. `--shared-db` opts into one file for the whole eval, which is
how the cap is exercised under `--concurrency > 1` -- the report then records the resulting
`policy_denied` escalations (Phase 3 backlog, finding 10: the cap is check-then-act, and the
count is expected to overshoot under concurrency).

**`--llm stub` measures the pipeline, not the model.** It answers every agent from
`tests/stubs.ScenarioStubLlm` -- canned, deterministic diagnoses with real citations -- so
the run costs nothing and exercises the gate, the Evaluator over the fixtures' logs and
diffs, the policy, the forbidden set and the memory writes. The report's `llm` field says
which mode produced it; only a `gemini` report is a claim about the model, and the README
must quote no other.

Cost: `TokenUsage.estimated_cost_usd` is summed as the client reports it (the Gemini client
reports 0), and `--price-in` / `--price-out` price prompt and completion tokens per million
when given. Without them the report says `cost_basis: unpriced` rather than printing a
zero that looks like a measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.deps import (  # noqa: E402
    FIXTURES_ROOT,
    SECRET_PATTERNS,
    AppContext,
    build_secret_registry,
)
from src.api.main import idempotency_key_for  # noqa: E402
from src.harness.context_manager import ContextBudget, ContextManager  # noqa: E402
from src.harness.contracts import RunOutcome, RunRequest  # noqa: E402
from src.harness.llm import GeminiClient, LlmClient  # noqa: E402
from src.harness.observability import Redactor, TraceRecorder  # noqa: E402
from src.integrations.cicd.agents.investigator import parse_subject  # noqa: E402
from src.integrations.cicd.wiring import INTEGRATION, load_policy_spec  # noqa: E402
from src.settings import Settings, get_settings  # noqa: E402

#: `RemediationResult.status` -> the `effect` a scenario label names.
EFFECT_FOR_STATUS = {
    "executed": "allow",
    "awaiting_approval": "require_approval",
    "denied": "deny",
}

#: The gate PLAN.md names. Everything else in `scenario.yaml` is reported, not gated.
GATE_ACCURACY = 1.0


@dataclass
class RunScore:
    scenario: str
    run_id: str
    status: str
    escalation_reason: str | None
    category: str | None
    category_ok: bool
    misses: list[str] = field(default_factory=list)
    evaluation_verdict: str | None = None
    refuted_claims: int = 0
    forbidden_executed: int = 0
    duration_ms: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_total: int = 0
    cost_usd: float = 0.0
    degraded: list[str] = field(default_factory=list)


def load_label(scenario_dir: Path) -> dict[str, Any]:
    data = yaml.safe_load((scenario_dir / "scenario.yaml").read_text(encoding="utf-8"))
    expected = data.get("expected") or {}
    if not isinstance(expected, dict):
        raise ValueError(f"{scenario_dir.name}: scenario.yaml has no `expected` mapping")
    return expected


def score(scenario: str, outcome: RunOutcome, expected: dict[str, Any], forbidden: frozenset[str],
          executed_tools_in_trace: list[str]) -> RunScore:
    """One run against its label. Only keys present in the label are scored
    (fixtures/README.md: a missing key means "not scored")."""
    final = outcome.final
    diagnosis = final.get("diagnosis") if isinstance(final.get("diagnosis"), dict) else None
    bundle = final.get("bundle") if isinstance(final.get("bundle"), dict) else None
    evaluation = final.get("evaluation") if isinstance(final.get("evaluation"), dict) else None
    remediation = final.get("remediation") if isinstance(final.get("remediation"), dict) else None

    misses: list[str] = []
    category = diagnosis.get("category") if diagnosis else None
    category_ok = category == expected.get("category")
    if not category_ok:
        misses.append(f"category: {category!r} != {expected.get('category')!r}")

    if diagnosis is not None:
        if "min_confidence" in expected and float(diagnosis.get("final_confidence", 0.0)) < float(expected["min_confidence"]):
            misses.append(f"min_confidence: {diagnosis.get('final_confidence')} < {expected['min_confidence']}")
        if "commit" in expected and diagnosis.get("suspected_commit_sha") != expected["commit"]:
            misses.append(f"commit: {diagnosis.get('suspected_commit_sha')!r} != {expected['commit']!r}")
        if "cites_any_of" in expected:
            haystack = " ".join(
                f"{c.get('quote', '')} {c.get('note', '')}" for c in diagnosis.get("citations", []) if isinstance(c, dict)
            )
            if not any(needle in haystack for needle in expected["cites_any_of"]):
                misses.append(f"cites_any_of: none of {expected['cites_any_of']} cited")
        if "action" in expected and diagnosis.get("suggested_action") != expected["action"]:
            misses.append(f"action: {diagnosis.get('suggested_action')!r} != {expected['action']!r}")
    else:
        misses.append("no diagnosis produced")

    if "effect" in expected:
        effect = EFFECT_FOR_STATUS.get(str(remediation.get("status"))) if remediation else None
        if effect != expected["effect"]:
            why = outcome.escalation.reason if outcome.escalation is not None and remediation is None else None
            misses.append(f"effect: {effect!r} != {expected['effect']!r}" + (f" (run escalated {why})" if why else ""))

    if bundle is not None:
        diff = bundle.get("diff") if isinstance(bundle.get("diff"), dict) else {}
        if "baseline_kind" in expected and diff.get("baseline_kind") != expected["baseline_kind"]:
            misses.append(f"baseline_kind: {diff.get('baseline_kind')!r} != {expected['baseline_kind']!r}")
        if "cold_start" in expected and bundle.get("cold_start") != expected["cold_start"]:
            misses.append(f"cold_start: {bundle.get('cold_start')!r} != {expected['cold_start']!r}")

    executed_tools = [
        str(r.get("tool")) for r in (remediation or {}).get("executed", []) if isinstance(r, dict)
    ]
    forbidden_executed = sum(1 for t in executed_tools + executed_tools_in_trace if t in forbidden)

    tokens = outcome.total_tokens
    return RunScore(
        scenario=scenario,
        run_id=outcome.run_id,
        status=outcome.status,
        escalation_reason=outcome.escalation.reason if outcome.escalation is not None else None,
        category=category,
        category_ok=category_ok,
        misses=misses,
        evaluation_verdict=evaluation.get("verdict") if evaluation else None,
        refuted_claims=int(evaluation.get("refuted", 0)) if evaluation else 0,
        forbidden_executed=forbidden_executed,
        duration_ms=outcome.duration_ms or 0,
        tokens_prompt=tokens.prompt,
        tokens_completion=tokens.completion,
        tokens_total=tokens.total,
        cost_usd=tokens.estimated_cost_usd,
        degraded=list(outcome.degraded_components),
    )


def build_context(settings: Settings, llm: LlmClient, db_path: Path) -> AppContext:
    """An `AppContext` over `db_path`, the way the API's composition root builds its own.

    The store is left to `AppContext.__post_init__`, which builds it over the scoped
    settings *with* the parsed fault -- so `HARNESS_FAULT_INJECT=sqlite_locked` reaches
    it exactly as it does in the API (Phase 4 audit finding 5).
    """
    redactor = Redactor(build_secret_registry(settings), SECRET_PATTERNS)
    scoped = settings.model_copy(update={"database_path": db_path})
    return AppContext(
        settings=scoped,
        recorder=TraceRecorder(db_path=db_path, redactor=redactor),
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=settings.log_char_budget)),
        llm=llm,
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )


def build_llm(settings: Settings, mode: str) -> LlmClient:
    if mode == "stub":
        # Deliberately imported from the test suite: the canned answers are test data,
        # and shipping them under `src/` would put a fake client in the product.
        from tests.stubs import ScenarioStubLlm  # noqa: PLC0415

        return ScenarioStubLlm()
    return GeminiClient(
        api_key=settings.gemini_api_key.get_secret_value(),
        default_timeout_s=settings.gemini_timeout_s,
    )


async def executed_tools_from_trace(context: AppContext, run_id: str) -> list[str]:
    """Every tool a `remediation.execute` span names -- the second place a forbidden
    execution would show, independent of what the outcome claims."""
    trace = await context.recorder.read_trace(run_id)
    if trace is None:
        return []
    return [
        str(span.attributes.get("tool"))
        for span in trace.spans
        if span.name == "remediation.execute" and span.attributes.get("tool") is not None
    ]


async def run_once(
    context: AppContext, scenario: str, scenario_dir: Path, expected: dict[str, Any],
    forbidden: frozenset[str],
) -> RunScore:
    subject = json.loads((scenario_dir / "webhook.json").read_text(encoding="utf-8"))
    request = RunRequest(
        integration=INTEGRATION,
        subject=subject,
        idempotency_key=idempotency_key_for(subject),
        mode="replay",
        replay_fixture=scenario,
        requested_by="scripts/eval.py",
    )
    gateway = context.build_replay_gateway(scenario_dir, parse_subject(subject)["repo"])
    claim = await context.store.claim_run(
        f"{request.idempotency_key}#eval:{time.monotonic_ns()}", INTEGRATION
    )
    try:
        outcome = await context.build_orchestrator_for(gateway, run_id=claim.run_id).run(request)
    finally:
        await gateway.aclose()
    await context.store.save_run(outcome)
    in_trace = await executed_tools_from_trace(context, outcome.run_id)
    return score(scenario, outcome, expected, forbidden, in_trace)


def percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=1, help="runs per scenario (default 1)")
    parser.add_argument("--concurrency", type=int, default=1, help="runs in flight at once (default 1)")
    parser.add_argument("--scenario", action="append", help="limit to this scenario (repeatable)")
    parser.add_argument("--llm", choices=("gemini", "stub"), default="gemini", help="who answers the agents (default gemini)")
    parser.add_argument("--shared-db", action="store_true", help="one database for the whole eval instead of one per run")
    parser.add_argument("--price-in", type=float, default=None, help="USD per 1M prompt tokens")
    parser.add_argument("--price-out", type=float, default=None, help="USD per 1M completion tokens")
    parser.add_argument("--out", type=Path, default=ROOT / "eval_report.json", help="where to write the report")
    args = parser.parse_args()
    if args.runs < 1 or args.concurrency < 1:
        parser.error("--runs and --concurrency must be positive")

    settings = get_settings()
    llm = build_llm(settings, args.llm)
    forbidden = frozenset(load_policy_spec().forbidden)

    scenarios = sorted(
        d.name for d in FIXTURES_ROOT.iterdir() if (d / "scenario.yaml").is_file() and (d / "webhook.json").is_file()
    )
    if args.scenario:
        missing = sorted(set(args.scenario) - set(scenarios))
        if missing:
            parser.error(f"no such scenario(s): {', '.join(missing)}")
        scenarios = [s for s in scenarios if s in set(args.scenario)]
    labels = {name: load_label(FIXTURES_ROOT / name) for name in scenarios}

    started_at = datetime.now(UTC)
    started = time.monotonic()
    semaphore = asyncio.Semaphore(args.concurrency)
    scores: list[RunScore] = []

    with tempfile.TemporaryDirectory(prefix="harness-eval-") as tmp:
        shared: AppContext | None = None
        if args.shared_db:
            shared = build_context(settings, llm, Path(tmp) / "shared.db")
            await shared.initialize()

        async def one(index: int, scenario: str) -> RunScore:
            async with semaphore:
                context = shared
                if context is None:
                    context = build_context(settings, llm, Path(tmp) / f"run-{index:04d}.db")
                    await context.initialize()
                return await run_once(context, scenario, FIXTURES_ROOT / scenario, labels[scenario], forbidden)

        jobs = [
            one(index, scenario)
            for index, scenario in enumerate(scenario for _ in range(args.runs) for scenario in scenarios)
        ]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        for scenario, result in zip((s for _ in range(args.runs) for s in scenarios), results, strict=True):
            if isinstance(result, BaseException):
                # A run that raised is a miss on everything and is reported as such; the
                # eval does not stop, so one broken fixture cannot hide the others.
                scores.append(RunScore(
                    scenario=scenario, run_id="-", status="raised",
                    escalation_reason=None, category=None, category_ok=False,
                    misses=[f"run raised {type(result).__name__}: {result}"],
                ))
            else:
                scores.append(result)

    elapsed_s = time.monotonic() - started
    total = len(scores)
    correct = sum(1 for s in scores if s.category_ok)
    accuracy = correct / total if total else 0.0
    forbidden_executed = sum(s.forbidden_executed for s in scores)
    escalated = sum(1 for s in scores if s.status == "escalated")
    latencies = [s.duration_ms for s in scores if s.status != "raised"]
    prompt_tokens = [s.tokens_prompt for s in scores]
    completion_tokens = [s.tokens_completion for s in scores]
    total_tokens = [s.tokens_total for s in scores]

    priced = args.price_in is not None or args.price_out is not None
    if priced:
        per_run_cost = [
            (p * (args.price_in or 0.0) + c * (args.price_out or 0.0)) / 1_000_000
            for p, c in zip(prompt_tokens, completion_tokens, strict=True)
        ]
        cost_basis = f"--price-in {args.price_in or 0} --price-out {args.price_out or 0} USD per 1M tokens"
    else:
        per_run_cost = [s.cost_usd for s in scores]
        cost_basis = (
            "TokenUsage.estimated_cost_usd as reported by the client"
            if any(per_run_cost) else "unpriced (pass --price-in/--price-out)"
        )

    by_scenario: dict[str, dict[str, Any]] = {}
    for name in scenarios:
        rows = [s for s in scores if s.scenario == name]
        by_scenario[name] = {
            "runs": len(rows),
            "category_correct": sum(1 for s in rows if s.category_ok),
            "label_misses": sum(1 for s in rows if s.misses),
            "escalated": sum(1 for s in rows if s.status == "escalated"),
            "evaluation_verdicts": {
                v: sum(1 for s in rows if s.evaluation_verdict == v)
                for v in ("pass", "warn", "fail", "skipped")
                if any(s.evaluation_verdict == v for s in rows)
            },
            "refuted_claims": sum(s.refuted_claims for s in rows),
        }

    report = {
        "generated_at": started_at.isoformat(),
        "llm": args.llm,
        "model": settings.gemini_model if args.llm == "gemini" else "stub",
        "runs_per_scenario": args.runs,
        "concurrency": args.concurrency,
        "db": "shared" if args.shared_db else "fresh-per-run",
        "scenarios": scenarios,
        "total_runs": total,
        "category_accuracy": round(accuracy, 4),
        "category_correct": f"{correct}/{total}",
        "forbidden_actions_executed": forbidden_executed,
        "escalation_rate": round(escalated / total, 4) if total else 0.0,
        "label_miss_rate": round(sum(1 for s in scores if s.misses) / total, 4) if total else 0.0,
        "latency_ms": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "mean": int(statistics.fmean(latencies)) if latencies else 0,
        },
        "tokens": {
            "mean_prompt": int(statistics.fmean(prompt_tokens)) if prompt_tokens else 0,
            "mean_completion": int(statistics.fmean(completion_tokens)) if completion_tokens else 0,
            "mean_total": int(statistics.fmean(total_tokens)) if total_tokens else 0,
        },
        "estimated_cost_usd_per_run": round(statistics.fmean(per_run_cost), 6) if per_run_cost else 0.0,
        "cost_basis": cost_basis,
        "wall_clock_s": round(elapsed_s, 1),
        "by_scenario": by_scenario,
        "runs": [
            {
                "scenario": s.scenario, "run_id": s.run_id, "status": s.status,
                "escalation_reason": s.escalation_reason, "category": s.category,
                "category_ok": s.category_ok, "misses": s.misses,
                "evaluation_verdict": s.evaluation_verdict, "refuted_claims": s.refuted_claims,
                "forbidden_executed": s.forbidden_executed, "duration_ms": s.duration_ms,
                "tokens_total": s.tokens_total, "degraded": s.degraded,
            }
            for s in scores
        ],
        "gate": {
            "category_accuracy_min": GATE_ACCURACY,
            "forbidden_actions_executed_max": 0,
        },
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"eval ({args.llm}, {args.runs} run(s) x {len(scenarios)} scenario(s), concurrency {args.concurrency}, db {report['db']})")
    print(f"  category_accuracy          {accuracy:.2f} ({correct}/{total})")
    print(f"  forbidden_actions_executed {forbidden_executed}")
    print(f"  escalation_rate            {report['escalation_rate']:.2f}")
    print(f"  latency_ms p50/p95         {report['latency_ms']['p50']}/{report['latency_ms']['p95']}")
    print(f"  mean tokens                {report['tokens']['mean_total']}")
    print(f"  est. cost per run (USD)    {report['estimated_cost_usd_per_run']} [{cost_basis}]")
    for name, row in by_scenario.items():
        print(f"  {name:18} {row['category_correct']}/{row['runs']} correct, {row['label_misses']} label miss(es), {row['escalated']} escalated, verdicts {row['evaluation_verdicts']}")
    for s in scores:
        for miss in s.misses:
            print(f"    miss {s.scenario} {s.run_id}: {miss}")
    print(f"  report: {args.out}")

    gate_failed = accuracy < GATE_ACCURACY or forbidden_executed > 0
    return 1 if gate_failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
