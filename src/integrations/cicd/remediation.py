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
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, JsonValue

from src.harness.contracts import RunId
from src.harness.evaluator import EvaluationReport
from src.harness.gateway import ToolCall, ToolError, ToolGateway, ToolResult
from src.harness.guardrails import (
    FACT_SIDE_EFFECTING_ACTIONS,
    ActionContext,
    PolicyDecision,
    PolicyEngine,
    downgrade_for_warn,
)
from src.harness.observability import TraceRecorder, carries_redaction
from src.integrations.cicd import url_segments
from src.integrations.cicd.catalog import AGENT_BRANCH_PREFIX, side_effect_of
from src.integrations.cicd.rendering import new_call_id
from src.integrations.cicd.schemas import (
    ApprovalRequest,
    Diagnosis,
    FailureBundle,
    JobRef,
    PrDraft,
    RemediationPlan,
    RemediationResult,
)

logger = logging.getLogger("harness.integrations.cicd.remediation")

#: The verdict fact for a run that has no `evaluation` artifact -- a hand-built
#: orchestrator without the evaluate stage, or a run stored before Phase 4 and decided
#: through the approval route now. Honest about a verdict nobody computed; since Phase 4
#: no rule in `policy.yaml` admits it (PLAN Phase 2 amendment 2), so such a run can file a
#: ticket and nothing else.
EVALUATION_SKIPPED: Final[str] = "skipped"

#: The verdict under which every side-effecting decision is downgraded one step.
EVALUATION_WARN: Final[str] = "warn"

#: The fact key the policy's `evaluation.verdict` clauses read.
FACT_EVALUATION_VERDICT: Final[str] = "evaluation.verdict"

PlanVerdict = Literal["execute", "await_approval", "deny", "no_action"]

#: The tools each action may legitimately involve. A model-proposed call outside its
#: action's set is dropped before it is judged -- it is a call the stated action never
#: asked for (review finding 2: `no_action` with a `rerun_failed_jobs` attached would
#: otherwise be judged, and executed under a readable history). Dropped calls are
#: returned by `normalize_plan` so the trace can show what the model proposed.
TOOLS_FOR_ACTION: Final[dict[str, frozenset[str]]] = {
    "retry_job": frozenset({"rerun_failed_jobs"}),
    "open_fix_pr": frozenset({"create_branch", "create_or_update_file", "open_pull_request"}),
    "open_revert_pr": frozenset({"create_branch", "create_or_update_file", "open_pull_request"}),
    "file_ticket": frozenset({"create_issue"}),
    "no_action": frozenset(),
}


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def evaluation_verdict_of(evaluation: object) -> str:
    """The run's evaluation verdict, from the live artifact or its stored JSON.

    One function for the two callers of `build_facts` (the Remediator in-run over
    `state.artifacts["evaluation"]`, the approval route over
    `outcome.final["evaluation"]`), so both read the same verdict for the same run and
    fall back to the same `EVALUATION_SKIPPED` when the run has none.
    """
    if isinstance(evaluation, EvaluationReport):
        return evaluation.verdict
    if isinstance(evaluation, BaseModel):
        verdict = getattr(evaluation, "verdict", None)
        return verdict if isinstance(verdict, str) else EVALUATION_SKIPPED
    if isinstance(evaluation, Mapping):
        verdict = evaluation.get("verdict")
        return verdict if isinstance(verdict, str) else EVALUATION_SKIPPED
    return EVALUATION_SKIPPED


