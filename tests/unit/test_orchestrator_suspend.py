"""`StageSpec.suspend` -- the post-stage hook (A.2 amendment, Phase 2), in isolation.

Pins the three things the hook exists for: a suspension ends the run with the status it
names, the stage's artifact is kept in `final`, and an escalating suspension produces a
proper `EscalationRecord` (with an unknown reason coerced, not lost).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from src.harness.contracts import AgentError, AgentResult, RunRequest, TokenUsage
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.harness.orchestrator import Orchestrator, RunState, StageSpec, Suspension


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    needs_person: bool = False
    refused: bool = False


class Agent:
    key = "a"

    def __init__(self, output: Out) -> None:
        self.output = output
        self.runs = 0

    async def run(self, state: RunState) -> AgentResult[Out]:
        self.runs += 1
        return AgentResult[Out](
            agent=self.key, status="ok", output=self.output, latency_ms=1, tokens=TokenUsage()
        )


def suspend(state: RunState) -> Suspension | None:
    out = state.artifacts["a"]
    assert isinstance(out, Out)
    if out.needs_person:
        return Suspension(status="awaiting_approval", reason="someone must say yes")
    if out.refused:
        return Suspension(
            status="escalated", reason="policy said no", escalate_as="policy_denied",
            payload={"why": "cap"},
        )
    return None


def orchestrator(tmp_db_path: Path, first: Agent, second: Agent) -> Orchestrator:
    return Orchestrator(
        stages=[
            StageSpec(name="a", agent_key="a", output_model=Out, suspend=suspend),
            StageSpec(name="b", agent_key="b", output_model=Out),
        ],
        agents={"a": first, "b": second},
        recorder=TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ())),
    )


def request() -> RunRequest:
    return RunRequest(integration="t", subject={}, idempotency_key="t:suspend-test")


async def test_no_suspension_lets_later_stages_run(tmp_db_path: Path) -> None:
    second = Agent(Out())
    outcome = await orchestrator(tmp_db_path, Agent(Out()), second).run(request())
    assert outcome.status == "completed"
    assert second.runs == 1
    assert set(outcome.final) == {"a", "b"}


async def test_awaiting_approval_ends_the_run_and_keeps_the_artifact(tmp_db_path: Path) -> None:
    second = Agent(Out())
    outcome = await orchestrator(tmp_db_path, Agent(Out(needs_person=True)), second).run(request())
    assert outcome.status == "awaiting_approval"
    assert outcome.escalation is None
    assert second.runs == 0, "a suspended run does not carry on by itself"
    assert outcome.final == {"a": {"needs_person": True, "refused": False}}
    assert [s.stage for s in outcome.stages] == ["a"]
    assert outcome.stages[0].status == "ok"
    assert outcome.stages[0].summary == "someone must say yes"


async def test_escalating_suspension_produces_a_record(tmp_db_path: Path) -> None:
    second = Agent(Out())
    outcome = await orchestrator(tmp_db_path, Agent(Out(refused=True)), second).run(request())
    assert outcome.status == "escalated"
    assert outcome.escalation is not None
    assert outcome.escalation.reason == "policy_denied"
    assert outcome.escalation.message == "policy said no"
    assert outcome.escalation.payload == {"stage": "a", "why": "cap"}
    assert second.runs == 0
    assert "a" in outcome.final


async def test_unknown_escalation_reason_is_coerced_not_dropped(tmp_db_path: Path) -> None:
    def bad_suspend(state: RunState) -> Suspension | None:
        return Suspension(status="escalated", reason="x", escalate_as="not_a_reason")

    orch = Orchestrator(
        stages=[StageSpec(name="a", agent_key="a", output_model=Out, suspend=bad_suspend)],
        agents={"a": Agent(Out())},
        recorder=TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ())),
    )
    outcome = await orch.run(request())
    assert outcome.status == "escalated"
    assert outcome.escalation is not None
    assert outcome.escalation.reason == "unknown_category"


async def test_suspend_is_not_consulted_when_the_stage_failed(tmp_db_path: Path) -> None:
    class Failing(Agent):
        async def run(self, state: RunState) -> AgentResult[Out]:
            self.runs += 1
            return AgentResult[Out](
                agent="a", status="invalid_output", output=None, latency_ms=1,
                tokens=TokenUsage(),
                error=AgentError(kind="invalid_output", message="bad json", attempts=3),
            )

    consulted: list[bool] = []

    def spy(state: RunState) -> Suspension | None:
        consulted.append(True)
        return None

    orch = Orchestrator(
        stages=[StageSpec(name="a", agent_key="a", output_model=Out, suspend=spy)],
        agents={"a": Failing(Out())},
        recorder=TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ())),
    )
    outcome = await orch.run(request())
    assert outcome.status == "escalated"
    assert consulted == [], "no output, nothing to suspend on"
