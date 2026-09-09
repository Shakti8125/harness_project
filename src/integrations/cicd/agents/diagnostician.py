"""The Diagnostician: one model call over the completed failure bundle.

Produces a `Diagnosis`, then writes the two fields Appendix A.11 marks "added by the
harness after the model returns, not requested from the model": `final_confidence` and
`confidence_adjustments`. The model reports how sure it is on the prompt's rubric and
never sees the adjusted number -- PLAN.md is explicit that calibration is the harness's
business, not the model's.

Which signals fired is a domain reading of the bundle, which is why it happens here and
not in `harness/confidence.py`: only this layer knows that an empty diff contradicts a
`real_regression` verdict, or that a failed log fetch is what "a required read tool
returned an error" means for this integration.
"""

from __future__ import annotations

from typing import Final

from src.harness.agent import AgentPrompt, LLMAgent
from src.harness.confidence import Adjustment, ConfidenceModel, calibrate
from src.harness.context_manager import (
    ContextBudget,
    ContextManager,
    ContextRequest,
    Section,
)
from src.harness.contracts import AgentResult, Evidence
from src.harness.llm import LlmClient
from src.harness.observability import TraceRecorder
from src.harness.orchestrator import RunState
from src.harness.recovery import RetryPolicy
from src.integrations.cicd.agents.investigator import ANCHOR_PATTERNS
from src.integrations.cicd.rendering import (
    load_prompt_template,
    make_evidence,
    render_dependencies,
    render_diagnostician_prompt,
    render_diff_patches,
    render_diff_summary,
    render_investigation_summary,
    render_job,
    render_truncation,
)
from src.integrations.cicd.schemas import Diagnosis, FailureBundle

#: The bundle key the Investigator's output is filed under in `RunState.artifacts`.
BUNDLE_KEY: Final[str] = "bundle"

#: PLAN.md's adjustment table, seventh row. Deliberately NOT in the harness: its
#: condition names `DiffSummary.files`, a type this integration owns. Registered into
#: `ConfidenceModel.deltas` at the composition root; if it is ever missing, `calibrate`
#: still emits the row carrying `UNREGISTERED_SIGNAL_REASON` so the omission is visible
#: in the trace rather than silently worth nothing.
EMPTY_DIFF_CONTRADICTION: Final[str] = "empty_diff_contradiction"
EMPTY_DIFF_CONTRADICTION_DELTA: Final[float] = -0.10

_LOG_PRIORITY: Final[int] = 10
_DIFF_PRIORITY: Final[int] = 7


