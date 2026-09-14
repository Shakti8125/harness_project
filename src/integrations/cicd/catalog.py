"""The CI/CD tool catalog of PLAN.md Appendix A.4, as `ToolSpec`s.

One definition, shared by `ReplayToolGateway` and `GitHubToolGateway`, so the two cannot
advertise different tools to the model or disagree about a tool's side-effect class -- the
class is what `PolicyEngine` keys `"read:*"` on and what the per-run action cap counts, so
a drift between gateways would be a drift in what the policy permits.

Nothing here is callable. Each gateway decides what it does with a name; this module only
says what the names are and what each one costs.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import JsonValue

from src.harness.gateway import ToolSpec

SideEffect = Literal["read", "write", "destructive"]


def _spec(
    name: str,
    description: str,
    properties: dict[str, JsonValue],
    required: list[str],
    *,
    side_effect: SideEffect = "read",
    idempotent: bool = True,
) -> ToolSpec:
    required_names: list[JsonValue] = list(required)
    return ToolSpec(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": properties, "required": required_names},
        side_effect=side_effect,
        idempotent=idempotent,
    )


#: A.4's table, in its order. `merge_pull_request` is registered only so the deny path is
#: testable -- it is in `policy.yaml`'s `forbidden` list and no gateway implements it.
CATALOG: Final[tuple[ToolSpec, ...]] = (
    _spec(
        "list_workflow_run_jobs",
        "List the jobs of one workflow run attempt, with their conclusions.",
        {"run_id": {"type": "integer"}, "attempt": {"type": "integer"}},
        ["run_id", "attempt"],
    ),
    _spec(
        "get_job_logs",
        "Fetch the raw text log of one job. Returns the LAST max_bytes bytes.",
        {"job_id": {"type": "integer"}, "max_bytes": {"type": "integer"}},
        ["job_id"],
    ),
    _spec(
        "find_last_successful_run",
        "Find the most recent successful run of a workflow on a branch.",
        {
            "workflow_id": {"type": "integer"},
            "branch": {"type": "string"},
            "before": {"type": "string"},
        },
        ["workflow_id", "branch"],
    ),
    _spec(
        "compare_commits",
        "Compare two commits and return the changed files with their patches.",
        {"base": {"type": "string"}, "head": {"type": "string"}},
        ["base", "head"],
    ),
    _spec(
        "get_commit",
        "Fetch one commit, including the files it touched.",
        {"sha": {"type": "string"}},
        ["sha"],
    ),
    _spec(
        "get_file_contents",
        "Fetch the contents of one file at a given ref.",
        {"path": {"type": "string"}, "ref": {"type": "string"}},
        ["path", "ref"],
    ),
    _spec(
        "search_workflow_runs",
        "Search runs of a workflow, filtered by branch and status.",
        {
            "workflow_id": {"type": "integer"},
            "branch": {"type": "string"},
            "status": {"type": "string"},
            "per_page": {"type": "integer"},
        },
        ["workflow_id"],
    ),
    _spec(
        "rerun_failed_jobs",
        "Re-run only the failed jobs of one workflow run. Idempotent: a run already "
        "re-running is reported as success.",
        {"run_id": {"type": "integer"}, "attempt": {"type": "integer"}},
        ["run_id"],
        side_effect="write",
    ),
    _spec(
        "create_branch",
        "Create a branch from a commit. Idempotent: an existing branch is returned as-is.",
        {"name": {"type": "string"}, "from_sha": {"type": "string"}},
        ["name", "from_sha"],
        side_effect="write",
    ),
    _spec(
        "create_or_update_file",
        "Write one file on a branch, as a commit.",
        {
            "branch": {"type": "string"},
            "path": {"type": "string"},
            "content_b64": {"type": "string"},
            "message": {"type": "string"},
            "sha": {"type": "string"},
        },
        ["branch", "path", "content_b64", "message"],
        side_effect="write",
    ),
    _spec(
        "open_pull_request",
        "Open a DRAFT pull request. Idempotent: an open PR for the same head is returned.",
        {
            "head": {"type": "string"},
            "base": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "draft": {"type": "boolean"},
            "labels": {"type": "array", "items": {"type": "string"}},
        },
        ["head", "base", "title", "body"],
        side_effect="write",
    ),
    _spec(
        "create_issue",
        "File an issue. Idempotent per failure signature: a duplicate is commented on.",
        {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
            # Set by the harness, never by the model: the failure signature the issue
            # is filed for, embedded as Appendix C's marker so a repeat is a comment.
            "signature_id": {"type": "string"},
        },
        ["title", "body"],
        side_effect="write",
    ),
    _spec(
        "merge_pull_request",
        "Merge a pull request. FORBIDDEN by policy; registered so the refusal is testable.",
        {"number": {"type": "integer"}},
        ["number"],
        side_effect="destructive",
        idempotent=False,
    ),
)

SPECS_BY_NAME: Final[dict[str, ToolSpec]] = {spec.name: spec for spec in CATALOG}

READ_TOOLS: Final[tuple[str, ...]] = tuple(
    spec.name for spec in CATALOG if spec.side_effect == "read"
)
WRITE_TOOLS: Final[tuple[str, ...]] = tuple(
    spec.name for spec in CATALOG if spec.side_effect != "read"
)

#: Write tools with a real body. Since Phase 5 that is every write the policy can allow;
#: `merge_pull_request` is forbidden and refused before any gateway looks at it. A future
#: catalog write tool without a body answers `ToolError(kind="unknown")` naming the phase
#: that implements it -- returned rather than raised because `invoke` raises only for
#: programming errors, and a person approving a plan through the API is not one.
IMPLEMENTED_WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "rerun_failed_jobs",
        "create_branch",
        "create_or_update_file",
        "open_pull_request",
        "create_issue",
    }
)


def side_effect_of(tool: str) -> SideEffect:
    """The catalog's side-effect class for `tool` -- `"destructive"` when it is not listed.

    Fail closed: a tool the catalog does not know cannot be a read, and treating it as the
    most dangerous class means no `"read:*"` rule can match it and the per-run action cap
    applies. The policy's `default_effect: deny` then answers, and the trace says so.
    """
    spec = SPECS_BY_NAME.get(tool)
    return spec.side_effect if spec is not None else "destructive"


def not_implemented_message(tool: str) -> str:
    return f"{tool!r} is registered in the catalog but no gateway implements it yet"
