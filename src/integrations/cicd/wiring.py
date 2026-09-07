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

import yaml

from src.harness.agent import Agent
from src.harness.confidence import DEFAULT_ADJUSTMENT_DELTAS, ConfidenceModel
from src.harness.context_manager import ContextManager
from src.harness.gateway import ToolGateway
from src.harness.llm import LlmClient
from src.harness.observability import TraceRecorder
from src.harness.orchestrator import GateDecision, Orchestrator, RunState, StageSpec
from src.harness.recovery import RetryPolicy
from src.integrations.cicd.agents.diagnostician import (
    EMPTY_DIFF_CONTRADICTION,
    EMPTY_DIFF_CONTRADICTION_DELTA,
    Diagnostician,
)
from src.integrations.cicd.agents.investigator import Investigator
from src.integrations.cicd.schemas import Diagnosis, FailureBundle, RemediationPlan

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


def load_forbidden(path: Path = POLICY_PATH) -> tuple[str, ...]:
    """The `forbidden` list out of `policy.yaml`, for the gateway's own re-check.

    Not the policy loader — that arrives with the engine in the next phase. This reads
    exactly one key, so that the gateway's hardcoded refusal list and the policy file
    cannot drift apart in the meantime.
    """
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    forbidden = spec.get("forbidden", []) if isinstance(spec, dict) else []
    return tuple(str(name) for name in forbidden)


def make_remediation_gate(escalation_threshold: float):  # noqa: ANN201 - closure type is the contract
    """Appendix A.2's confidence short-circuit, closed over the configured threshold.

    Attached to the remediate stage because that is the stage it guards: the question
    "is this diagnosis good enough to act on" belongs at the moment something would act.
    In this phase no remediating agent is registered, so the stage runs its gate and is
    then skipped — the short-circuit is live, the action it guards simply does not exist
    yet.
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
    """The Phase 1 pipeline: investigate, diagnose, and the guarded remediate stage."""
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
            output_model=RemediationPlan,
            # Not required: no remediating agent exists in this phase, so the stage is
            # recorded as skipped rather than failing the run. The gate above still runs.
            required=False,
            gate=make_remediation_gate(escalation_threshold),
        ),
    ]


def build_agents(
    *,
    gateway: ToolGateway,
    context_manager: ContextManager,
    llm: LlmClient,
    recorder: TraceRecorder,
    investigator_model: str,
    diagnostician_model: str,
    confidence_model: ConfidenceModel | None = None,
    retry_policy: RetryPolicy | None = None,
    timeout_s: float | None = None,
) -> dict[str, Agent[Any]]:
    """The two agents this phase runs, keyed by `StageSpec.agent_key`."""
    return {
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
    escalation_threshold: float,
    investigator_model: str,
    diagnostician_model: str,
    confidence_model: ConfidenceModel | None = None,
    retry_policy: RetryPolicy | None = None,
    timeout_s: float | None = None,
    escalation_channels: Sequence[str] = ("log",),
) -> Orchestrator:
    """Assemble the CI/CD orchestrator from primitives the caller already built."""
    return Orchestrator(
        stages=build_stages(escalation_threshold=escalation_threshold),
        agents=build_agents(
            gateway=gateway,
            context_manager=context_manager,
            llm=llm,
            recorder=recorder,
            investigator_model=investigator_model,
            diagnostician_model=diagnostician_model,
            confidence_model=confidence_model,
            retry_policy=retry_policy,
            timeout_s=timeout_s,
        ),
        recorder=recorder,
        artifact_keys=ARTIFACT_KEYS,
        escalation_channels=list(escalation_channels),  # type: ignore[arg-type]
    )