class Diagnostician(LLMAgent[Diagnosis]):
    """Turns a failure bundle into a cited, calibrated diagnosis."""

    key = "diagnostician"

    def __init__(
        self,
        *,
        llm: LlmClient,
        model: str,
        recorder: TraceRecorder,
        context_manager: ContextManager,
        confidence_model: ConfidenceModel,
        budget: ContextBudget | None = None,
        retry_policy: RetryPolicy | None = None,
        anchor_patterns: tuple[str, ...] = ANCHOR_PATTERNS,
        timeout_s: float | None = None,
    ) -> None:
        super().__init__(
            key="diagnostician",
            output_model=Diagnosis,
            llm=llm,
            model=model,
            recorder=recorder,
            retry_policy=retry_policy,
            **({"timeout_s": timeout_s} if timeout_s is not None else {}),
        )
        self.context_manager = context_manager
        self.confidence_model = confidence_model
        self.budget = budget if budget is not None else context_manager.default_budget
        self.anchor_patterns = anchor_patterns

    def _bundle(self, state: RunState) -> FailureBundle:
        bundle = state.artifacts.get(BUNDLE_KEY)
        if not isinstance(bundle, FailureBundle):
            raise TypeError(
                f"diagnostician expected a FailureBundle at artifacts[{BUNDLE_KEY!r}]"
            )
        return bundle

    async def build_prompt(self, state: RunState) -> AgentPrompt:
        bundle = self._bundle(state)

        # Re-budgeted rather than concatenated: the log excerpt was budgeted on its own,
        # and adding the diff to it can exceed the total. Running the same assembler over
        # both sections is the only way the pair is guaranteed to fit, and it keeps the
        # anchor guarantee -- the error lines survive this pass too.
        log_text = "\n".join(excerpt.excerpt for excerpt in bundle.logs)
        sections = [Section(key="log", content=log_text, priority=_LOG_PRIORITY)]
        diff_text = render_diff_patches(bundle.diff)
        if diff_text:
            sections.append(Section(key="diff", content=diff_text, priority=_DIFF_PRIORITY))

        assembled = self.context_manager.assemble(
            ContextRequest(
                sections=sections,
                budget=self.budget,
                anchor_patterns=list(self.anchor_patterns),
            )
        )

        evidence: list[Evidence] = []
        if log_text:
            evidence.append(
                make_evidence(
                    "log", f"log:job/{bundle.job.job_id}", assembled.per_section["log"]
                )
            )
        if diff_text:
            evidence.append(make_evidence("diff", f"diff:{bundle.diff.head_sha}", diff_text))

        prompt_text = render_diagnostician_prompt(
            job_summary=render_job(bundle.job),
            diff_summary=render_diff_summary(bundle.diff),
            dependency_summary=render_dependencies(bundle.dependency_changes),
            prior_history_summary=(
                "No prior history is available: the memory store is not wired up in "
                "this phase. Do not treat the absence of history as evidence of "
                "either flakiness or novelty."
            ),
            investigation_summary=render_investigation_summary(bundle.notes),
            context_bundle=assembled.text,
            truncation_note=render_truncation(assembled.truncation),
        )
        # See `Investigator.build_prompt` for why this is a nested span rather than an
        # `AgentPrompt` field: it is the only place the template's `version:`
        # front-matter can reach the trace without a change under `src/harness/`.
        async with self.recorder.span(
            "prompt.render",
            "agent",
            agent=self.key,
            prompt_version=load_prompt_template("diagnostician").version,
        ):
            pass

        return AgentPrompt(text=prompt_text, evidence=evidence)

    def signals(self, output: Diagnosis, bundle: FailureBundle) -> dict[str, str]:
        """Which rows of PLAN.md's adjustment table fired, and why.

        Only the rows this phase can actually observe. `memory_agreement` needs the
        memory store (Phase 3) and the two evidence rows need the Evaluator (Phase 4);
        signalling them from here on a guess would put a number in the trace that nothing
        verified.
        """
        fired: dict[str, str] = {}
        if not output.citations:
            fired["no_citations"] = "the model cited no evidence"
        if bundle.cold_start:
            fired["cold_start"] = "no green baseline run existed to compare against"
        # `bundle.gateway_errors` is populated ONLY from the Investigator's required
        # deterministic-collection calls (see `schemas.FailureBundle.gateway_errors` and
        # `Investigator.run`) -- optional additional-tool-call errors and synthesised
        # policy refusals never reach it, so this reason is never asserted falsely.
        if bundle.gateway_errors:
            kinds = ", ".join(sorted({error.kind for error in bundle.gateway_errors}))
            fired["gateway_degraded"] = f"a required read tool returned an error ({kinds})"
        if output.category == "real_regression" and not bundle.diff.files:
            fired[EMPTY_DIFF_CONTRADICTION] = (
                "category is real_regression but the diff contains no changed files"
            )
        return fired

    def _calibrate(
        self, output: Diagnosis, state: RunState
    ) -> tuple[float, list[Adjustment]]:
        return calibrate(
            output.self_confidence,
            self.signals(output, self._bundle(state)),
            self.confidence_model,
        )

    def confidence_for(self, output: Diagnosis, state: RunState) -> float | None:
        """The post-calibration figure that goes into `AgentResult.confidence`."""
        return self._calibrate(output, state)[0]

    async def run(self, state: RunState) -> AgentResult[Diagnosis]:
        """Diagnose, then write the calibrated confidence back into the diagnosis.

        `Diagnosis` is frozen, so the two harness-added fields land through a
        `model_copy`. They are set here rather than left to a caller because a `Diagnosis`
        travelling with `final_confidence` still at its 0.0 default is indistinguishable
        from one the model was genuinely unsure about -- and the guardrail rules that read
        it cannot tell the difference either.
        """
        result = await super().run(state)
        if result.output is None:
            return result
        final_confidence, adjustments = self._calibrate(result.output, state)
        diagnosis = result.output.model_copy(
            update={
                "final_confidence": final_confidence,
                "confidence_adjustments": adjustments,
            }
        )
        return result.model_copy(
            update={"output": diagnosis, "confidence": final_confidence}
        )
