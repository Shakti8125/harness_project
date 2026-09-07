"""The Investigator: deterministic collection, then exactly one model call.

PLAN.md's Phase 1 design note, implemented literally. Collection is plain Python --
fetch jobs, pick the failed ones, download logs, resolve the baseline, fetch the compare,
parse dependency manifests. The single LLM call takes the *assembled* bundle and produces
`InvestigationNotes` (observations plus up to three additional read-only tool calls it
wants), which are then executed and merged.

The rationale, from PLAN.md: a full ReAct loop over the API for evidence gathering is
where these systems become slow, expensive and nondeterministic, and it buys almost
nothing when the evidence set is known in advance. The one model call keeps the
extensibility point -- the model asking for evidence nobody anticipated -- without paying
for a loop.

**When the notes call fails, the stage still succeeds.** The bundle is emitted with
`notes=None` and `"investigator_notes"` appended to the run's degraded components. The
deterministic collection is the load-bearing half of this stage and it has already
happened; failing the whole run because the optional half failed would throw away
evidence the Diagnostician can still work from, and the Diagnostician will escalate on
its own if the model is genuinely unreachable.
"""

from __future__ import annotations

import contextvars
import re
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from src.harness.agent import AgentPrompt, LLMAgent
from src.harness.context_manager import (
    ContextBudget,
    ContextManager,
    ContextRequest,
    Section,
)
from src.harness.contracts import AgentResult, Evidence
from src.harness.gateway import ToolCall, ToolError, ToolGateway, ToolResult
from src.harness.guardrails import PolicyDecision
from src.harness.llm import LlmClient
from src.harness.observability import TraceRecorder
from src.harness.orchestrator import RunState
from src.harness.recovery import RetryPolicy
from src.integrations.cicd.gateway_replay import READ_TOOLS
from src.integrations.cicd.prompts.investigator import render_investigator_prompt
from src.integrations.cicd.rendering import (
    diff_from_compare,
    make_evidence,
    new_call_id,
    parse_datetime,
    render_catalog,
    render_dependencies,
    render_diff_patches,
    render_diff_summary,
    render_job,
    render_truncation,
)
from src.integrations.cicd.schemas import (
    DependencyChange,
    DiffSummary,
    FailureBundle,
    FileChange,
    InvestigationNotes,
    JobRef,
    LogExcerpt,
    PriorHistory,
)

#: PLAN.md Phase 1, step 2 of the Context Manager algorithm. This list lives in the
#: integration and is passed in, because every entry names a convention of a specific
#: toolchain -- the harness's Context Manager takes anchors as a parameter and knows
#: nothing about test runners or workflow log formats.
ANCHOR_PATTERNS: Final[tuple[str, ...]] = (
    r"^E\s",
    r"^FAILED\s",
    r"^ERROR\b",
    r"Traceback \(most recent call last\)",
    r"##\[error\]",
    r"AssertionError",
    r"Error:\s",
    r"npm ERR!",
    r"exit code \d+",
    r"\bTimeout\b",
    r"Connection refused",
    r"ModuleNotFoundError",
    r"ImportError",
)

#: How many bytes of a job log to ask the gateway for. The gateway keeps the LAST
#: `max_bytes`; the Context Manager then budgets what survives down to the char budget.
DEFAULT_MAX_LOG_BYTES: Final[int] = 2 * 1024 * 1024

#: Section keys the assembled context is built from, and their trim priorities.
#: The log is 10 -- trimmed last -- because it is the only section that names the actual
#: failure; the diff is smaller, and a truncated diff is still readable.
_LOG_PRIORITY: Final[int] = 10
_DIFF_PRIORITY: Final[int] = 7

_DEGRADED_LOGS: Final[str] = "logs"
_DEGRADED_BASELINE: Final[str] = "baseline"
_DEGRADED_DIFF: Final[str] = "diff"
_DEGRADED_NOTES: Final[str] = "investigator_notes"

_MANIFESTS: Final[dict[str, str]] = {
    "requirements.txt": "pip",
    "requirements-dev.txt": "pip",
    "pyproject.toml": "pip",
    "package.json": "npm",
    "package-lock.json": "npm",
    "go.mod": "go",
    "go.sum": "go",
    "pom.xml": "maven",
    "Cargo.toml": "cargo",
    "Cargo.lock": "cargo",
}

_LOCKFILES: Final[frozenset[str]] = frozenset(
    {"package-lock.json", "go.sum", "Cargo.lock"}
)