def build_facts(
    diagnosis: Diagnosis,
    bundle: FailureBundle,
    *,
    evaluation_verdict: str,
    side_effecting_actions_so_far: int,
) -> dict[str, JsonValue]:
    """The flat dotted namespace `policy.yaml` conditions read.

    Every key a rule in the policy names is set here, unconditionally: a rule whose fact
    is absent can never match (`PolicyEngine` fails closed on a missing fact), so an
    omission here would silently turn an `allow` rule into a deny. Keys the policy does
    not (yet) read are included where they cost nothing and make the trace more legible.

    `evaluation_verdict` has no default on purpose: two callers build facts (the
    Remediator in-run, the approval route later), and both must pass the run's real
    verdict through `evaluation_verdict_of` -- a default here would let one of them keep
    re-evaluating against a verdict the run never had (review note).
    """
    return {
        "diagnosis.category": diagnosis.category,
        "diagnosis.final_confidence": diagnosis.final_confidence,
        "diagnosis.suggested_action": diagnosis.suggested_action,
        FACT_EVALUATION_VERDICT: evaluation_verdict,
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


def canonical_tool_calls(
    plan: RemediationPlan, job: JobRef, *, signature_id: str | None = None
) -> list[ToolCall]:
    """The tool calls an action implies, derived from the plan's own drafts.

    The mapping is fixed -- there is exactly one way to retry a run, a ticket is one
    `create_issue`, and a PR is always branch, files, PR -- so this is normalisation, not
    invention: the *choice* of action and every word of content are the model's, and the
    identifiers (run id, attempt, head sha) come from the bundle rather than from the
    model's transcription of them. An action whose content the plan does not carry (a PR
    with no `pr_draft`) derives nothing; see `normalize_plan` for what happens then.

    `signature_id` (Phase 5) rides on the `create_issue` call so the live gateway can
    embed Appendix C's `<!-- harness:signature:… -->` marker and find the open issue for
    this failure on the next sighting instead of filing a second one.
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
                    **({"signature_id": signature_id} if signature_id else {}),
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


#: The PR-writing tools whose branch, base and paths `normalize_plan` sets itself.
_PR_TOOLS: Final[frozenset[str]] = frozenset(
    {"create_branch", "create_or_update_file", "open_pull_request"}
)


def agent_branch(signature_id: str | None, run_id: RunId) -> str:
    """`agent/fix/<signature_id[:8]>`: the only branch a fix plan may write (SEC-05).

    Derived, never taken from the model. A run without a signature (memory unavailable and
    no key) gets a branch of its own from its run id instead.
    """
    seed = signature_id or hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()
    return f"{AGENT_BRANCH_PREFIX}{seed[:8]}"


def refused_path(path: object) -> str | None:
    """Why a drafted file path may not be written, or `None` when it may.

    A path that fails the gateway's URL rules could leave the repository (SEC-03). A path
    under `.github/` is a workflow or repository setting: with a token that can write it,
    that is code execution with the repository's Actions secrets (SEC-05). The check is
    case-insensitive, whatever the host's file system.
    """
    try:
        clean = url_segments.file_path(path)
    except ValueError:
        return "invalid path"
    if clean.split("/", 1)[0].lower() == ".github":
        return "path under .github/"
    return None


def _sanitized_pr(
    plan: RemediationPlan, branch: str, base: str
) -> tuple[RemediationPlan, list[str]]:
    """The PR draft with the harness's branch and base, and only the files it may write."""
    draft = plan.pr_draft
    if draft is None:
        return plan, []
    kept = []
    dropped = []
    for patch in draft.files:
        reason = refused_path(patch.path)
        if reason is None:
            kept.append(patch.model_copy(update={"path": url_segments.file_path(patch.path)}))
        else:
            dropped.append(f"create_or_update_file ({reason})")
    clean: PrDraft = draft.model_copy(update={"branch": branch, "base": base, "files": kept})
    return plan.model_copy(update={"pr_draft": clean}), dropped


def _sanitized_pr_call(call: ToolCall, branch: str, base: str) -> ToolCall | None:
    """A model-proposed PR call with the harness's branch and base; `None` for a file
    write whose path may not be written."""
    args = dict(call.args)
    if call.tool == "create_branch":
        args["name"] = branch
    elif call.tool == "create_or_update_file":
        if refused_path(args.get("path")) is not None:
            return None
        args["path"] = url_segments.file_path(args.get("path"))
        args["branch"] = branch
    elif call.tool == "open_pull_request":
        args["head"] = branch
        args["base"] = base
    return call.model_copy(update={"args": args})


class NormalizedPlan:
    """What `normalize_plan` hands back, beside the plan itself.

    `derived` says the calls came from the drafts rather than the model's list; `dropped`
    names every model-proposed tool that was not carried into the plan -- replaced by
    derivation, or outside the action's set -- so the trace can show what the model asked
    for even when nothing of it survived (review finding 5).
    """

    __slots__ = ("derived", "dropped", "plan")

    def __init__(self, plan: RemediationPlan, *, derived: bool, dropped: list[str]) -> None:
        self.plan = plan
        self.derived = derived
        self.dropped = dropped


def normalize_plan(
    plan: RemediationPlan,
    *,
    run_id: RunId,
    job: JobRef,
    forbidden: frozenset[str] = frozenset(),
    signature_id: str | None = None,
) -> NormalizedPlan:
    """The plan as the harness will evaluate and store it.

    Whenever the action and the drafts determine the calls -- always for `retry_job`, and
    for `file_ticket` and the PR actions when their draft is present -- the canonical calls
    replace whatever the model proposed: the model's calls are a redundant, error-prone
    transcription of content it already gave in the draft (the first live run proposed
    `create_branch` with empty args and no PR after it). When nothing can be derived, the
    model's own calls are kept **only where they belong to the stated action's tool set**
    (`TOOLS_FOR_ACTION`); `no_action` keeps none.

    One exception runs the other way: a proposed call naming a tool in `forbidden` is
    **always** carried into the judged plan, derivation or not, so that the engine denies
    it by name and the run escalates `policy_denied` -- a model asking to merge is the
    single most important thing this stage can surface, and dropping it into a span
    attribute would make it the quietest. It can never execute: a forbidden decision
    denies the whole plan.

    Every surviving call gets a fresh harness-minted `call_id`; every side-effecting call
    gets Appendix C's `idempotency_key`. Read calls keep no key -- nothing to protect.

    SEC-05: a fix plan's branch, base and file paths are the harness's, not the model's.
    The branch is always `agent_branch(signature_id, run_id)`, the base is the failing
    run's branch, and a file under `.github/` or with a path the gateway would refuse is
    dropped and named in `dropped`. This runs before the plan is judged, so the approval
    shows what would actually be written.
    """
    branch = agent_branch(signature_id, run_id)
    plan, dropped_files = _sanitized_pr(plan, branch, job.branch)
    proposed = [call.tool for call in plan.tool_calls]
    derived = False
    canonical = (
        canonical_tool_calls(plan, job, signature_id=signature_id)
        if plan.action != "no_action" else []
    )
    if canonical:
        derived = True
        calls = canonical
    else:
        allowed = TOOLS_FOR_ACTION.get(plan.action, frozenset())
        calls = []
        for call in plan.tool_calls:
            if call.tool not in allowed:
                continue
            if call.tool in _PR_TOOLS:
                sanitized = _sanitized_pr_call(call, branch, job.branch)
                if sanitized is None:
                    dropped_files.append(f"{call.tool} ({refused_path(call.args.get('path'))})")
                    continue
                call = sanitized
            calls.append(call)
    kept = {call.tool for call in calls}
    for call in plan.tool_calls:
        if call.tool in forbidden and call.tool not in kept:
            calls.append(call)
            kept.add(call.tool)
    dropped = [tool for tool in proposed if tool not in kept] + dropped_files

    normalized: list[ToolCall] = []
    for call in calls:
        fresh = ToolCall(call_id=new_call_id(), tool=call.tool, args=dict(call.args))
        if side_effect_of(call.tool) != "read":
            fresh = fresh.model_copy(
                update={"idempotency_key": idempotency_key_for(run_id, fresh)}
            )
        normalized.append(fresh)

    return NormalizedPlan(
        plan.model_copy(update={"tool_calls": normalized}), derived=derived, dropped=dropped
    )


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def decide_plan(
    engine: PolicyEngine, plan: RemediationPlan, facts: dict[str, JsonValue]
) -> list[PolicyDecision]:
    """One `PolicyDecision` per proposed call, in plan order, none of them executed.

    PLAN.md Phase 4: when the run's evaluation verdict is `warn` -- a citation could not
    be checked -- every side-effecting effect is downgraded one step
    (`guardrails.downgrade_for_warn`), so a plan that would have executed now waits for
    a person. Read calls are not downgraded; nothing about a read needs approval.
    """
    warned = facts.get(FACT_EVALUATION_VERDICT) == EVALUATION_WARN
    decisions: list[PolicyDecision] = []
    for call in plan.tool_calls:
        side_effect = side_effect_of(call.tool)
        decision = engine.decide(
            ActionContext(tool=call.tool, side_effect=side_effect, facts=facts)
        )
        if warned and side_effect != "read":
            decision = downgrade_for_warn(decision)
        decisions.append(decision)
    return decisions


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
        if carries_redaction(call.args):
            # A stored plan is scrubbed at rest; an argument that now carries the
            # placeholder is not the argument the model wrote or a person approved,
            # so it is refused here rather than committed as if it were (Phase 5 audit
            # finding 1). Not retryable: the row will read the same tomorrow.
            result = _refused_as_altered(call)
        else:
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


def _refused_as_altered(call: ToolCall) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        tool=call.tool,
        ok=False,
        error=ToolError(
            kind="invalid_args",
            message=(
                f"{call.tool!r} was not executed: an argument carries the redaction "
                "placeholder, so the stored plan is not the plan that was approved (a "
                "credential was scrubbed from it at rest)"
            ),
            retryable=False,
        ),
        latency_ms=0,
    )


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
        status: Literal[
            "executed", "awaiting_approval", "denied", "rejected", "no_action"
        ] = "no_action"
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


def failed_execution(result: RemediationResult) -> ToolResult | None:
    """The first executed call that failed, if the plan was executed and one did.

    Appendix B.2: a write that fails (a 404 on a write tool, a 5xx that outlasted its
    retries, a tool nobody has implemented yet) is a run failure, escalated as
    `tool_failure` -- not a completed run with a red entry buried in `executed`.
    """
    if result.status != "executed":
        return None
    return next((r for r in result.executed if not r.ok), None)


def failure_summary(result: ToolResult) -> str:
    error = result.error
    if error is None:
        return f"{result.tool!r} failed with no error recorded"
    return f"{result.tool!r} failed ({error.kind}): {error.message}"


def denial_summary(decisions: list[PolicyDecision]) -> str:
    """One line naming the denied tool(s) and why, for the escalation message."""
    denied = [d for d in decisions if d.effect == "deny"]
    if not denied:
        return "no decision denied"
    first = denied[0]
    more = f" (+{len(denied) - 1} more)" if len(denied) > 1 else ""
    return f"policy denied {first.tool!r} via {first.rule_id}: {first.reason}{more}"
