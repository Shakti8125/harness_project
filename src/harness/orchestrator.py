"""Staged run driver.

Frozen transcription of PLAN.md Appendix A.2, plus the Phase 1 run loop. The
orchestrator knows about stages, gates and run state; it does not know what a stage
produces. Stage payload types arrive as ``StageSpec.output_model`` and short-circuit
logic arrives as ``StageSpec.gate`` -- both supplied by the integration, so every
short-circuit condition that is domain-specific lives outside this module.

**A gate guards the stage it is attached to, and runs before that stage does.** That is
what makes the confidence short-circuit of A.2 a `StageSpec.gate` on the remediate stage
rather than a branch in this loop: "is the diagnosis good enough to act on" is asked at
the moment something would act. In Phase 1 no remediating agent exists yet, so the
integration registers the stage with its gate and no agent behind it; the gate still
fires, still escalates, and Phase 2 fills in the agent without moving the condition.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, JsonValue

from src.harness.contracts import (
    EscalationRecord,
    RunId,
    RunOutcome,
    RunRequest,
    StageRecord,
    TokenUsage,
)
from src.harness.errors import ConfigurationError
from src.harness.observability import ATTR_DEGRADED_COMPONENT, TraceRecorder

if TYPE_CHECKING:
    from src.harness.agent import Agent

logger = logging.getLogger("harness.orchestrator")

# PLAN.md "Concrete numbers in one place": run heartbeat interval / staleness = 15 s / 120 s.
HEARTBEAT_INTERVAL_S: Final[float] = 15.0
HEARTBEAT_STALE_AFTER_S: Final[float] = 120.0

#: Crockford base32: no I, L, O or U, so a transcribed run id cannot be misread. Matches
#: the alphabet `contracts.RunId`'s pattern accepts.
_CROCKFORD: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Must stay member-for-member identical to `EscalationRecord.reason` in `contracts.py`:
#: this alias is what the mapping below and `_escalate` are typed against, and a member
#: present here but not there is a `ValidationError` at the moment a run escalates.
EscalationReason = Literal[
    "low_confidence", "evidence_refuted", "invalid_output", "llm_timeout",
    "llm_upstream", "config_error", "policy_denied", "tool_failure",
    "cold_start_restricted", "rate_limited", "unknown_category",
]

_ESCALATION_REASONS: Final[frozenset[str]] = frozenset(get_args(EscalationReason))

#: The verdict/reason pair a stage failure resolves to. Named rather than spelled inline at
#: each use site so the table below and its fallback cannot drift apart, and -- more to the
#: point -- so the fallback can be *annotated*. A bare tuple literal written as the default
#: argument of `.get()` is inferred as `tuple[str, str]`, which widens `reason` back to `str`
#: and quietly retires the one check that keeps this table honest: that every reason in it is
#: a real `EscalationReason` member.
_StageOutcome = tuple[Literal["escalated", "failed"], EscalationReason]

#: How a failed stage becomes a run verdict. Appendix B.1 fixes the two that differ from
#: the rest: an auth failure *fails* the run (the deployment is misconfigured and no
#: amount of human triage on this run will help), everything else *escalates* it (the run
#: is sound, a person needs to look at it).
_OUTCOME_FOR_ERROR_KIND: Final[Mapping[str, _StageOutcome]] = {
    "invalid_output": ("escalated", "invalid_output"),
    "llm_timeout": ("escalated", "llm_timeout"),
    "llm_rate_limited": ("escalated", "rate_limited"),
    "llm_auth": ("failed", "config_error"),
    # Appendix B.1's last row asks for this reason by name. It is its own member rather
    # than a synonym for `tool_failure` because "the model was unreachable" and "a tool
    # call failed" want different responses from whoever or whatever reads the record:
    # the first is an upstream outage to wait out, the second points at the run's own
    # inputs or at a broken integration.
    "llm_upstream": ("escalated", "llm_upstream"),
    "tool_error": ("escalated", "tool_failure"),
    "internal": ("failed", "config_error"),
}

#: The catch-all for an error kind the table above does not name: the run is sound and a
#: person needs to look at it, and the failure is attributed no more precisely than "a step
#: of the run did not complete".
_DEFAULT_STAGE_OUTCOME: Final[_StageOutcome] = ("escalated", "tool_failure")


def new_run_id() -> RunId:
    """A ULID-shaped run id: 48 bits of millisecond timestamp, 80 bits of randomness.

    Lexicographically sortable by creation time, which is the whole reason for the shape
    -- ``ORDER BY run_id`` is ``ORDER BY created_at`` without a second column.
    """
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        value, remainder = divmod(value, 32)
        chars.append(_CROCKFORD[remainder])
    return "run_" + "".join(reversed(chars))


class StageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    name: str
    agent_key: str
    output_model: type[BaseModel]          # not serialized
    required: bool = True
    gate: Callable[[RunState], GateDecision] | None = None


class GateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proceed: bool
    reason: str
    escalate_as: str | None = None


class RunState(BaseModel):                 # mutable, extra="allow" -- the only non-frozen model
    model_config = ConfigDict(extra="allow", frozen=False, arbitrary_types_allowed=True)

    run_id: RunId
    request: RunRequest
    artifacts: dict[str, BaseModel]        # stage outputs, keyed by an integration-chosen name
    degraded: list[str]
    stages: list[StageRecord]


class Orchestrator:
    """Runs the configured stages for one request and returns its outcome."""

    def __init__(
        self,
        *,
        stages: Sequence[StageSpec],
        agents: Mapping[str, Agent[Any]],
        recorder: TraceRecorder,
        artifact_keys: Mapping[str, str] | None = None,
        trace_url_template: str = "/v1/runs/{run_id}/trace",
        escalation_channels: Sequence[Literal["log", "db", "webhook"]] = ("log",),
        run_id_factory: Callable[[], RunId] = new_run_id,
    ) -> None:
        """Bind the driver to its stage list and the agents behind it.

        ``agents`` is a plain mapping from ``StageSpec.agent_key``, resolved once here.
        No registry, no discovery, no import-by-name: PLAN.md's "the harness is a
        library, not a framework" reduces, in this class, to the rule that the only way
        an agent gets run is that someone passed it in.

        ``artifact_keys`` maps a stage name to the key its output is filed under in
        ``RunState.artifacts``, defaulting to the stage name. Appendix A.2 names stages
        ``investigate``/``diagnose`` while indexing artifacts as ``bundle``/``diagnosis``,
        so the two vocabularies are genuinely distinct -- and ``StageSpec`` is frozen and
        ``extra="forbid"``, so the mapping belongs at the composition root rather than as
        a new field on the contract.

        A ``required`` stage with no registered agent is a wiring fault and raises here,
        at startup. A non-required one is skipped and recorded as skipped, which is how a
        stage whose agent arrives in a later phase stays declared -- gate and all --
        without pretending to have run.
        """
        missing = [
            stage.agent_key
            for stage in stages
            if stage.required and stage.agent_key not in agents
        ]
        if missing:
            raise ConfigurationError(
                "no agent registered for required stage key(s): " + ", ".join(sorted(missing))
            )
        self.stages = tuple(stages)
        self.agents = dict(agents)
        self.recorder = recorder
        self.artifact_keys = dict(artifact_keys or {})
        self.trace_url_template = trace_url_template
        self.escalation_channels = list(escalation_channels)
        self.run_id_factory = run_id_factory

    def _artifact_key(self, stage: StageSpec) -> str:
        return self.artifact_keys.get(stage.name, stage.name)

    def _escalate(
        self,
        *,
        run_id: RunId,
        reason: EscalationReason,
        message: str,
        payload: dict[str, JsonValue],
    ) -> EscalationRecord:
        """Build the escalation record and deliver it on the configured channels.

        Phase 1 delivers to the log only. Appendix B.4 is explicit that a delivery
        failure never fails the run -- the record is the durable artifact, the webhook is
        a convenience -- so the outbound channels arriving in a later phase changes what
        is *notified*, never what is *returned*.
        """
        logger.warning("run %s escalated (%s): %s", run_id, reason, message)
        return EscalationRecord(
            escalation_id="esc_" + secrets.token_hex(8),
            reason=reason,
            message=message,
            payload=payload,
            channels=list(self.escalation_channels),
            delivered_at=datetime.now(UTC),
        )

    async def run(self, request: RunRequest) -> RunOutcome:
        """Drive every stage for one request and return its outcome.

        The loop never raises for a stage failure: a failed required stage ends the run
        with a status and an escalation record, which is the same shape a successful run
        returns and therefore the same shape the API serves.
        """
        run_id = self.run_id_factory()
        created_at = datetime.now(UTC)
        started = time.monotonic()

        state = RunState(
            run_id=run_id, request=request, artifacts={}, degraded=[], stages=[]
        )
        status: Literal["completed", "escalated", "failed"] = "completed"
        escalation: EscalationRecord | None = None

        # `run_scope` binds this recorder AND publishes `run_id` as the ambient run,
        # so the agents' own recorders -- built once at the composition root, long
        # before any run existed -- write their spans into this trace instead of
        # dropping them for having no run to belong to.
        with self.recorder.run_scope(run_id) as recorder:
            async with recorder.span(
                "run", "orchestrator",
                integration=request.integration,
                mode=request.mode,
                requested_by=request.requested_by,
            ) as run_span:
                run_span.set_attribute("run_id", run_id)

                for stage in self.stages:
                    if stage.gate is not None:
                        decision = stage.gate(state)
                        if not decision.proceed:
                            state.stages.append(
                                StageRecord(
                                    stage=stage.name, agent=None, status="gated",
                                    started_at=datetime.now(UTC), duration_ms=0, attempts=0,
                                    tokens=TokenUsage(), summary=decision.reason,
                                )
                            )
                            if decision.escalate_as is not None:
                                reason = decision.escalate_as
                                if reason not in _ESCALATION_REASONS:
                                    # An unrecognised reason is a wiring mistake in the
                                    # integration's gate, not a reason to lose the escalation.
                                    logger.warning(
                                        "gate on stage %r escalated as unknown reason %r",
                                        stage.name, reason,
                                    )
                                    reason = "unknown_category"
                                status = "escalated"
                                escalation = self._escalate(
                                    run_id=run_id,
                                    reason=reason,  # type: ignore[arg-type]
                                    message=decision.reason,
                                    payload={"stage": stage.name, "gate": decision.reason},
                                )
                            break

                    agent = self.agents.get(stage.agent_key)
                    if agent is None:
                        # Only reachable for a non-required stage: `__init__` refused to build
                        # an orchestrator whose required stages had no agent.
                        state.stages.append(
                            StageRecord(
                                stage=stage.name, agent=None, status="skipped",
                                started_at=datetime.now(UTC), duration_ms=0, attempts=0,
                                tokens=TokenUsage(),
                                summary=f"no agent registered for {stage.agent_key!r}",
                            )
                        )
                        continue

                    stage_started_at = datetime.now(UTC)
                    stage_started = time.monotonic()
                    result = await agent.run(state)
                    state.stages.append(
                        StageRecord(
                            stage=stage.name,
                            agent=result.agent,
                            status=result.status,
                            started_at=stage_started_at,
                            duration_ms=int((time.monotonic() - stage_started) * 1000),
                            attempts=result.attempts,
                            tokens=result.tokens,
                            summary=(
                                result.error.message
                                if result.error is not None
                                else f"{stage.name} produced {stage.output_model.__name__}"
                            ),
                        )
                    )

                    if result.status == "ok" and result.output is not None:
                        state.artifacts[self._artifact_key(stage)] = result.output
                        continue

                    if not stage.required:
                        continue

                    kind = result.error.kind if result.error is not None else "internal"
                    # Unpacked via its own binding rather than straight into `status, reason`:
                    # `reason` is already bound to a plain `str` in the gate branch above, and
                    # assigning into it here makes that wider type the expected type of the
                    # lookup, which drags `reason` back to `str` and drops the
                    # `EscalationReason` check on the argument below.
                    outcome = _OUTCOME_FOR_ERROR_KIND.get(kind, _DEFAULT_STAGE_OUTCOME)
                    status = outcome[0]
                    escalation = self._escalate(
                        run_id=run_id,
                        reason=outcome[1],
                        message=(
                            result.error.message if result.error is not None
                            else f"stage {stage.name!r} produced no output"
                        ),
                        payload={"stage": stage.name, "agent": result.agent, "kind": kind},
                    )
                    break

                run_span.set_attribute("status", status)
                if state.degraded:
                    # `ATTR_DEGRADED_COMPONENT`, not a second spelling of the same idea:
                    # the trace read path aggregates *this* key, and `RunOutcome`
                    # aggregates `state.degraded`. Written under any other name, the two
                    # read paths answer differently for the same run -- one from the list
                    # below, one from a key nothing writes. This span is also the only
                    # place the run-level list is complete: a stage may append to
                    # `state.degraded` after its own `agent.run` span has closed, so the
                    # per-stage writes are a subset, not a substitute.
                    run_span.set_attribute(ATTR_DEGRADED_COMPONENT, list(state.degraded))

        completed_at = datetime.now(UTC)
        return RunOutcome(
            run_id=run_id,
            integration=request.integration,
            status=status,
            created_at=created_at,
            completed_at=completed_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            stages=state.stages,
            degraded_components=list(state.degraded),
            total_tokens=TokenUsage(
                prompt=sum(record.tokens.prompt for record in state.stages),
                completion=sum(record.tokens.completion for record in state.stages),
                thinking=sum(record.tokens.thinking for record in state.stages),
                total=sum(record.tokens.total for record in state.stages),
                estimated_cost_usd=sum(
                    record.tokens.estimated_cost_usd for record in state.stages
                ),
            ),
            final={
                key: artifact.model_dump(mode="json")
                for key, artifact in state.artifacts.items()
            },
            escalation=escalation,
            trace_url=self.trace_url_template.format(run_id=run_id),
        )


StageSpec.model_rebuild()
