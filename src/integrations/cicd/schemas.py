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

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.harness.confidence import Adjustment
from src.harness.context_manager import TruncationReport
from src.harness.contracts import RunId
from src.harness.gateway import ToolCall, ToolError, ToolResult
from src.harness.guardrails import PolicyDecision

_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)


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
    occurrences: int = 0
    verdict_counts: dict[str, int] = {}
    last_verdict: str | None = None
    last_seen_at: datetime | None = None
    prior_hint: Literal["likely_flaky", "likely_real", "unknown"] = "unknown"
    retries_in_24h: int = 0
    sample_run_ids: list[str] = []
    unavailable: bool = False


class InvestigationNotes(BaseModel):  # the Investigator's LLM output
    model_config = _MODEL_CONFIG

    observations: list[str] = Field(max_length=8)
    additional_tool_calls: list[ToolCall] = Field(max_length=3)  # read-only tools only
    narrative: str = Field(max_length=800)


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
    gateway_errors: list[ToolError] = []


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

    action: Literal["retry_job", "open_fix_pr", "open_revert_pr", "file_ticket", "no_action"]
    rationale: str = Field(max_length=800)
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
    status: Literal["executed", "awaiting_approval", "denied", "no_action"]


__all__ = [
    "JobRef",
    "LogExcerpt",
    "FileChange",
    "DiffSummary",
    "DependencyChange",
    "PriorHistory",
    "InvestigationNotes",
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
