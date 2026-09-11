"""Composition: the CI/CD integration's contribution to building an Orchestrator.

This is where the domain is assembled out of harness primitives — stage list, gate,
confidence table, agents. It stops short of constructing the harness's own singletons
(the recorder, the LLM client, the settings snapshot); those belong to the application's
composition root in `src/api/deps.py`, which calls the factories here.

The split matters: everything in this module is domain knowledge that the harness must
not contain, and nothing in it reads configuration or the environment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from src.harness.agent import Agent
from src.harness.confidence import DEFAULT_ADJUSTMENT_DELTAS, ConfidenceModel
from src.harness.context_manager import ContextManager
from src.harness.gateway import ToolGateway
from src.harness.guardrails import PolicyEngine, PolicySpec, load_policy
from src.harness.llm import LlmClient
from src.harness.observability import TraceRecorder
from src.harness.orchestrator import (
    GateDecision,
    Orchestrator,
    RunState,
    StageSpec,
    Suspension,
)
from src.harness.recovery import RetryPolicy
from src.integrations.cicd.agents.diagnostician import (
    EMPTY_DIFF_CONTRADICTION,
    EMPTY_DIFF_CONTRADICTION_DELTA,
    Diagnostician,
)
from src.integrations.cicd.agents.investigator import Investigator
from src.integrations.cicd.agents.remediator import DEFAULT_APPROVAL_TTL_H, Remediator
from src.integrations.cicd.remediation import (
    denial_summary,
    failed_execution,
    failure_summary,
)
from src.integrations.cicd.schemas import Diagnosis, FailureBundle, RemediationResult

INTEGRATION: Final[str] = "cicd"

POLICY_PATH: Final[Path] = Path(__file__).with_name("policy.yaml")

#: Stage name -> the key its output is filed under in `RunState.artifacts`. Appendix A.2
#: uses both vocabularies — stages are verbs, artifacts are nouns — so the mapping is
#: explicit rather than assumed equal.
ARTIFACT_KEYS: Final[Mapping[str, str]] = {
    "investigate": "bundle",
    "diagnose": "diagnosis",
    "remediate": "remediation",
}

DIAGNOSIS_KEY: Final[str] = "diagnosis"
EVALUATION_KEY: Final[str] = "evaluation"
REMEDIATION_KEY: Final[str] = "remediation"


def build_confidence_model() -> ConfidenceModel:
    """The harness's delta table plus this integration's own row.

    `empty_diff_contradiction` is deliberately not in the harness: its condition reads
    `DiffSummary.files`, a type the harness has never heard of. Registering it here is
    what keeps PLAN.md's adjustment table complete without pushing a domain fact into
    `harness/confidence.py`.
    """
    return ConfidenceModel(
        deltas={
            **DEFAULT_ADJUSTMENT_DELTAS,
            EMPTY_DIFF_CONTRADICTION: EMPTY_DIFF_CONTRADICTION_DELTA,
        }
    )


def load_policy_spec(path: Path = POLICY_PATH) -> PolicySpec:
    """This integration's `policy.yaml`, validated. Raises on anything malformed."""
    return load_policy(path)


def load_forbidden(path: Path = POLICY_PATH) -> tuple[str, ...]:
    """The `forbidden` list out of `policy.yaml`, for the gateway's own re-check.

    Read through the same loader the engine uses, so the gateway's refusal list and the
    rules the engine enforces come from one validated parse of one file.
    """
    return tuple(load_policy_spec(path).forbidden)


def remediation_suspend(state: RunState) -> Suspension | None:
    """The post-stage hook on the remediate stage (A.2 amendment, Phase 2).

    A `RemediationResult` that awaits approval suspends the run with that status; one the
    policy denied escalates it as `policy_denied` with the first denying decision quoted,
    so `GET /v1/escalations` reads as a sentence; one whose execution failed escalates as
    `tool_failure` naming the call (Appendix B.2). Anything else lets the run complete.
    """
    result = state.artifacts.get(REMEDIATION_KEY)
    if not isinstance(result, RemediationResult):
        return None
    if result.status == "awaiting_approval" and result.pending_approval is not None:
        return Suspension(
            status="awaiting_approval",
            reason=(
                f"plan {result.plan.action!r} requires approval "
                f"({result.pending_approval.approval_id})"
            ),
        )
    if result.status == "denied":
        return Suspension(
            status="escalated",
            reason=denial_summary(result.decisions),
            escalate_as="policy_denied",
            payload={
                "action": result.plan.action,
                "decisions": [
                    {"tool": d.tool, "rule_id": d.rule_id, "effect": d.effect}
                    for d in result.decisions
                ],
            },
        )
    failed = failed_execution(result)
    if failed is not None:
        return Suspension(
            status="escalated",
            reason=failure_summary(failed),
            escalate_as="tool_failure",
            payload={
                "action": result.plan.action,
                "tool": failed.tool,
                "error_kind": failed.error.kind if failed.error is not None else None,
                "executed": len(result.executed),
            },
        )
    return None


