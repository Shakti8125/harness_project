# ruff: noqa: E501
"""SEC-05: a fix plan's branch, base and paths are the harness's, whatever the model wrote.

`PrDraft.branch` was constrained only by a comment. A plan with `branch: "main"` would
have committed to `main` once dry run was off, and a drafted `.github/workflows/*.yml` is
code execution with the repository's secrets. `normalize_plan` runs before the plan is
judged or shown for approval, so what a person approves is what would be written.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.integrations.cicd.remediation import normalize_plan
from src.integrations.cicd.schemas import JobRef, RemediationPlan

SIGNATURE = "0123456789abcdef0123456789abcdef"
BRANCH = f"agent/fix/{SIGNATURE[:8]}"


def job(branch: str = "release/1.2") -> JobRef:
    return JobRef(
        repo="octo-org/harness-demo-repo", workflow_name="CI", workflow_id=9001,
        run_id=501234567, run_attempt=1, job_id=601234567, job_name="test (3.12)",
        head_sha="e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df", branch=branch, event="push",
        started_at=datetime.now(UTC), completed_at=None, conclusion="failure",
    )


def drafted(files: list[tuple[str, str]], *, branch: str = "main", base: str = "main") -> RemediationPlan:
    return RemediationPlan.model_validate({
        "rationale": "Fix it.",
        "action": "open_fix_pr",
        "tool_calls": [],
        "pr_draft": {
            "branch": branch, "base": base, "title": "Fix", "body": "Body",
            "files": [
                {"path": path, "new_content": content, "rationale": "r"} for path, content in files
            ],
            "labels": ["agent-generated"], "draft": True,
        },
        "ticket_draft": None,
    })


def test_the_model_branch_and_base_are_replaced_and_a_workflow_file_is_dropped() -> None:
    plan = drafted([("src/pricing.py", "fixed\n"), (".github/workflows/x.yml", "on: push\n")])

    normalized = normalize_plan(
        plan, run_id="run_01M2R8VWQMXD36ATHMKCD08G5F", job=job(), signature_id=SIGNATURE
    )

    draft = normalized.plan.pr_draft
    assert draft is not None
    assert draft.branch == BRANCH
    assert draft.base == "release/1.2"
    assert [f.path for f in draft.files] == ["src/pricing.py"]
    calls = normalized.plan.tool_calls
    assert [c.tool for c in calls] == ["create_branch", "create_or_update_file", "open_pull_request"]
    assert calls[0].args["name"] == BRANCH
    assert calls[1].args["branch"] == BRANCH and calls[1].args["path"] == "src/pricing.py"
    assert calls[2].args["head"] == BRANCH and calls[2].args["base"] == "release/1.2"
    assert any(".github/" in entry for entry in normalized.dropped), normalized.dropped


def test_every_refused_path_is_dropped_and_named() -> None:
    plan = drafted([
        (".GitHub/workflows/x.yml", "x"), ("../outside.py", "x"), ("a/../../b.py", "x"),
        ("x?ref=evil", "x"), ("docs/ok.md", "x"),
    ])

    normalized = normalize_plan(
        plan, run_id="run_01M2R8VWQMXD36ATHMKCD08G5F", job=job(), signature_id=SIGNATURE
    )

    draft = normalized.plan.pr_draft
    assert draft is not None and [f.path for f in draft.files] == ["docs/ok.md"]
    assert len([d for d in normalized.dropped if d.startswith("create_or_update_file")]) == 4


def test_model_proposed_pr_calls_are_rewritten_when_there_is_no_draft() -> None:
    plan = RemediationPlan.model_validate({
        "rationale": "Fix it.",
        "action": "open_fix_pr",
        "tool_calls": [
            {"call_id": "tc_model000001", "tool": "create_branch",
             "args": {"name": "main", "from_sha": "e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df"}},
            {"call_id": "tc_model000002", "tool": "create_or_update_file",
             "args": {"branch": "main", "path": ".github/workflows/x.yml", "content_b64": "eA==", "message": "m"}},
            {"call_id": "tc_model000003", "tool": "create_or_update_file",
             "args": {"branch": "main", "path": "src/x.py", "content_b64": "eA==", "message": "m"}},
            {"call_id": "tc_model000004", "tool": "open_pull_request",
             "args": {"head": "main", "base": "main", "title": "t", "body": "b"}},
        ],
        "pr_draft": None,
        "ticket_draft": None,
    })

    normalized = normalize_plan(
        plan, run_id="run_01M2R8VWQMXD36ATHMKCD08G5F", job=job(), signature_id=SIGNATURE
    )

    calls = normalized.plan.tool_calls
    assert [c.tool for c in calls] == ["create_branch", "create_or_update_file", "open_pull_request"]
    assert calls[0].args["name"] == BRANCH
    assert calls[1].args == {"branch": BRANCH, "path": "src/x.py", "content_b64": "eA==", "message": "m"}
    assert calls[2].args["head"] == BRANCH and calls[2].args["base"] == "release/1.2"
    assert any(".github/" in entry for entry in normalized.dropped), normalized.dropped


def test_a_run_without_a_signature_still_gets_an_agent_branch() -> None:
    normalized = normalize_plan(
        drafted([("src/x.py", "x")]), run_id="run_01M2R8VWQMXD36ATHMKCD08G5F", job=job(),
        signature_id=None,
    )

    draft = normalized.plan.pr_draft
    assert draft is not None and draft.branch.startswith("agent/fix/")
    assert len(draft.branch) == len("agent/fix/") + 8


def test_a_file_write_without_a_string_path_is_dropped_not_written_as_none() -> None:
    """The Stage 1e audit's finding 4: `file_path(None)` validated `str(None)`."""
    plan = RemediationPlan.model_validate({
        "rationale": "Fix it.",
        "action": "open_fix_pr",
        "tool_calls": [
            {"call_id": "tc_model000001", "tool": "create_or_update_file",
             "args": {"branch": "main", "content_b64": "eA==", "message": "m"}},
            {"call_id": "tc_model000002", "tool": "create_or_update_file",
             "args": {"branch": "main", "path": 42, "content_b64": "eA==", "message": "m"}},
        ],
        "pr_draft": None,
        "ticket_draft": None,
    })

    normalized = normalize_plan(
        plan, run_id="run_01M2R8VWQMXD36ATHMKCD08G5F", job=job(), signature_id=SIGNATURE
    )

    assert normalized.plan.tool_calls == []
    assert normalized.dropped.count("create_or_update_file (invalid path)") == 2
