"""The evaluate stage: every citation in the diagnosis, checked against the bundle.

No model call. This agent is a plain `Agent` (not an `LLMAgent`): it turns the
Diagnostician's `Citation`s into `Claim`s, runs the harness `Evaluator` over the
integration's checkers (`claim_checkers.py`), and returns the `EvaluationReport` that
becomes `final.evaluation`. Deterministic, free, reproducible, and explainable to a
reviewer -- PLAN.md's reason for not using a second model to check the first.

Two things happen here that a reader of the other agents would not expect, both decided
in `docs/progress/phase-4/dispatch.md`:

1. **The diagnosis is re-filed with the evaluator's adjustment.** PLAN.md's confidence
   table has two Evaluator rows (`evidence_fully_verified` +0.05, `evidence_refuted`
   -0.15) and `final_confidence` is one sum clamped once, so this stage re-calibrates
   from `self_confidence` with the Diagnostician's signals plus its own and replaces
   `state.artifacts["diagnosis"]`. The remediation gate, the policy facts and the memory
   write then all read one number.
2. **Memory is written here, not in the Diagnostician** (dispatch decision 1). A verdict
   the evaluator refutes must not be tallied at its pre-penalty confidence; this is the
   first point at which the confidence is final, so this is where the sighting is counted
   and the observation recorded. `verdict_threshold` is the remediation gate's number: a
   verdict below it is remembered as a sighting, not as a verdict (Phase 3 audit
   finding 4), and the comparison is made on the post-penalty figure.
"""

from __future__ import annotations

import logging
import time
from typing import Final

from src.harness.confidence import ConfidenceModel, calibrate
from src.harness.contracts import AgentResult, TokenUsage
from src.harness.evaluator import (
    SIGNAL_FULLY_VERIFIED,
    SIGNAL_REFUTED,
    EvaluationReport,
    Evaluator,
)
from src.harness.memory import MemoryStore
from src.harness.observability import TraceRecorder
from src.harness.orchestrator import RunState
from src.integrations.cicd.claim_checkers import (
    AVAILABILITY_KEY,
    BUNDLE_KEY,
    DIAGNOSIS_KEY,
    availability_of,
    claims_from_citations,
)
from src.integrations.cicd.history import DEGRADED_MEMORY, observation_for
from src.integrations.cicd.schemas import Diagnosis, FailureBundle

logger = logging.getLogger("harness.integrations.cicd.evaluator")

#: `StageSpec.agent_key` and `AgentResult.agent` for this stage.
EVALUATOR_KEY: Final[str] = "evaluator"