def make_remediation_gate(escalation_threshold: float):  # noqa: ANN201 - closure type is the contract
    """Appendix A.2's confidence short-circuit, closed over the configured threshold.

    Attached to the remediate stage because that is the stage it guards: the question
    "is this diagnosis good enough to act on" belongs at the moment something would act.
    """

    def remediation_gate(state: RunState) -> GateDecision:
        diagnosis = state.artifacts.get(DIAGNOSIS_KEY)
        if not isinstance(diagnosis, Diagnosis):
            return GateDecision(
                proceed=False,
                reason="no diagnosis was produced",
                escalate_as="invalid_output",
            )

        evaluation = state.artifacts.get(EVALUATION_KEY)
        # PLAN.md: "a refuted claim overrides confidence entirely". Checked first, and
        # with no config override, so grounding beats self-belief.
        if evaluation is not None and getattr(evaluation, "verdict", None) == "fail":
            return GateDecision(
                proceed=False,
                reason="evidence refuted by evaluator",
                escalate_as="evidence_refuted",
            )
        if diagnosis.final_confidence < escalation_threshold:
            return GateDecision(
                proceed=False,
                reason=(
                    f"confidence {diagnosis.final_confidence:.2f} < {escalation_threshold}"
                ),
                escalate_as="low_confidence",
            )
        if diagnosis.category == "unknown":
            return GateDecision(
                proceed=False, reason="unclassified", escalate_as="unknown_category"
            )
        return GateDecision(proceed=True, reason="ok")

    return remediation_gate


def build_stages(*, escalation_threshold: float) -> list[StageSpec]:
    """The pipeline: investigate, diagnose, and the gated-and-suspendable remediate stage."""
    return [
        StageSpec(
            name="investigate",
            agent_key="investigator",
            output_model=FailureBundle,
        ),
        StageSpec(
            name="diagnose",
            agent_key="diagnostician",
            output_model=Diagnosis,
        ),
        StageSpec(
            name="remediate",
            agent_key="remediator",
            output_model=RemediationResult,
            gate=make_remediation_gate(escalation_threshold),
            suspend=remediation_suspend,
        ),
    ]


def build_agents(
    *,
    gateway: ToolGateway,
    context_manager: ContextManager,
    llm: LlmClient,
    recorder: TraceRecorder,
    engine: PolicyEngine,
    investigator_model: str,
    diagnostician_model: str,
    remediator_model: str,
    confidence_model: ConfidenceModel | None = None,
    retry_policy: RetryPolicy | None = None,
    timeout_s: float | None = None,
    approval_ttl_h: int = DEFAULT_APPROVAL_TTL_H,
) -> dict[str, Agent[Any]]:
    """The three agents, keyed by `StageSpec.agent_key`."""
    return {
        "remediator": Remediator(
            llm=llm,
            model=remediator_model,
            recorder=recorder,
            gateway=gateway,
            engine=engine,
            context_manager=context_manager,
            approval_ttl_h=approval_ttl_h,
            retry_policy=retry_policy,
            timeout_s=timeout_s,
        ),
        "investigator": Investigator(
            gateway=gateway,
            context_manager=context_manager,
            llm=llm,
            model=investigator_model,
            recorder=recorder,
            retry_policy=retry_policy,
            timeout_s=timeout_s,
        ),
        "diagnostician": Diagnostician(
            llm=llm,
            model=diagnostician_model,
            recorder=recorder,
            context_manager=context_manager,
            confidence_model=confidence_model or build_confidence_model(),
            retry_policy=retry_policy,
            timeout_s=timeout_s,
        ),
    }


def build_orchestrator(
    *,
    gateway: ToolGateway,
    context_manager: ContextManager,
    llm: LlmClient,
    recorder: TraceRecorder,
    engine: PolicyEngine,
    escalation_threshold: float,
    investigator_model: str,
    diagnostician_model: str,
    remediator_model: str,
    confidence_model: ConfidenceModel | None = None,
    retry_policy: RetryPolicy | None = None,
    timeout_s: float | None = None,
    approval_ttl_h: int = DEFAULT_APPROVAL_TTL_H,
    escalation_channels: Sequence[str] = ("log",),
) -> Orchestrator:
    """Assemble the CI/CD orchestrator from primitives the caller already built.

    `engine` is passed in rather than built here for the same reason the recorder and the
    client are: the composition root builds it once at startup (so a malformed policy
    fails the boot, not the first request), and `readyz` reports on that same object.
    """
    return Orchestrator(
        stages=build_stages(escalation_threshold=escalation_threshold),
        agents=build_agents(
            gateway=gateway,
            context_manager=context_manager,
            llm=llm,
            recorder=recorder,
            engine=engine,
            investigator_model=investigator_model,
            diagnostician_model=diagnostician_model,
            remediator_model=remediator_model,
            confidence_model=confidence_model,
            retry_policy=retry_policy,
            timeout_s=timeout_s,
            approval_ttl_h=approval_ttl_h,
        ),
        recorder=recorder,
        artifact_keys=ARTIFACT_KEYS,
        escalation_channels=list(escalation_channels),  # type: ignore[arg-type]
    )
