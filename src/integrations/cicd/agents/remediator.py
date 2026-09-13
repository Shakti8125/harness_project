"""The Remediator: one model call that proposes a plan, then the harness decides.

Agents propose; they never execute. The model's output is a `RemediationPlan` -- an
action, the tool calls that action implies, and the drafted content those calls carry.
Everything after the model returns is deterministic and lives in
`src/integrations/cicd/remediation.py`: the plan is normalised (harness-minted ids and
idempotency keys), every call is judged by the `PolicyEngine` against facts read from the
diagnosis and the bundle, and only a plan the policy allows in full is executed -- through
the gateway, which re-checks the forbidden set on its own. A plan needing approval is
returned as an `ApprovalRequest` inside the result for the API layer to persist; a plan the
policy refuses is returned with every decision so the run can escalate with the reason in
hand. This agent touches no storage and takes no action the engine did not allow.

**Every decision is a span.** `policy.decide` spans, component `guardrails`, carry the
tool, the rule id, the effect, the reason, the obligations and the facts the rule was
evaluated against, plus the matched rule's own text verbatim -- PLAN.md's "the policy is
quoted verbatim in the trace". A `<default>` deny carries the near-miss explanation from
the engine, so the trace of a denied retry says *which clause* denied it.

**When the model call fails, the stage fails.** Unlike the Investigator there is no
deterministic half to fall back on: a plan that does not exist cannot be evaluated. The
`AgentResult` from `LLMAgent.run` is returned re-typed, its `status` and `error` intact,
and the orchestrator ends the run on the escalation reason it already derives from the
error kind.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

from pydantic import JsonValue

from src.harness.agent import AgentPrompt, LLMAgent
from src.harness.context_manager import (
    ContextBudget,
    ContextManager,
    ContextRequest,
    Section,
)
from src.harness.contracts import AgentResult
from src.harness.gateway import ToolGateway
from src.harness.guardrails import PolicyDecision, PolicyEngine, PolicySpec
from src.harness.llm import DEFAULT_REQUEST_TIMEOUT_S, LlmClient
from src.harness.memory import MemoryStore
from src.harness.observability import TraceRecorder
from src.harness.orchestrator import RunState
from src.harness.recovery import RetryPolicy
from src.integrations.cicd.agents.diagnostician import BUNDLE_KEY
from src.integrations.cicd.history import (
    DEGRADED_MEMORY,
    action_observation,
)
from src.integrations.cicd.remediation import (
    EVALUATION_SKIPPED,
    build_facts,
    decide_plan,
    execute_plan,
    new_approval,
    normalize_plan,
    plan_verdict,
    result_for,
)
from src.integrations.cicd.rendering import (
    load_prompt_template,
    render_action_catalog,
    render_diagnosis,
    render_diff_patches,
    render_diff_summary,
    render_job,
    render_remediator_prompt,
)
from src.integrations.cicd.schemas import (
    Diagnosis,
    FailureBundle,
    RemediationPlan,
    RemediationResult,
)

logger = logging.getLogger("harness.integrations.cicd.remediator")

#: The artifact key the Diagnostician's output is filed under in `RunState.artifacts`.
DIAGNOSIS_KEY: Final[str] = "diagnosis"

#: The diff is the only large thing this prompt carries, and it is budgeted rather than
#: pasted: a fix PR needs the patch, not the log -- the Diagnostician already pulled the
#: lines that matter into its citations, which travel with the diagnosis.
_DIFF_PRIORITY: Final[int] = 7

#: PLAN.md "Concrete numbers in one place": approval expiry, 24 h.
DEFAULT_APPROVAL_TTL_H: Final[int] = 24

#: Twice the harness default. The provider counts thinking tokens against
#: `max_output_tokens`, and the first live run spent ~3 900 of the default 4 096 thinking,
#: hit `MAX_TOKENS` with 152 tokens of output, and -- after Recovery's "answer more
#: briefly" nudge -- produced a one-call plan with no draft. A fix PR carries a whole
#: file in `pr_draft.files[].new_content`; it needs the room.
REMEDIATOR_MAX_OUTPUT_TOKENS: Final[int] = 8192


def render_policy_summary(spec: PolicySpec) -> str:
    """The policy, as a short list the model can read before it proposes.

    Telling the model what will be refused is cheaper than letting it find out: a proposed
    call the policy denies costs a model call and produces a denied run. The text is derived
    from the loaded spec, so it cannot drift from what the engine enforces.
    """
    lines = [f"Forbidden, always refused: {', '.join(spec.forbidden) or 'none'}"]
    for rule in spec.rules:
        conditions = ", ".join(
            f"{key} {cond.model_dump(by_alias=True, exclude_none=True)}"
            for key, cond in rule.when.items()
        )
        lines.append(
            f"  {rule.id}: {', '.join(rule.tools)} -> {rule.effect}"
            + (f" when {conditions}" if conditions else "")
        )
    lines.append(f"Anything else -> {spec.default_effect}.")
    return "\n".join(lines)


class Remediator(LLMAgent[RemediationPlan]):
    """Proposes a plan, has it judged, and executes only what was allowed."""

    key = "remediator"

    def __init__(
        self,
        *,
        llm: LlmClient,
        model: str,
        recorder: TraceRecorder,
        gateway: ToolGateway,
        engine: PolicyEngine,
        context_manager: ContextManager,
        approval_ttl_h: int = DEFAULT_APPROVAL_TTL_H,
        evaluation_verdict: str = EVALUATION_SKIPPED,
        budget: ContextBudget | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout_s: float | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        super().__init__(
            key="remediator",
            output_model=RemediationPlan,
            llm=llm,
            model=model,
            recorder=recorder,
            retry_policy=retry_policy,
            max_output_tokens=REMEDIATOR_MAX_OUTPUT_TOKENS,
            timeout_s=DEFAULT_REQUEST_TIMEOUT_S if timeout_s is None else timeout_s,
        )
        self.gateway = gateway
        self.engine = engine
        self.context_manager = context_manager
        self.approval_ttl_h = approval_ttl_h
        # Phase 4 replaces the literal with the Evaluator's verdict for the run. Injected
        # rather than read from an artifact so the wiring, not this class, says where a
        # verdict comes from.
        self.evaluation_verdict = evaluation_verdict
        self.budget = budget if budget is not None else context_manager.default_budget
        # Phase 3: an executed side-effecting plan is written back to memory as the
        # observation's action (`record_observation` obligation). `None` records nothing.
        self.memory = memory

    # -- artifacts ----------------------------------------------------------------
    def _diagnosis(self, state: RunState) -> Diagnosis:
        diagnosis = state.artifacts.get(DIAGNOSIS_KEY)
        if not isinstance(diagnosis, Diagnosis):
            raise TypeError(f"remediator expected a Diagnosis at artifacts[{DIAGNOSIS_KEY!r}]")
        return diagnosis

    def _bundle(self, state: RunState) -> FailureBundle:
        bundle = state.artifacts.get(BUNDLE_KEY)
        if not isinstance(bundle, FailureBundle):
            raise TypeError(f"remediator expected a FailureBundle at artifacts[{BUNDLE_KEY!r}]")
        return bundle

    # -- prompt -------------------------------------------------------------------
    async def build_prompt(self, state: RunState) -> AgentPrompt:
        diagnosis = self._diagnosis(state)
        bundle = self._bundle(state)

        diff_text = render_diff_patches(bundle.diff)
        if diff_text:
            assembled = self.context_manager.assemble(
                ContextRequest(
                    sections=[Section(key="diff", content=diff_text, priority=_DIFF_PRIORITY)],
                    budget=self.budget,
                    anchor_patterns=[],
                )
            )
            diff_text = assembled.text

        prompt_text = render_remediator_prompt(
            diagnosis_summary=render_diagnosis(diagnosis),
            job_summary=render_job(bundle.job),
            diff_summary=render_diff_summary(bundle.diff),
            diff_patches=diff_text or "(no patches available)",
            tool_catalog=render_action_catalog(self.gateway),
            policy_summary=render_policy_summary(self.engine.spec),
        )
        # See `Investigator.build_prompt` for why the template version travels as a
        # nested span rather than an `AgentPrompt` field.
        async with self.recorder.span(
            "prompt.render",
            "agent",
            agent=self.key,
            prompt_version=load_prompt_template("remediator").version,
        ):
            pass
        return AgentPrompt(text=prompt_text)

    # -- decide and act -----------------------------------------------------------
    async def _record_decision(
        self, decision: PolicyDecision, facts: Mapping[str, JsonValue]
    ) -> None:
        rule = self.engine.rule(decision.rule_id)
        async with self.recorder.span(
            "policy.decide",
            "guardrails",
            tool=decision.tool,
            rule_id=decision.rule_id,
            effect=decision.effect,
            reason=decision.reason,
            obligations=list(decision.obligations),
            facts=dict(facts),
            rule=rule.model_dump(by_alias=True, mode="json") if rule is not None else None,
        ):
            pass

    async def _record_action(self, state: RunState, output: RemediationResult) -> None:
        """Rewrite this run's observation with the action that executed (decision 9).

        What counts as an action -- a plan that ran to completion with at least one write,
        dry-run included -- is `history.action_observation`'s rule, shared with the
        approval route so the retry cap counts an approved retry exactly like an
        automatic one. Same deterministic observation id as the Diagnostician's write, so
        this is the same row.
        """
        if self.memory is None:
            return
        bundle = self._bundle(state)
        key = bundle.prior_history.key
        if key is None:
            return
        observation = action_observation(
            key=key,
            run_id=state.run_id,
            diagnosis=self._diagnosis(state),
            job=bundle.job,
            executed=output.executed,
        )
        if observation is None:
            return
        try:
            await self.memory.record_observation(observation)
        except Exception:  # noqa: BLE001 - the action happened; failing to note it must not undo the run
            logger.warning("remediator: could not record the action in memory", exc_info=True)
            if DEGRADED_MEMORY not in state.degraded:
                state.degraded.append(DEGRADED_MEMORY)

    async def run(self, state: RunState) -> AgentResult[RemediationResult]:  # type: ignore[override]
        plan_result = await super().run(state)
        if plan_result.output is None:
            # No plan, nothing to judge: the stage fails on the model's own error and the
            # orchestrator escalates from `error.kind` -- see the module docstring.
            return AgentResult[RemediationResult](
                agent=plan_result.agent,
                status=plan_result.status,
                output=None,
                confidence=plan_result.confidence,
                evidence=plan_result.evidence,
                attempts=plan_result.attempts,
                latency_ms=plan_result.latency_ms,
                tokens=plan_result.tokens,
                prompt_sha256=plan_result.prompt_sha256,
                model=plan_result.model,
                error=plan_result.error,
            )

        diagnosis = self._diagnosis(state)
        bundle = self._bundle(state)

        normalized = normalize_plan(
            plan_result.output,
            run_id=state.run_id,
            job=bundle.job,
            forbidden=self.engine.forbidden,
        )
        plan = normalized.plan
        facts = build_facts(
            diagnosis,
            bundle,
            evaluation_verdict=self.evaluation_verdict,
            # One plan per run in this phase, so nothing has executed yet when it is
            # judged. A later phase that lets a run act twice supplies the real count.
            side_effecting_actions_so_far=0,
        )

        async with self.recorder.span(
            "remediation.plan",
            "guardrails",
            agent=self.key,
            action=plan.action,
            tool_calls=[call.tool for call in plan.tool_calls],
            tool_calls_derived=normalized.derived,
            # What the model actually asked for, before normalisation -- so a hallucinated
            # `merge_pull_request` under a derivable action is still on the record even
            # though it was never judged (review finding 5).
            proposed_tool_calls=[call.tool for call in plan_result.output.tool_calls],
            dropped_tool_calls=list(normalized.dropped),
        ) as plan_span:
            decisions = decide_plan(self.engine, plan, facts)
            for decision in decisions:
                await self._record_decision(decision, facts)
            verdict = plan_verdict(plan, decisions)
            plan_span.set_attribute("verdict", verdict)

            if verdict == "execute":
                executed = await execute_plan(
                    self.gateway, plan, decisions, recorder=self.recorder
                )
                output = result_for(plan, decisions, executed=executed)
                await self._record_action(state, output)
            elif verdict == "await_approval":
                approval = new_approval(
                    run_id=state.run_id,
                    plan=plan,
                    decisions=decisions,
                    ttl_h=self.approval_ttl_h,
                )
                plan_span.set_attribute("approval_id", approval.approval_id)
                output = result_for(plan, decisions, approval=approval)
            else:
                output = result_for(plan, decisions)
            plan_span.set_attribute("status", output.status)

        return AgentResult[RemediationResult](
            agent=self.key,
            status="ok",
            output=output,
            confidence=None,
            evidence=plan_result.evidence,
            attempts=plan_result.attempts,
            latency_ms=plan_result.latency_ms,
            tokens=plan_result.tokens,
            prompt_sha256=plan_result.prompt_sha256,
            model=plan_result.model,
            error=None,
        )
