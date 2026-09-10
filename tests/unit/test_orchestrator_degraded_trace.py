"""review.md finding 7: `GET /v1/runs/{id}` and `.../trace` must agree on
`degraded_components` for the same run.

Before the fix, `Orchestrator.run`'s run span wrote the literal attribute name
``"degraded"``, while `TraceRecorder.read_trace` aggregates the constant
``ATTR_DEGRADED_COMPONENT`` (``"degraded_component"``). The two spellings never matched, so
the trace side reported `[]` for any run whose only degraded-component write happened after
the writing stage's own `agent.run` span had already closed and been persisted -- exactly
the shape `Investigator` uses: `_DEGRADED_NOTES` is appended to `state.degraded` only after
`super().run()` (and the `agent.run` span it opens and closes) has already returned.

The stub agent below reproduces that shape directly against the real `Orchestrator`, with
no cicd-integration dependency: open and close its own per-stage span first, THEN append to
`state.degraded`. If the run-level span the orchestrator itself owns is the only place this
list is ever completely visible, this is the regression the fix has to survive.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from src.harness.contracts import AgentResult, RunRequest, TokenUsage
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.harness.orchestrator import Orchestrator, RunState, StageSpec

_DEGRADED_COMPONENT = "investigator_notes"


class _StubOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool = True


class _DegradedAfterItsOwnSpanClosesAgent:
    """Mimics `Investigator.run`: the degraded write lands strictly after this agent's
    own per-stage span has already been opened, closed and persisted -- so nothing on
    that span's attributes can carry it. Only `state.degraded`, read by the run span
    AFTER every stage has returned, can still see it.
    """

    key = "stub_investigator"

    def __init__(self, recorder: TraceRecorder) -> None:
        self._recorder = recorder

    async def run(self, state: RunState) -> AgentResult[_StubOutput]:
        async with self._recorder.span("agent.run", "agent", agent=self.key):
            pass  # closes with no degraded attribute at all

        # Written only after the span above has already been persisted.
        state.degraded.append(_DEGRADED_COMPONENT)

        return AgentResult[_StubOutput](
            agent=self.key,
            status="ok",
            output=_StubOutput(),
            latency_ms=0,
            tokens=TokenUsage(),
        )


async def test_trace_degraded_components_matches_outcome_degraded_components(
    tmp_db_path: Path,
) -> None:
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    await recorder.initialize()

    stage = StageSpec(
        name="investigate", agent_key="stub_investigator", output_model=_StubOutput
    )
    orchestrator = Orchestrator(
        stages=[stage],
        agents={"stub_investigator": _DegradedAfterItsOwnSpanClosesAgent(recorder)},
        recorder=recorder,
    )

    request = RunRequest(
        integration="cicd",
        subject={},
        idempotency_key="test-degraded-trace-parity",
    )

    outcome = await orchestrator.run(request)
    assert outcome.degraded_components == [_DEGRADED_COMPONENT]

    trace = await recorder.read_trace(outcome.run_id)
    assert trace is not None
    # This is the assertion that was false before the fix: the trace side reported `[]`
    # because nothing wrote the key `read_trace` actually aggregates.
    assert trace.degraded_components == [_DEGRADED_COMPONENT]
    assert trace.degraded_components == outcome.degraded_components


async def test_a_component_reported_by_both_the_stage_span_and_the_run_span_appears_once(
    tmp_db_path: Path,
) -> None:
    """`read_trace` de-duplicates by construction (`if component not in degraded`).
    Worth pinning on its own: the run-span fix could plausibly have double-counted a
    component that a per-stage span already reported under the same attribute name.
    """

    class _DegradedOnBothSpansAgent:
        key = "stub_both"

        def __init__(self, recorder: TraceRecorder) -> None:
            self._recorder = recorder

        async def run(self, state: RunState) -> AgentResult[_StubOutput]:
            # Reported on the stage's own span (the ordinary `LLMAgent.run` path)...
            async with self._recorder.span(
                "agent.run", "agent", agent=self.key, degraded_component=_DEGRADED_COMPONENT
            ):
                pass
            # ...AND on `state.degraded`, which the run span aggregates a second time.
            state.degraded.append(_DEGRADED_COMPONENT)
            return AgentResult[_StubOutput](
                agent=self.key,
                status="ok",
                output=_StubOutput(),
                latency_ms=0,
                tokens=TokenUsage(),
            )

    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    await recorder.initialize()

    stage = StageSpec(name="investigate", agent_key="stub_both", output_model=_StubOutput)
    orchestrator = Orchestrator(
        stages=[stage],
        agents={"stub_both": _DegradedOnBothSpansAgent(recorder)},
        recorder=recorder,
    )
    request = RunRequest(
        integration="cicd", subject={}, idempotency_key="test-degraded-dedup-parity"
    )

    outcome = await orchestrator.run(request)
    trace = await recorder.read_trace(outcome.run_id)

    assert trace is not None
    assert trace.degraded_components == [_DEGRADED_COMPONENT]  # not doubled
