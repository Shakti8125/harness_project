"""From a `RemediationPlan` to a `RemediationResult`: facts, decisions, execution.

Everything between "the model proposed a plan" and "the run knows what happened to it"
lives here, as plain functions over the domain models, so that the Remediator agent and
the approval route (`POST /v1/approvals/{id}`, which executes a stored plan later) run
the *same* code. PLAN.md's two enforcement points are both visible in this file: the
policy engine is consulted per call before anything executes (`decide_plan`), and every
call that does execute goes through `ToolGateway.invoke`, which re-checks the forbidden
set on its own (`execute_plan`).

Three rules, stated once:

1. **A plan is evaluated whole and executes whole.** Every proposed call gets a
   `PolicyDecision` before any call runs. One `deny` and nothing runs; otherwise one
   `require_approval` and nothing runs until a person says so; otherwise everything runs
   in order and stops at the first failure. Half a plan -- a branch and no PR -- is a
   worse state than no plan.
2. **The model's call ids and keys are discarded.** `normalize_plan` re-mints `call_id`
   and writes Appendix C's `idempotency_key` on every side-effecting call, the same way
   the Investigator already re-mints ids for the model's optional read calls. A model
   that can name a tool can name an idempotency key; only the harness may.
3. **The facts are read from the bundle, not from a stub.** `memory.retries_for_signature_24h`
   is `bundle.prior_history.retries_in_24h`, which `PriorHistory` itself forces to
   `FAIL_CLOSED_RETRIES_IN_24H` whenever the history is `unavailable` -- the Investigator's
   only shape in this phase. So the retry cap denies until memory is real (Phase 3), and
   the `<default>` reason in the trace says which clause did it. That is amendment 3 of
   PLAN.md Phase 2, chosen over a stub that would make the cap unreachable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from pydantic import JsonValue

from src.harness.contracts import RunId
from src.harness.gateway import ToolCall, ToolGateway, ToolResult
from src.harness.guardrails import (
    FACT_SIDE_EFFECTING_ACTIONS,
    ActionContext,
    PolicyDecision,
    PolicyEngine,
)
from src.harness.observability import TraceRecorder
from src.integrations.cicd.catalog import side_effect_of
from src.integrations.cicd.rendering import new_call_id
from src.integrations.cicd.schemas import (
    ApprovalRequest,
    Diagnosis,
    FailureBundle,
    JobRef,
    RemediationPlan,
    RemediationResult,
)

logger = logging.getLogger("harness.integrations.cicd.remediation")

#: The Evaluator is Phase 4. Until then the verdict fact is the literal `"skipped"` --
#: honest about a verdict nobody computed, and the policy rules admit it explicitly (the
#: `{in: [pass, skipped]}` clauses), so that admission is visible in the policy file and
#: comes back out when a real verdict exists.
EVALUATION_SKIPPED: Final[str] = "skipped"

PlanVerdict = Literal["execute", "await_approval", "deny", "no_action"]


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def build_facts(
    diagnosis: Diagnosis,
    bundle: FailureBundle,
    *,
    evaluation_verdict: str = EVALUATION_SKIPPED,
    side_effecting_actions_so_far: int = 0,
) -> dict[str, JsonValue]:
    """The flat dotted namespace `policy.yaml` conditions read.

    Every key a rule in the policy names is set here, unconditionally: a rule whose fact
    is absent can never match (`PolicyEngine` fails closed on a missing fact), so an
    omission here would silently turn an `allow` rule into a deny. Keys the policy does
    not (yet) read are included where they cost nothing and make the trace more legible.
    """
    return {
        "diagnosis.category": diagnosis.category,
        "diagnosis.final_confidence": diagnosis.final_confidence,
        "diagnosis.suggested_action": diagnosis.suggested_action,
        "evaluation.verdict": evaluation_verdict,
        "memory.retries_for_signature_24h": bundle.prior_history.retries_in_24h,
        "memory.unavailable": bundle.prior_history.unavailable,
        "context.cold_start": bundle.cold_start,
        FACT_SIDE_EFFECTING_ACTIONS: side_effecting_actions_so_far,
    }


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def _canonical_args(args: dict[str, JsonValue]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def idempotency_key_for(run_id: RunId, call: ToolCall) -> str:
    """Appendix C: `f"{run_id}:{tool}:{sha256(args)[:8]}"`."""
    digest = hashlib.sha256(_canonical_args(call.args).encode("utf-8")).hexdigest()[:8]
    return f"{run_id}:{call.tool}:{digest}"


def canonical_tool_calls(plan: RemediationPlan, job: JobRef) -> list[ToolCall]:
    """The tool calls an action implies, derived from the plan's own drafts.

    Used only when the model named an action and proposed no calls for it. The mapping is
    fixed -- there is exactly one way to retry a run, and a PR is always branch, files, PR
    -- so this is normalisation, not invention: the *choice* of action and every word of
    content are the model's. An action whose content the plan does not carry (a PR with no
    `pr_draft`) derives nothing, and the plan then reads as `no_action`.
    """
    if plan.action == "retry_job":
        return [
            ToolCall(
                call_id=new_call_id(),
                tool="rerun_failed_jobs",
                args={"run_id": job.run_id, "attempt": job.run_attempt},
            )
        ]
    if plan.action == "file_ticket":
        if plan.ticket_draft is None:
            return []
        return [
            ToolCall(
                call_id=new_call_id(),
                tool="create_issue",
                args={
                    "title": plan.ticket_draft.title,
                    "body": plan.ticket_draft.body,
                    "labels": list(plan.ticket_draft.labels),
                },
            )
        ]
    if plan.action in ("open_fix_pr", "open_revert_pr"):
        draft = plan.pr_draft
        if draft is None:
            return []
        calls = [
            ToolCall(
                call_id=new_call_id(),
                tool="create_branch",
                args={"name": draft.branch, "from_sha": job.head_sha},
            )
        ]
        for patch in draft.files:
            calls.append(
                ToolCall(
                    call_id=new_call_id(),
                    tool="create_or_update_file",
                    args={
                        "branch": draft.branch,
                        "path": patch.path,
                        "content_b64": base64.b64encode(
                            patch.new_content.encode("utf-8")
                        ).decode("ascii"),
                        "message": draft.title,
                    },
                )
            )
        calls.append(
            ToolCall(
                call_id=new_call_id(),
                tool="open_pull_request",
                args={
                    "head": draft.branch,
                    "base": draft.base,
                    "title": draft.title,
                    "body": draft.body,
                    "draft": True,
                    "labels": list(draft.labels),
                },
            )
        )
        return calls
    return []


def normalize_plan(
    plan: RemediationPlan, *, run_id: RunId, job: JobRef
) -> tuple[RemediationPlan, bool]:
    """The plan as the harness will evaluate and store it.

    Returns the normalised plan and whether its calls were *derived* (the model proposed
    none). Every call gets a fresh harness-minted `call_id`; every side-effecting call gets
    Appendix C's `idempotency_key`. Read calls keep no key -- nothing to protect.
    """
    derived = False
    calls = list(plan.tool_calls)
    if not calls and plan.action != "no_action":
        calls = canonical_tool_calls(plan, job)
        derived = bool(calls)

    normalized: list[ToolCall] = []
    for call in calls:
        fresh = ToolCall(call_id=new_call_id(), tool=call.tool, args=dict(call.args))
        if side_effect_of(call.tool) != "read":
            fresh = fresh.model_copy(
                update={"idempotency_key": idempotency_key_for(run_id, fresh)}
            )
        normalized.append(fresh)

    return plan.model_copy(update={"tool_calls": normalized}), derived


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def decide_plan(
    engine: PolicyEngine, plan: RemediationPlan, facts: dict[str, JsonValue]
) -> list[PolicyDecision]:
    """One `PolicyDecision` per proposed call, in plan order, none of them executed."""
    return [
        engine.decide(
            ActionContext(
                tool=call.tool, side_effect=side_effect_of(call.tool), facts=facts
            )
        )
        for call in plan.tool_calls
    ]


def plan_verdict(plan: RemediationPlan, decisions: list[PolicyDecision]) -> PlanVerdict:
    """Rule 1 of the module docstring, as a function."""
    if not plan.tool_calls:
        return "no_action"
    effects = {decision.effect for decision in decisions}
    if "deny" in effects:
        return "deny"
    if "require_approval" in effects:
        return "await_approval"
    return "execute"


def new_approval(
    *,
    run_id: RunId,
    plan: RemediationPlan,
    decisions: list[PolicyDecision],
    ttl_h: int,
    now: datetime | None = None,
) -> ApprovalRequest:
    requested_at = now or datetime.now(UTC)
    return ApprovalRequest(
        approval_id="apr_" + secrets.token_hex(8),
        run_id=run_id,
        state="pending",
        plan=plan,
        decisions=decisions,
        requested_at=requested_at,
        expires_at=requested_at + timedelta(hours=ttl_h),
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def execute_plan(
    gateway: ToolGateway,
    plan: RemediationPlan,
    decisions: list[PolicyDecision],
    *,
    recorder: TraceRecorder | None = None,
) -> list[ToolResult]:
    """Run every call in order with its decision; stop at the first failure.

    Callers must have established `plan_verdict(...) == "execute"` (or hold an approval)
    first -- this function does not re-decide. It does hand each call's decision to the
    gateway, which is what lets the gateway's own forbidden re-check refuse a call that
    somehow reached here with paperwork saying `allow`.
    """
    if len(decisions) != len(plan.tool_calls):
        raise ValueError("one decision per tool call is required")
    results: list[ToolResult] = []
    for call, decision in zip(plan.tool_calls, decisions, strict=True):
        result = await gateway.invoke(call, decision)
        results.append(result)
        if recorder is not None:
            async with recorder.span(
                "remediation.execute",
                "gateway",
                tool=call.tool,
                side_effect=side_effect_of(call.tool),
                rule_id=decision.rule_id,
                ok=result.ok,
                dry_run=result.dry_run,
                cached=result.cached,
                error_kind=result.error.kind if result.error is not None else None,
            ):
                pass
        if not result.ok:
            logger.warning(
                "remediation stopped at %r: %s",
                call.tool,
                result.error.message if result.error is not None else "no error recorded",
            )
            break
    return results


def result_for(
    plan: RemediationPlan,
    decisions: list[PolicyDecision],
    *,
    executed: list[ToolResult] | None = None,
    approval: ApprovalRequest | None = None,
) -> RemediationResult:
    """Assemble the stage output from what actually happened."""
    verdict = plan_verdict(plan, decisions)
    if verdict == "no_action":
        status: Literal["executed", "awaiting_approval", "denied", "no_action"] = "no_action"
    elif verdict == "deny":
        status = "denied"
    elif approval is not None:
        status = "awaiting_approval"
    else:
        status = "executed"
    return RemediationResult(
        plan=plan,
        decisions=decisions,
        executed=list(executed or []),
        pending_approval=approval,
        status=status,
    )


def denial_summary(decisions: list[PolicyDecision]) -> str:
    """One line naming the denied tool(s) and why, for the escalation message."""
    denied = [d for d in decisions if d.effect == "deny"]
    if not denied:
        return "no decision denied"
    first = denied[0]
    more = f" (+{len(denied) - 1} more)" if len(denied) > 1 else ""
    return f"policy denied {first.tool!r} via {first.rule_id}: {first.reason}{more}"