class EvidenceEvaluator:
    """Checks the diagnosis's citations, re-calibrates it, and remembers the verdict."""

    key = EVALUATOR_KEY

    def __init__(
        self,
        *,
        evaluator: Evaluator,
        recorder: TraceRecorder,
        confidence_model: ConfidenceModel,
        memory: MemoryStore | None = None,
        verdict_threshold: float | None = None,
    ) -> None:
        self.evaluator = evaluator
        self.recorder = recorder
        self.confidence_model = confidence_model
        self.memory = memory
        self.verdict_threshold = verdict_threshold

    # -- artifacts ----------------------------------------------------------------
    def _bundle(self, state: RunState) -> FailureBundle:
        bundle = state.artifacts.get(BUNDLE_KEY)
        if not isinstance(bundle, FailureBundle):
            raise TypeError(f"evaluator expected a FailureBundle at artifacts[{BUNDLE_KEY!r}]")
        return bundle

    def _diagnosis(self, state: RunState) -> Diagnosis:
        diagnosis = state.artifacts.get(DIAGNOSIS_KEY)
        if not isinstance(diagnosis, Diagnosis):
            raise TypeError(f"evaluator expected a Diagnosis at artifacts[{DIAGNOSIS_KEY!r}]")
        return diagnosis

    # -- the evaluator's own confidence row ---------------------------------------
    @staticmethod
    def signal(report: EvaluationReport) -> tuple[str, str] | None:
        """Which of PLAN.md's two Evaluator rows fired, and why. `None` for neither.

        `evidence_refuted` on any refuted claim; `evidence_fully_verified` when every
        claim verified -- which is `pass`, and never `skipped`: a diagnosis with no
        citations has nothing fully verified, and `no_citations` already prices it.
        """
        if report.refuted:
            return SIGNAL_REFUTED, report.reason
        if report.verdict == "pass":
            return SIGNAL_FULLY_VERIFIED, report.reason
        return None

    def recalibrate(self, diagnosis: Diagnosis, report: EvaluationReport) -> Diagnosis:
        """The diagnosis with the evaluator's row added: one sum, one clamp.

        The Diagnostician's signals are rebuilt from the adjustments it recorded (name ->
        reason), so `calibrate` sees the complete list and clamps the total exactly once
        -- appending a delta to an already-clamped figure is the wrong arithmetic
        (`confidence.calibrate`'s worked example).
        """
        signals = {
            adjustment.name: adjustment.reason
            for adjustment in diagnosis.confidence_adjustments
        }
        fired = self.signal(report)
        if fired is not None:
            signals[fired[0]] = fired[1]
        final_confidence, adjustments = calibrate(
            diagnosis.self_confidence, signals, self.confidence_model
        )
        return diagnosis.model_copy(
            update={
                "final_confidence": final_confidence,
                "confidence_adjustments": adjustments,
            }
        )

    # -- memory (moved here from the Diagnostician; dispatch decision 1) ------------
    def _counts_as_verdict(self, diagnosis: Diagnosis) -> bool:
        """Whether the signature's `verdict_counts` should count this verdict.

        The observation row always carries the verdict and its final confidence; this
        decides only the signature's tally, which `dominant_verdict` and the
        `memory_agreement` bonus read. Three sightings the gate refused must not add up
        to a prior that lifts a fourth over the same gate (Phase 3 audit finding 4).
        """
        return (
            self.verdict_threshold is None
            or diagnosis.final_confidence >= self.verdict_threshold
        )

    async def _remember(self, diagnosis: Diagnosis, state: RunState) -> None:
        """Count this sighting and record its verdict (Phase 3 dispatch decision 9, as
        amended by Phase 4 decision 1).

        Every evaluated run is remembered, gated or not: the sighting always counts, the
        observation always carries the final confidence, and the verdict joins the
        signature's tally only when it clears `verdict_threshold`. Any failure degrades
        the run's `memory` component and leaves the diagnosis untouched: memory is a
        soft dependency on the write side exactly as on the read side.
        """
        if self.memory is None:
            return
        bundle = self._bundle(state)
        key = bundle.prior_history.key
        if key is None:
            return
        verdict = diagnosis.category if self._counts_as_verdict(diagnosis) else None
        try:
            await self.memory.upsert_signature(key, verdict, state.run_id)
            await self.memory.record_observation(
                observation_for(
                    key=key, run_id=state.run_id, diagnosis=diagnosis, job=bundle.job
                )
            )
        except Exception:  # noqa: BLE001 - a dead cache must never take down triage
            logger.warning("evaluator: could not record the verdict in memory", exc_info=True)
            if DEGRADED_MEMORY not in state.degraded:
                state.degraded.append(DEGRADED_MEMORY)

    # -- the stage ----------------------------------------------------------------
    async def run(self, state: RunState) -> AgentResult[EvaluationReport]:
        started = time.monotonic()
        bundle = self._bundle(state)
        diagnosis = self._diagnosis(state)
        claims = claims_from_citations(diagnosis.citations)
        artifacts = {
            BUNDLE_KEY: bundle,
            DIAGNOSIS_KEY: diagnosis,
            AVAILABILITY_KEY: availability_of(bundle, state.degraded),
        }

        async with self.recorder.span("agent.run", "agent", agent=self.key) as span:
            report = self.evaluator.evaluate(claims, artifacts)
            # One span per claim, like `policy.decide`: the trace shows each verdict and
            # why, so a refuted citation is readable without opening the artifact.
            for claim, verdict in zip(claims, report.verdicts, strict=True):
                async with self.recorder.span(
                    "evaluation.claim",
                    "evaluator",
                    claim_id=claim.claim_id,
                    kind=claim.kind,
                    locator=claim.payload.get("locator"),
                    result=verdict.result,
                    detail=verdict.detail,
                    matched_locator=verdict.matched_locator,
                ):
                    pass
            span.set_attribute("verdict", report.verdict)
            span.set_attribute("verified", report.verified)
            span.set_attribute("refuted", report.refuted)
            span.set_attribute("unverifiable", report.unverifiable)
            span.set_attribute("confidence_delta", report.confidence_delta)

            recalibrated = self.recalibrate(diagnosis, report)
            state.artifacts[DIAGNOSIS_KEY] = recalibrated
            span.set_attribute("final_confidence", recalibrated.final_confidence)
            span.set_attribute("status", "ok")

            await self._remember(recalibrated, state)

        return AgentResult[EvaluationReport](
            agent=self.key,
            status="ok",
            output=report,
            confidence=recalibrated.final_confidence,
            evidence=[],
            attempts=1,
            latency_ms=int((time.monotonic() - started) * 1000),
            tokens=TokenUsage(),
            prompt_sha256=None,
            model=None,
            error=None,
        )
