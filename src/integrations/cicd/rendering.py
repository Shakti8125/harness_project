"""Deterministic rendering of the CI/CD bundle into prompt text and evidence.

Both agents render the same objects — the failing job, the diff, the dependency bumps,
the truncation notice — and they must render them identically: the Diagnostician's
citations are checked against text the Investigator's prompt also carried, and two
formatters that drift apart turn a correct quote into a refuted one.

Nothing here calls a model or a tool. Everything is a pure function of the domain models,
which is what makes prompt text reproducible across runs and diffable across edits.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any, Final

from src.harness.context_manager import TruncationReport
from src.harness.contracts import Evidence
from src.harness.gateway import ToolGateway
from src.integrations.cicd.schemas import (
    DependencyChange,
    Diagnosis,
    DiffSummary,
    FileChange,
    InvestigationNotes,
    JobRef,
)

#: Appendix A.1 caps an excerpt at 2000 characters.
MAX_EXCERPT_CHARS: Final[int] = 2000

#: How many changed files to list in the prompt's diff summary before stopping. The full
#: patch text reaches the model through the budgeted context section; this listing is an
#: index, and an index of 300 paths is noise.
MAX_LISTED_FILES: Final[int] = 50

#: `prompts/*.md` -- versioned template files, not Python string constants. PLAN.md's
#: repo layout is explicit about this shape ("prompts/*.md <- versioned, hash logged into
#: the trace") and names the rejected alternative by its actual cost: a Python constant
#: is invisible in diffs and impossible to attribute an eval regression to.
_PROMPTS_DIR: Final[Path] = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class PromptTemplate:
    """One parsed `prompts/*.md` file: its declared version and its template body."""

    version: str
    body: str


_template_cache: dict[str, PromptTemplate] = {}


def load_prompt_template(name: str) -> PromptTemplate:
    """Read and parse `prompts/{name}.md`: a `version:` line, a `---` separator, then body.

    Cached per process. These are static files shipped with the image -- re-reading and
    re-parsing the same template on every prompt render buys nothing and costs a file
    open per agent call.

    The body is a `string.Template`, not an f-string or `.format()` template: every
    section this renders is JSON, and JSON is full of braces that `.format()` would
    demand doubled or blow up on. `$identifier` has no meaning to a curly brace, so a log
    excerpt or a diff patch substitutes in verbatim regardless of what it contains.
    """
    if name in _template_cache:
        return _template_cache[name]
    path = _PROMPTS_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    first_line, _, rest = text.partition("\n")
    if not first_line.startswith("version:"):
        raise ValueError(f"{path} is missing its `version:` front-matter line")
    version = first_line.removeprefix("version:").strip()
    separator, _, body = rest.partition("\n")
    if separator.strip() != "---":
        raise ValueError(f"{path} is missing the `---` front-matter separator")
    template = PromptTemplate(version=version, body=body)
    _template_cache[name] = template
    return template


#: Every template name this integration renders. Kept here, not derived by scanning
#: `_PROMPTS_DIR`, so that a stray or half-written `.md` file dropped in the directory
#: does not silently become part of what startup validates -- only the names the agents
#: actually call `load_prompt_template` with are load-bearing.
_TEMPLATE_NAMES: Final[tuple[str, ...]] = ("investigator", "diagnostician", "remediator")


def validate_prompt_templates() -> None:
    """Load every prompt template this integration renders; raise on any failure.

    A missing or malformed `prompts/*.md` file is a packaging fault, not a per-request
    condition -- `review-2.md` finding 3. Calling this eagerly at process startup (the
    caller's job, not this module's -- see the docstring on `load_prompt_template`) turns
    that fault into a loud boot failure instead of a silent green `/healthz` followed by a
    `500` on the first replay. Deliberately does not catch anything: `load_prompt_template`
    already raises a specific `ValueError` for a missing `version:` line or `---`
    separator, and `Path.read_text` raises its own `OSError` subclass for a missing file;
    both are informative on their own and swallowing either here would just repackage them
    as this function's own generic failure.
    """
    for name in _TEMPLATE_NAMES:
        load_prompt_template(name)


def new_call_id() -> str:
    return "tc_" + secrets.token_hex(6)


def make_evidence(source: str, locator: str, excerpt: str) -> Evidence:
    """One `Evidence` row, with the hash the Evaluator re-checks against.

    The hash is taken over the *normalized* excerpt — trailing whitespace stripped per
    line — so that a later re-read of the same source that differs only in trailing
    spaces still verifies.
    """
    normalized = "\n".join(line.rstrip() for line in excerpt.strip().splitlines())
    return Evidence(
        evidence_id="ev_" + secrets.token_hex(6),
        source=source,  # type: ignore[arg-type]
        locator=locator,
        excerpt=excerpt[:MAX_EXCERPT_CHARS],
        sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating the trailing `Z` the API emits."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def diff_from_compare(data: dict[str, Any], base_sha: str, head_sha: str) -> DiffSummary:
    """Build a `DiffSummary` from a recorded compare-commits response body."""
    raw_files = data.get("files")
    files: list[FileChange] = []
    if isinstance(raw_files, list):
        for entry in raw_files:
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("status", "modified"))
            if status not in ("added", "modified", "removed", "renamed"):
                status = "modified"
            files.append(
                FileChange(
                    path=str(entry.get("filename", "")),
                    status=status,  # type: ignore[arg-type]
                    additions=int(entry.get("additions", 0) or 0),
                    deletions=int(entry.get("deletions", 0) or 0),
                    patch=entry.get("patch"),
                )
            )
    total_files = int(data.get("total_files", len(files)) or len(files))
    return DiffSummary(
        baseline_kind="branch_green",
        base_sha=base_sha,
        head_sha=head_sha,
        commits_behind=int(data.get("behind_by", 0) or 0),
        files=files,
        # The API caps a compare at 300 files; a shorter `files` list than `total_files`
        # is how that cap announces itself.
        truncated=total_files > len(files),
        total_files=total_files,
    )


def render_job(job: JobRef) -> str:
    return (
        f"repo: {job.repo}\n"
        f"workflow: {job.workflow_name} (id {job.workflow_id})\n"
        f"run: {job.run_id} attempt {job.run_attempt}, event {job.event}, "
        f"branch {job.branch}\n"
        f"job: {job.job_name} (id {job.job_id}), conclusion {job.conclusion}\n"
        f"head_sha: {job.head_sha}\n"
        f"runner labels: {', '.join(job.runner_labels) or 'none recorded'}"
    )


def render_diff_summary(diff: DiffSummary) -> str:
    """The index of what changed. The patches themselves go through the context budget."""
    if diff.baseline_kind == "none":
        return (
            "No green baseline run exists for this branch, so there is no diff to "
            "compare against. Do not infer a cause from changed files; there are none "
            "available."
        )
    lines = [
        f"baseline: {diff.baseline_kind}, base {diff.base_sha} -> head {diff.head_sha}",
        f"{len(diff.files)} changed file(s)"
        + (f" of {diff.total_files} (truncated)" if diff.truncated else ""),
    ]
    lines.extend(
        f"  {change.status:8} {change.path} (+{change.additions}/-{change.deletions})"
        for change in diff.files[:MAX_LISTED_FILES]
    )
    return "\n".join(lines)


def render_diff_patches(diff: DiffSummary) -> str:
    """The patch text itself, as a context section the budget can trim."""
    return "\n\n".join(
        f"--- {change.path} ({change.status})\n{change.patch}"
        for change in diff.files
        if change.patch
    )


def render_dependencies(changes: list[DependencyChange]) -> str:
    if not changes:
        return "None detected in the diff."
    return "\n".join(
        f"  {change.ecosystem}: {change.package} {change.from_version} -> "
        f"{change.to_version} ({change.manifest_path}, {change.source})"
        for change in changes
    )


def render_catalog(gateway: ToolGateway) -> str:
    """The read-only half of the tool catalog, as the model may request from it."""
    return "\n".join(
        f"  {spec.name}({', '.join(_schema_args(spec.input_schema))}) - {spec.description}"
        for spec in gateway.catalog()
        if spec.side_effect == "read"
    )


def render_action_catalog(gateway: ToolGateway) -> str:
    """The side-effecting half of the catalog, as the Remediator may propose from it.

    Every write tool is listed -- including the ones no phase has implemented yet -- and
    the one destructive tool is listed *as forbidden*, so the model is told rather than
    left to discover that it will be refused. Read tools are omitted: a plan is an action,
    and the evidence-gathering already happened.
    """
    lines = []
    for spec in gateway.catalog():
        if spec.side_effect == "read":
            continue
        marker = "  [FORBIDDEN -- never propose]" if spec.side_effect == "destructive" else ""
        lines.append(
            f"  {spec.name}({', '.join(_schema_args(spec.input_schema))}) - "
            f"{spec.description}{marker}"
        )
    return "\n".join(lines)


def render_diagnosis(diagnosis: Diagnosis) -> str:
    """The Diagnostician's conclusion, for the Remediator's prompt."""
    citations = "\n".join(
        f"  - [{c.claim_kind}] {c.locator}: {c.quote!r}" + (f" ({c.note})" if c.note else "")
        for c in diagnosis.citations
    ) or "  (none)"
    return (
        f"category: {diagnosis.category}\n"
        f"final_confidence: {diagnosis.final_confidence:.2f}\n"
        f"suggested_action: {diagnosis.suggested_action}\n"
        f"summary: {diagnosis.summary}\n"
        f"suspected_commit_sha: {diagnosis.suspected_commit_sha or 'none'}\n"
        f"suspected_test_ids: {', '.join(diagnosis.suspected_test_ids) or 'none'}\n"
        f"suspected_package: {diagnosis.suspected_package or 'none'}\n"
        f"reasoning: {diagnosis.reasoning}\n"
        f"citations:\n{citations}"
    )


def _schema_args(schema: dict[str, Any]) -> list[str]:
    properties = schema.get("properties")
    return list(properties) if isinstance(properties, dict) else []


def render_truncation(reports: dict[str, TruncationReport]) -> str:
    """Tell the model what it is not seeing.

    A model shown a trimmed log with no notice will reason as though the trimmed part did
    not exist. Saying so — and saying explicitly that error lines were preserved — is
    what lets it treat an absent line as "not shown" rather than "did not happen".
    """
    lines = []
    for key, report in reports.items():
        if report.anchors_dropped:
            lines.append(
                f"NOTE: section {key!r} was trimmed and {report.anchors_dropped} error "
                f"anchor window(s) did not fit. Some failure output is missing."
            )
        elif report.elisions:
            lines.append(
                f"NOTE: section {key!r} was trimmed from {report.original_lines:,} to "
                f"{report.kept_lines:,} lines. Elided ranges are marked inline; every "
                f"line matching an error pattern was kept."
            )
    return "\n".join(lines) or "The evidence below is complete; nothing was trimmed."


def render_investigation_summary(notes: InvestigationNotes | None) -> str:
    """The Investigator's notes, rendered for the Diagnostician's prompt."""
    if notes is None:
        return (
            "The Investigator's notes are unavailable for this run. Work from the raw "
            "evidence below."
        )
    if not notes.observations:
        return notes.narrative
    observations = "\n".join(f"  - {line}" for line in notes.observations)
    return f"{notes.narrative}\n\nObservations:\n{observations}"


def render_investigator_prompt(
    *,
    job_summary: str,
    diff_summary: str,
    dependency_summary: str,
    prior_history_summary: str,
    tool_catalog: str,
    context_bundle: str,
    truncation_note: str,
) -> str:
    """Assemble the Investigator prompt from `prompts/investigator.md` and the bundle."""
    return Template(load_prompt_template("investigator").body).substitute(
        job_summary=job_summary,
        diff_summary=diff_summary,
        dependency_summary=dependency_summary,
        prior_history_summary=prior_history_summary,
        tool_catalog=tool_catalog,
        context_bundle=context_bundle,
        truncation_note=truncation_note,
    )


def render_remediator_prompt(
    *,
    diagnosis_summary: str,
    job_summary: str,
    diff_summary: str,
    diff_patches: str,
    tool_catalog: str,
    policy_summary: str,
) -> str:
    """Assemble the Remediator prompt from `prompts/remediator.md` and the artifacts."""
    return Template(load_prompt_template("remediator").body).substitute(
        diagnosis_summary=diagnosis_summary,
        job_summary=job_summary,
        diff_summary=diff_summary,
        diff_patches=diff_patches,
        tool_catalog=tool_catalog,
        policy_summary=policy_summary,
    )


def render_diagnostician_prompt(
    *,
    job_summary: str,
    diff_summary: str,
    dependency_summary: str,
    prior_history_summary: str,
    investigation_summary: str,
    context_bundle: str,
    truncation_note: str,
) -> str:
    """Assemble the Diagnostician prompt from `prompts/diagnostician.md` and the bundle."""
    return Template(load_prompt_template("diagnostician").body).substitute(
        job_summary=job_summary,
        diff_summary=diff_summary,
        dependency_summary=dependency_summary,
        prior_history_summary=prior_history_summary,
        investigation_summary=investigation_summary,
        context_bundle=context_bundle,
        truncation_note=truncation_note,
    )
