"""Payload models for the CI/CD integration.

Transcribed verbatim from PLAN.md Appendix A.11. Every model is
``ConfigDict(extra="forbid", frozen=True)`` per PLAN.md line 931 (the harness-wide
schema policy: no silent extra fields, no in-place mutation after construction).

These models import their harness-owned building blocks (``TruncationReport``,
``ToolCall``, ``ToolResult``, ``ToolError``, ``PolicyDecision``, ``RunId``,
``Adjustment``) from ``src.harness.*`` — integrations depend on the harness,
never the reverse. ``Adjustment`` in particular is re-exported, not
redeclared: it is the same class ``src.harness.confidence.calibrate()``
returns, so a ``Diagnosis.confidence_adjustments`` list built from that
function's output validates without a foreign-model coercion error.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.harness.confidence import Adjustment
from src.harness.context_manager import TruncationReport
from src.harness.contracts import RunId
from src.harness.gateway import ToolCall, ToolError, ToolResult
from src.harness.guardrails import PolicyDecision
from src.harness.memory import SignatureKey

_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)

#: Appendix B.3's fail-closed retry count. Deliberately far above any cap a policy rule
#: would set, so an unreadable history can never read as "room for another retry".
FAIL_CLOSED_RETRIES_IN_24H: Final[int] = 999


class JobRef(BaseModel):
    model_config = _MODEL_CONFIG

    repo: str
    workflow_name: str
    workflow_id: int
    run_id: int
    run_attempt: int
    job_id: int
    job_name: str
    head_sha: str
    branch: str
    event: str
    started_at: datetime
    completed_at: datetime | None
    conclusion: str
    runner_labels: list[str] = []


class LogExcerpt(BaseModel):
    model_config = _MODEL_CONFIG

    job_id: int
    total_lines: int
    included_lines: int
    excerpt: str
    anchor_line_numbers: list[int]
    truncation: TruncationReport


class FileChange(BaseModel):
    model_config = _MODEL_CONFIG

    path: str
    status: Literal["added", "modified", "removed", "renamed"]
    additions: int
    deletions: int
    patch: str | None = None  # None when GitHub omits it (binary/too large)


class DiffSummary(BaseModel):
    model_config = _MODEL_CONFIG

    baseline_kind: Literal["branch_green", "default_green", "head_commit_only", "none"]
    base_sha: str | None
    head_sha: str
    commits_behind: int = 0
    files: list[FileChange] = []
    truncated: bool = False  # GitHub caps compare at 300 files
    total_files: int = 0


class DependencyChange(BaseModel):
    model_config = _MODEL_CONFIG

    ecosystem: Literal["pip", "npm", "go", "maven", "cargo", "other"]
    manifest_path: str
    package: str
    from_version: str | None
    to_version: str | None
    source: Literal["manifest_diff", "lockfile_diff"]


class PriorHistory(BaseModel):
    model_config = _MODEL_CONFIG

    signature_id: str | None
    # Phase 3 (A.11 amended, dispatch decision 8): the key the history was looked up under,
    # always computed by the Investigator whether or not the store answered. It lets the
    # Diagnostician and Remediator write under the same key without recomputing it, and
    # lets the approval route re-query memory for a fresh count. `None` only on a bundle
    # investigated before this phase.
    key: SignatureKey | None = None
    occurrences: int = 0
    verdict_counts: dict[str, int] = {}
    last_verdict: str | None = None
    last_seen_at: datetime | None = None
    prior_hint: Literal["likely_flaky", "likely_real", "unknown"] = "unknown"
    retries_in_24h: int = 0
    # Phase 3 fix round (audit finding 3): what the most recent *resolved* automatic retry
    # of this signature did. `None` when no rerun has been resolved yet. The prompts say
    # memory reports "whether an automatic retry of it passed"; this is that report, and
    # a `failed_again` here withholds the `memory_agreement` bonus and the
    # `likely_flaky` hint until a rerun passes again.
    last_retry_outcome: Literal["passed_on_retry", "failed_again"] | None = None
    sample_run_ids: list[str] = []
    unavailable: bool = False

    def _apply_fail_closed_retry_cap(self) -> None:
        """Appendix B.3: the degraded-memory path must default the retry-cap fact to
        a conservative ``999`` "so that the flaky-retry rule fails closed" -- an
        unreadable history must not read as "no retries yet" once a retry cap is
        wired to this field (Phase 2). Structural rather than a discipline guarantee
        at each construction site, the same principle already applied to the
        whole-body ``Redactor`` and to ``problem()``'s scrub: ``unavailable=True``
        implies ``retries_in_24h == 999``, unconditionally.

        The first version of this carried an escape hatch -- ``model_fields_set``
        distinguishes "not supplied" from "supplied as 0", so an explicitly supplied
        count was left alone. That was wrong twice over. It fails open on the exact
        shape it was written to defend: a caller that reads a degraded ``MemoryHit``,
        computes ``0`` retries from its empty ``actions_in_window`` and passes that
        ``0`` explicitly alongside ``unavailable=True`` gets ``0`` back, which is what
        B.3 exists to prevent. And it is incoherent on its face: a caller claiming to
        know the count while also declaring the history unreadable is asserting two
        things that cannot both hold. There is no legitimate reading of
        ``unavailable=True`` under which a supplied count is authoritative, so there is
        no override (final-audit finding 2).

        Shared by the ``after`` validator below (construction, ``model_validate``,
        ``model_validate_json``) and by the ``model_copy`` override further down
        (which pydantic deliberately does *not* run validators for -- see that
        method's docstring for why a second call site is required here rather
        than relying on the validator alone).

        ``object.__setattr__`` bypasses the model's own ``frozen=True`` -- the
        documented way to mutate a field from a place that runs once, before the
        model is handed to any caller.
        """
        if self.unavailable and self.retries_in_24h != FAIL_CLOSED_RETRIES_IN_24H:
            object.__setattr__(self, "retries_in_24h", FAIL_CLOSED_RETRIES_IN_24H)

    @model_validator(mode="after")
    def _fail_closed_retry_cap_when_unavailable(self) -> "PriorHistory":
        self._apply_fail_closed_retry_cap()
        return self

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> "PriorHistory":
        """``BaseModel.model_copy`` is documented as copying without re-running
        validators -- so the ``unavailable`` implies ``retries_in_24h == 999``
        guarantee above would silently stop holding on exactly the shape Phase 3's
        memory wiring needs: read a history, then flip ``unavailable`` on the
        already-constructed object when the read degrades, i.e.
        ``history.model_copy(update={"unavailable": True})`` almost verbatim.

        Reapplying the same rule against the *copy* fixes that without changing
        ``PriorHistory``'s field shape, and since the rule is unconditional the copy
        lands on the same answer the constructor and ``model_validate`` would give for
        the same field values -- including when ``update`` sets ``unavailable`` and a
        retry count in one call.

        ``model_construct`` (pydantic's documented validation bypass) is
        deliberately left unguarded: a caller reaching for it is opting out of
        validation on purpose, and re-deriving fail-closed state there would mean
        this model runs validator-equivalent logic even when explicitly told not
        to.
        """
        copied = super().model_copy(update=update, deep=deep)
        copied._apply_fail_closed_retry_cap()
        return copied


class InvestigationNotes(BaseModel):  # the Investigator's LLM output
    model_config = _MODEL_CONFIG

    observations: list[str] = Field(max_length=8)
    additional_tool_calls: list[ToolCall] = Field(max_length=3)  # read-only tools only
    narrative: str = Field(max_length=800)


class AdditionalToolCallOutcome(BaseModel):
    """What happened to one of the Investigator's up-to-three optional,
    model-requested read-only tool calls (`InvestigationNotes.additional_tool_calls`).

    Visibility only. PLAN.md's confidence-adjustment table has no row for "the model
    asked for something and it was refused, or it failed" -- inventing one would put a
    number on the confidence scale that no `calibrate()` row justifies, the same
    reasoning `gateway_errors` above already rests on for the required calls. This
    field carries no confidence signal in either direction; it exists so a
    `RunOutcome` reader can tell obtained from refused from failed, which
    `review-2.md` finding 4 (Phase 1 backlog) found impossible before this field
    existed -- `executed` was populated and never read, and `result.data` was
    discarded.
    """

    model_config = _MODEL_CONFIG

    tool: str
    outcome: Literal["obtained", "refused", "failed"]
    # Populated when `outcome == "failed"`: the same `ToolError` the gateway
    # returned, e.g. `kind="not_found"` for a 404 on a read tool (B.2: "data, not a
    # run failure").
    error: ToolError | None = None
    # Populated when `outcome == "refused"`: the model named a tool outside this
    # gateway's read-only catalog, so the call was never placed at all.
    reason: str = ""


class FailureBundle(BaseModel):  # the Investigator's stage output
    model_config = _MODEL_CONFIG

    job: JobRef
    logs: list[LogExcerpt]
    diff: DiffSummary
    dependency_changes: list[DependencyChange] = []
    prior_history: PriorHistory
    notes: InvestigationNotes | None = None
    cold_start: bool = False
    collected_at: datetime
    # Populated ONLY from the required deterministic-collection calls (jobs, log, baseline,
    # compare) -- see `Investigator.run`. Errors from the model's optional
    # `additional_tool_calls`, and the refusal the Investigator synthesises when the model
    # names a write tool, are deliberately excluded: PLAN.md's `gateway_degraded` adjustment
    # conditions on "any REQUIRED read tool returned an error", and this field is the only
    # thing `Diagnostician.signals` reads to decide that row.
    gateway_errors: list[ToolError] = []
    # One entry per optional call the model actually named (at most 3, the cap on
    # `InvestigationNotes.additional_tool_calls`). See `AdditionalToolCallOutcome`.
    additional_tool_outcomes: list[AdditionalToolCallOutcome] = []


class Citation(BaseModel):
    model_config = _MODEL_CONFIG

    claim_kind: Literal[
        "quote_exists", "file_in_diff", "dependency_bump", "test_in_log", "commit_in_range"
    ]
    locator: str  # "log:job/2001" | "diff:requirements.txt"
    quote: str = Field(max_length=500)
    note: str = Field(default="", max_length=200)


class Diagnosis(BaseModel):  # the Diagnostician's stage output
    model_config = _MODEL_CONFIG

    # ordered first, via propertyOrdering, so the model reasons before concluding
    reasoning: str = Field(max_length=1200)
    category: Literal[
        "flaky_test", "real_regression", "dependency_break",
        "infra_transient", "config_issue", "unknown",
    ]
    summary: str = Field(max_length=280)
    self_confidence: float = Field(ge=0.0, le=1.0)
    citations: list[Citation] = Field(max_length=6)
    suspected_commit_sha: str | None = None
    suspected_test_ids: list[str] = []
    suspected_package: str | None = None
    suggested_action: Literal["retry", "open_fix_pr", "open_revert_pr", "file_ticket", "escalate"]
    # --- added by the harness after the model returns, not requested from the model ---
    final_confidence: float = 0.0
    confidence_adjustments: list[Adjustment] = []


class FilePatch(BaseModel):
    model_config = _MODEL_CONFIG

    path: str
    new_content: str
    rationale: str


class PrDraft(BaseModel):
    model_config = _MODEL_CONFIG

    branch: str  # deterministic: "agent/fix/{signature_id[:8]}"
    base: str
    title: str
    body: str
    files: list[FilePatch] = Field(max_length=5)
    labels: list[str] = ["agent-generated"]
    draft: bool = True


class TicketDraft(BaseModel):
    model_config = _MODEL_CONFIG

    title: str
    body: str
    labels: list[str] = ["agent-triage"]


class RemediationPlan(BaseModel):  # the Remediator's LLM output
    model_config = _MODEL_CONFIG

    # ordered first, via propertyOrdering, so the model reasons before concluding
    rationale: str = Field(max_length=800)
    action: Literal["retry_job", "open_fix_pr", "open_revert_pr", "file_ticket", "no_action"]
    tool_calls: list[ToolCall] = Field(max_length=5)  # PROPOSED, never pre-executed
    pr_draft: PrDraft | None = None
    ticket_draft: TicketDraft | None = None


class ApprovalRequest(BaseModel):
    model_config = _MODEL_CONFIG

    approval_id: str
    run_id: RunId
    state: Literal["pending", "approved", "rejected", "expired"]
    plan: RemediationPlan
    decisions: list[PolicyDecision]
    requested_at: datetime
    expires_at: datetime


class RemediationResult(BaseModel):  # the remediate stage output
    model_config = _MODEL_CONFIG

    plan: RemediationPlan
    decisions: list[PolicyDecision]
    executed: list[ToolResult] = []
    pending_approval: ApprovalRequest | None = None
    # `rejected` added in Phase 2 (A.11 amended): a person refusing a plan is not the
    # policy denying it, and `pending_approval.state` alone should not be what tells the
    # two apart.
    status: Literal["executed", "awaiting_approval", "denied", "rejected", "no_action"]


__all__ = [
    "JobRef",
    "LogExcerpt",
    "FileChange",
    "DiffSummary",
    "DependencyChange",
    "PriorHistory",
    "InvestigationNotes",
    "AdditionalToolCallOutcome",
    "FailureBundle",
    "Citation",
    "Adjustment",
    "Diagnosis",
    "FilePatch",
    "PrDraft",
    "TicketDraft",
    "RemediationPlan",
    "ApprovalRequest",
    "RemediationResult",
]