_PIP_REQUIREMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"^([+-])\s*([A-Za-z0-9._-]+(?:\[[A-Za-z0-9,._-]+\])?)\s*==\s*([^\s;#]+)"
)
_NPM_DEPENDENCY_RE: Final[re.Pattern[str]] = re.compile(
    r'^([+-])\s*"([^"]+)"\s*:\s*"[~^]?([^"]+)"'
)
_GO_MODULE_RE: Final[re.Pattern[str]] = re.compile(
    r"^([+-])\s*([a-z0-9./_-]+(?:\.[a-z]{2,})[a-z0-9./_-]*)\s+v([0-9][^\s]*)"
)
_CARGO_DEPENDENCY_RE: Final[re.Pattern[str]] = re.compile(
    r'^([+-])\s*([A-Za-z0-9_-]+)\s*=\s*"([^"]+)"'
)


#: Phase 1 has no `PolicyEngine` -- it arrives in Phase 2 with the Guardrails. Every tool
#: this agent calls is read-only, and `read-only-always` in `policy.yaml` is an
#: unconditional `allow` for exactly those, so the decision the engine would return is
#: known in advance and is synthesized here rather than faked as something richer. The
#: rule id says plainly where it came from, so it cannot be mistaken in a trace for an
#: engine-issued decision. The gateway re-checks the forbidden set regardless.
def read_only_decision(tool: str) -> PolicyDecision:
    return PolicyDecision(
        tool=tool,
        rule_id="read-only-always",
        effect="allow",
        reason="read-only tool; policy engine arrives in a later phase",
        evaluated_at=datetime.now(UTC),
    )


class _Collection(BaseModel):
    """Everything deterministic collection produced, carried from `build_prompt` to `run`.

    A `ContextVar` rather than an attribute on the agent: one agent instance serves every
    run, and two concurrent runs writing their collection to the same attribute would
    hand each other's evidence to the wrong model call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    job: JobRef
    logs: list[LogExcerpt]
    diff: DiffSummary
    dependency_changes: list[DependencyChange]
    prior_history: PriorHistory
    cold_start: bool
    gateway_errors: list[ToolError]
    evidence: list[Evidence]
    degraded: list[str]
    context_text: str


_collection: contextvars.ContextVar[_Collection | None] = contextvars.ContextVar(
    "cicd_investigator_collection", default=None
)


def parse_subject(subject: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields the collector needs out of a `workflow_run` webhook body.

    Accepts either the whole delivery body or just its `workflow_run` object, because the
    replay route hands over the recorded webhook verbatim and a caller constructing a
    `RunRequest` by hand should not have to know which half matters.
    """
    run = subject.get("workflow_run", subject)
    repository = subject.get("repository") or run.get("repository") or {}
    return {
        "repo": repository.get("full_name", ""),
        "run_id": int(run.get("id", 0)),
        "run_attempt": int(run.get("run_attempt", 1)),
        "head_sha": str(run.get("head_sha", "")),
        "branch": str(run.get("head_branch", "")),
        "workflow_id": int(run.get("workflow_id", 0)),
        "workflow_name": str(run.get("name", "")),
        "event": str(run.get("event", "")),
    }


def parse_dependency_changes(files: list[FileChange]) -> list[DependencyChange]:
    """Read version bumps out of the patches of any manifest or lockfile in the diff.

    Deliberately conservative: a package is reported only when both a removed and an
    added line name it, so a plain addition to a requirements file is not reported as a
    "bump" from nothing. Formats it cannot parse are skipped rather than guessed at --
    a wrong `DependencyChange` becomes a citation the Diagnostician can be wrong about.
    """
    changes: list[DependencyChange] = []
    for change in files:
        name = change.path.rsplit("/", 1)[-1]
        ecosystem = _MANIFESTS.get(name)
        if ecosystem is None or not change.patch:
            continue
        source = "lockfile_diff" if name in _LOCKFILES else "manifest_diff"
        patterns = {
            "pip": _PIP_REQUIREMENT_RE,
            "npm": _NPM_DEPENDENCY_RE,
            "go": _GO_MODULE_RE,
            "cargo": _CARGO_DEPENDENCY_RE,
        }
        pattern = patterns.get(ecosystem)
        if pattern is None:
            continue

        removed: dict[str, str] = {}
        added: dict[str, str] = {}
        for line in change.patch.splitlines():
            match = pattern.match(line)
            if match is None:
                continue
            sign, package, version = match.group(1), match.group(2), match.group(3)
            (added if sign == "+" else removed)[package] = version

        for package in sorted(set(removed) | set(added)):
            from_version = removed.get(package)
            to_version = added.get(package)
            if from_version is None or to_version is None or from_version == to_version:
                continue
            changes.append(
                DependencyChange(
                    ecosystem=ecosystem,  # type: ignore[arg-type]
                    manifest_path=change.path,
                    package=package,
                    from_version=from_version,
                    to_version=to_version,
                    source=source,  # type: ignore[arg-type]
                )
            )
    return changes


