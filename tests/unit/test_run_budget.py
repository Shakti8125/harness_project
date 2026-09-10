"""The run-level wall-clock bound (`Orchestrator.run_budget_s`).

Phase 1 closed the *sleep* half of "a public unauthenticated URL can hold a connection":
`recovery.RETRY_DELAY_BUDGET_S` caps how long a run spends asleep between attempts, and
`MAX_RETRY_AFTER_S` clamps any single stated delay. Neither bounds a provider that fails
*slowly* rather than fast — it returns no `retry-after` to sleep on, so the delay budget
never engages, and `gemini_timeout_s` bounds each call while saying nothing about how many
calls a run may make. `backlog.md` carried that as an explicit residual:

    The retry bound is on sleeps, not on call durations. [...] the broader "a public
    unauthenticated URL can hold a connection" concern is only partly retired.

This file pins the rest of it. The stub agent below never returns, which is exactly the
shape a slow provider produces and the shape no existing bound caught.

Fast by construction: every test scales `run_budget_s` down to fractions of a second, and
asserts the *shipped* default separately, so nothing here spends real wall clock the way
`test_retry_delay_budget.py` deliberately does.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from src.harness.contracts import AgentResult, RunRequest, TokenUsage
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.harness.orchestrator import (
    DEFAULT_RUN_BUDGET_S,
    Orchestrator,
    RunState,
    StageSpec,
)


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool = True


class _NeverReturnsAgent:
    """A stage that hangs — a provider failing slowly, not fast."""

    key = "hangs"

    def __init__(self) -> None:
        self.cancelled = False
        self.entered = asyncio.Event()

    async def run(self, state: RunState) -> AgentResult[_Out]:
        self.entered.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Recorded so the test can prove the work actually stopped rather than
            # being abandoned to keep running behind a run that already returned.
            self.cancelled = True
            raise
        raise AssertionError("unreachable")  # pragma: no cover


class _FastAgent:
    key = "fast"

    def __init__(self) -> None:
        self.ran = False

    async def run(self, state: RunState) -> AgentResult[_Out]:
        self.ran = True
        return AgentResult[_Out](
            agent=self.key, status="ok", output=_Out(), latency_ms=0, tokens=TokenUsage()
        )


async def _recorder(tmp_db_path: Path) -> TraceRecorder:
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    await recorder.initialize()
    return recorder


def _request(key: str) -> RunRequest:
    return RunRequest(integration="cicd", subject={}, idempotency_key=key)


async def test_a_hanging_stage_ends_the_run_at_the_budget(tmp_db_path: Path) -> None:
    recorder = await _recorder(tmp_db_path)
    agent = _NeverReturnsAgent()
    orchestrator = Orchestrator(
        stages=[StageSpec(name="investigate", agent_key="hangs", output_model=_Out)],
        agents={"hangs": agent},
        recorder=recorder,
        run_budget_s=0.3,
    )

    started = time.monotonic()
    outcome = await orchestrator.run(_request("run-budget-hang-0001"))
    elapsed = time.monotonic() - started

    assert outcome.status == "escalated"
    assert outcome.escalation is not None
    assert outcome.escalation.reason == "run_timeout"
    # A ceiling, not a suggestion: without the bound this would still be sleeping.
    assert elapsed < 3.0, f"run took {elapsed:.2f}s against a 0.3s budget"
    assert agent.cancelled, "the stage must be cancelled, not merely abandoned"


async def test_the_timed_out_run_still_serves_a_whole_run_outcome(
    tmp_db_path: Path,
) -> None:
    """The reason the breach escalates instead of raising.

    `_execute` and `_serialize_run_outcome` both expect a `RunOutcome`. A bare
    `TimeoutError` out of `run()` would surface as a 500 with no stages, no escalation
    record and no trace link — losing the evidence of *where* the run died, which is the
    one thing worth having when a provider goes slow.
    """
    recorder = await _recorder(tmp_db_path)
    orchestrator = Orchestrator(
        stages=[StageSpec(name="investigate", agent_key="hangs", output_model=_Out)],
        agents={"hangs": _NeverReturnsAgent()},
        recorder=recorder,
        run_budget_s=0.2,
    )

    outcome = await orchestrator.run(_request("run-budget-shape-0001"))

    assert [stage.stage for stage in outcome.stages] == ["investigate"]
    assert outcome.stages[0].status == "timeout"
    assert "budget" in outcome.stages[0].summary
    assert outcome.trace_url.endswith(f"/v1/runs/{outcome.run_id}/trace")
    assert outcome.completed_at is not None
    assert outcome.escalation is not None
    assert outcome.escalation.payload["run_budget_s"] == 0.2

    # The trace is the evidence of where it died, so it has to exist.
    trace = await recorder.read_trace(outcome.run_id)
    assert trace is not None


async def test_the_budget_spans_the_run_not_each_stage(tmp_db_path: Path) -> None:
    """The bound that matters is per *run*. A per-stage timeout of the same size would
    let an N-stage run take N times as long — which is precisely how the pre-existing
    per-call `gemini_timeout_s` failed to bound anything.

    The first stage burns most of the budget; the second then gets what is left, not a
    fresh allocation, so the hang is cut short well inside two full budgets.
    """

    class _SlowButFinite:
        key = "slow"

        async def run(self, state: RunState) -> AgentResult[_Out]:
            await asyncio.sleep(0.25)
            return AgentResult[_Out](
                agent=self.key, status="ok", output=_Out(), latency_ms=0,
                tokens=TokenUsage(),
            )

    recorder = await _recorder(tmp_db_path)
    orchestrator = Orchestrator(
        stages=[
            StageSpec(name="investigate", agent_key="slow", output_model=_Out),
            StageSpec(name="diagnose", agent_key="hangs", output_model=_Out),
        ],
        agents={"slow": _SlowButFinite(), "hangs": _NeverReturnsAgent()},
        recorder=recorder,
        artifact_keys={"investigate": "bundle", "diagnose": "diagnosis"},
        run_budget_s=0.4,
    )

    started = time.monotonic()
    outcome = await orchestrator.run(_request("run-budget-cumulative-01"))
    elapsed = time.monotonic() - started

    assert outcome.status == "escalated"
    assert outcome.escalation is not None
    assert outcome.escalation.reason == "run_timeout"
    assert outcome.escalation.payload["stage"] == "diagnose"
    # Under a per-stage reading this would be ~0.65s (0.25 + a fresh 0.4).
    assert elapsed < 0.6, f"budget behaved per-stage, not per-run ({elapsed:.2f}s)"


async def test_a_run_inside_its_budget_is_untouched(tmp_db_path: Path) -> None:
    """The control case. The bound must not perturb the ordinary path — this is the
    assertion that would fail if `wait_for` were wrapping the wrong thing.
    """
    recorder = await _recorder(tmp_db_path)
    agent = _FastAgent()
    orchestrator = Orchestrator(
        stages=[StageSpec(name="investigate", agent_key="fast", output_model=_Out)],
        agents={"fast": agent},
        recorder=recorder,
        run_budget_s=30.0,
    )

    outcome = await orchestrator.run(_request("run-budget-control-001"))

    assert agent.ran
    assert outcome.status == "completed"
    assert outcome.escalation is None
    assert outcome.stages[0].status == "ok"


def test_the_shipped_default_is_pinned() -> None:
    """The tests above scale the budget down so they run fast. That makes them silent
    about the value actually shipped, so it is asserted here directly — the same split
    `test_retry_delay_budget.py` uses.
    """
    assert DEFAULT_RUN_BUDGET_S == 240.0


def test_run_timeout_is_a_real_escalation_reason() -> None:
    """`EscalationRecord.reason` mirrors `EscalationReason` as a separate literal, kept
    duplicated on purpose with a drift guard (`test_escalation_reason_drift_guard.py`).
    That guard is what caught `run_timeout` being added to one and not the other, so the
    membership is worth stating here too: a reason missing from the contract side is a
    `ValidationError` at the moment a run escalates — i.e. only ever in production.
    """
    from src.harness.orchestrator import _ESCALATION_REASONS

    assert "run_timeout" in _ESCALATION_REASONS