class Investigator(LLMAgent[InvestigationNotes]):
    """Collects the failure bundle, then asks the model what it observes in it."""

    key = "investigator"

    def __init__(
        self,
        *,
        gateway: ToolGateway,
        context_manager: ContextManager,
        llm: LlmClient,
        model: str,
        recorder: TraceRecorder,
        budget: ContextBudget | None = None,
        retry_policy: RetryPolicy | None = None,
        anchor_patterns: tuple[str, ...] = ANCHOR_PATTERNS,
        max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
        timeout_s: float | None = None,
    ) -> None:
        super().__init__(
            key="investigator",
            output_model=InvestigationNotes,
            llm=llm,
            model=model,
            recorder=recorder,
            retry_policy=retry_policy,
            **({"timeout_s": timeout_s} if timeout_s is not None else {}),
        )
        self.gateway = gateway
        self.context_manager = context_manager
        # The manager's `default_budget` is where the composition root threaded the
        # configured character budget; falling back to a bare `ContextBudget()` here is
        # exactly the silent no-op that would make the setting a lie.
        self.budget = budget if budget is not None else context_manager.default_budget
        self.anchor_patterns = anchor_patterns
        self.max_log_bytes = max_log_bytes

    async def _call_tool(
        self, tool: str, args: dict[str, Any], errors: list[ToolError]
    ) -> ToolResult:
        result = await self.gateway.invoke(
            ToolCall(call_id=new_call_id(), tool=tool, args=args),
            read_only_decision(tool),
        )
        if not result.ok and result.error is not None:
            errors.append(result.error)
        return result

    async def build_prompt(self, state: RunState) -> AgentPrompt:
        """Run the whole deterministic collection, then render the prompt over it."""
        subject = parse_subject(dict(state.request.subject))
        errors: list[ToolError] = []
        degraded: list[str] = []
        evidence: list[Evidence] = []

        # 1. the jobs of the failing run attempt
        jobs_result = await self._call_tool(
            "list_workflow_run_jobs",
            {"run_id": subject["run_id"], "attempt": subject["run_attempt"]},
            errors,
        )
        jobs: list[dict[str, Any]] = []
        if jobs_result.ok and jobs_result.data is not None:
            raw_jobs = jobs_result.data.get("jobs")
            if isinstance(raw_jobs, list):
                jobs = [job for job in raw_jobs if isinstance(job, dict)]
        failed = [job for job in jobs if job.get("conclusion") == "failure"] or jobs

        job = failed[0] if failed else {}
        job_ref = JobRef(
            repo=subject["repo"],
            workflow_name=subject["workflow_name"] or str(job.get("workflow_name", "")),
            workflow_id=subject["workflow_id"],
            run_id=subject["run_id"],
            run_attempt=subject["run_attempt"],
            job_id=int(job.get("id", 0)),
            job_name=str(job.get("name", "")),
            head_sha=subject["head_sha"],
            branch=subject["branch"],
            event=subject["event"],
            started_at=parse_datetime(job.get("started_at")) or datetime.now(UTC),
            completed_at=parse_datetime(job.get("completed_at")),
            conclusion=str(job.get("conclusion", "failure")),
            runner_labels=[str(label) for label in job.get("labels", []) or []],
        )

        # 2. the failing job's log
        log_excerpts: list[LogExcerpt] = []
        log_text = ""
        if job_ref.job_id:
            log_result = await self._call_tool(
                "get_job_logs",
                {"job_id": job_ref.job_id, "max_bytes": self.max_log_bytes},
                errors,
            )
            if log_result.ok and log_result.data is not None:
                log_text = str(log_result.data.get("content", ""))
            else:
                degraded.append(_DEGRADED_LOGS)
        else:
            degraded.append(_DEGRADED_LOGS)

        # 3. the last green run on the same branch, and the diff against it
        baseline_result = await self._call_tool(
            "find_last_successful_run",
            {
                "workflow_id": subject["workflow_id"],
                "branch": subject["branch"],
                "before": subject["head_sha"],
            },
            errors,
        )
        baseline_runs: list[dict[str, Any]] = []
        if baseline_result.ok and baseline_result.data is not None:
            raw_runs = baseline_result.data.get("workflow_runs")
            if isinstance(raw_runs, list):
                baseline_runs = [run for run in raw_runs if isinstance(run, dict)]
        elif not baseline_result.ok:
            degraded.append(_DEGRADED_BASELINE)

        # Appendix D: cold start is an EMPTY baseline list, not a failed call. The two
        # are different facts and only one of them is a degraded component.
        cold_start = not baseline_runs
        base_sha = str(baseline_runs[0].get("head_sha", "")) if baseline_runs else None

        diff = DiffSummary(
            baseline_kind="none" if cold_start else "branch_green",
            base_sha=base_sha,
            head_sha=subject["head_sha"],
        )
        if base_sha:
            compare_result = await self._call_tool(
                "compare_commits", {"base": base_sha, "head": subject["head_sha"]}, errors
            )
            if compare_result.ok and compare_result.data is not None:
                diff = diff_from_compare(compare_result.data, base_sha, subject["head_sha"])
            else:
                degraded.append(_DEGRADED_DIFF)

        dependency_changes = parse_dependency_changes(diff.files)

        # 4. budget the raw evidence down to something a model can read
        sections = [Section(key="log", content=log_text, priority=_LOG_PRIORITY)]
        diff_text = render_diff_patches(diff)
        if diff_text:
            sections.append(Section(key="diff", content=diff_text, priority=_DIFF_PRIORITY))

        bundle = self.context_manager.assemble(
            ContextRequest(
                sections=sections,
                budget=self.budget,
                anchor_patterns=list(self.anchor_patterns),
            )
        )
        log_report = bundle.truncation["log"]
        if log_text:
            log_excerpts.append(
                LogExcerpt(
                    job_id=job_ref.job_id,
                    total_lines=log_report.original_lines,
                    included_lines=log_report.kept_lines,
                    excerpt=bundle.per_section["log"],
                    anchor_line_numbers=self.context_manager.anchor_line_numbers(
                        sections[0], list(self.anchor_patterns)
                    ),
                    truncation=log_report,
                )
            )
            evidence.append(
                make_evidence(
                    "log",
                    f"log:job/{job_ref.job_id}",
                    bundle.per_section["log"],
                )
            )
        if diff_text:
            evidence.append(make_evidence("diff", f"diff:{subject['head_sha']}", diff_text))

        _collection.set(
            _Collection(
                job=job_ref,
                logs=log_excerpts,
                diff=diff,
                dependency_changes=dependency_changes,
                # Memory arrives in Phase 3; until then every run is its own first
                # sighting and says so, rather than claiming a history it cannot read.
                prior_history=PriorHistory(signature_id=None, unavailable=True),
                cold_start=cold_start,
                gateway_errors=errors,
                evidence=evidence,
                degraded=degraded,
                context_text=bundle.text,
            )
        )

        return AgentPrompt(
            text=render_investigator_prompt(
                job_summary=render_job(job_ref),
                diff_summary=render_diff_summary(diff),
                dependency_summary=render_dependencies(dependency_changes),
                prior_history_summary=(
                    "No prior history is available: the memory store is not wired up in "
                    "this phase. Treat this failure as a first sighting."
                ),
                tool_catalog=render_catalog(self.gateway),
                context_bundle=bundle.text,
                truncation_note=render_truncation(bundle.truncation),
            ),
            evidence=evidence,
            degraded=degraded,
        )

    async def run(self, state: RunState) -> AgentResult[FailureBundle]:  # type: ignore[override]
        """Collect, ask the model what it observes, execute what it asks for, assemble."""
        notes_result = await super().run(state)
        collected = _collection.get()
        if collected is None:  # pragma: no cover - build_prompt always sets it
            raise RuntimeError("investigator collection missing; build_prompt did not run")
        _collection.set(None)

        errors = list(collected.gateway_errors)
        notes = notes_result.output
        if notes is None and _DEGRADED_NOTES not in state.degraded:
            state.degraded.append(_DEGRADED_NOTES)

        # The extensibility point: up to three read-only calls the model asked for. The
        # cap and the read-only restriction are enforced here rather than trusted to the
        # prompt -- a model that can name a tool can name a write tool.
        executed: list[str] = []
        if notes is not None:
            for call in notes.additional_tool_calls[:3]:
                if call.tool not in READ_TOOLS:
                    errors.append(
                        ToolError(
                            kind="forbidden_by_policy",
                            message=f"{call.tool!r} is not a read-only tool; refused",
                            retryable=False,
                        )
                    )
                    continue
                result = await self._call_tool(call.tool, dict(call.args), errors)
                if result.ok:
                    executed.append(call.tool)

        bundle = FailureBundle(
            job=collected.job,
            logs=collected.logs,
            diff=collected.diff,
            dependency_changes=collected.dependency_changes,
            prior_history=collected.prior_history,
            notes=notes,
            cold_start=collected.cold_start,
            collected_at=datetime.now(UTC),
            gateway_errors=errors,
        )
        return AgentResult[FailureBundle](
            agent=self.key,
            # The stage succeeds on the strength of its deterministic half; see this
            # module's docstring.
            status="ok",
            output=bundle,
            confidence=None,
            evidence=collected.evidence,
            attempts=notes_result.attempts,
            latency_ms=notes_result.latency_ms,
            tokens=notes_result.tokens,
            prompt_sha256=notes_result.prompt_sha256,
            model=notes_result.model,
            error=None,
        )
