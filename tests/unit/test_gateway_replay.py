"""`ReplayToolGateway`: the truncation direction, and the forbidden re-check.

Two properties here are worth more than the rest of the file combined:

* `get_job_logs` keeps the LAST `max_bytes`. `fixtures/README.md` spells out why — get it
  backwards and the eval failure looks like a model or prompt problem when it is a
  one-line truncation bug.
* A forbidden tool is refused even when the `PolicyDecision` handed in says `allow`, the
  refusal comes back as data, and it costs zero file access.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.harness.gateway import ToolCall
from src.harness.guardrails import PolicyDecision
from src.integrations.cicd.gateway_replay import ReplayToolGateway, repo_slug

REPO = "octo-org/harness-demo-repo"


def allow_decision(tool: str) -> PolicyDecision:
    return PolicyDecision(
        tool=tool,
        rule_id="read-only-always",
        effect="allow",
        reason="test",
        evaluated_at=datetime.now(UTC),
    )


@pytest.fixture
def scenario_dir(repo_root: Path) -> Path:
    return repo_root / "fixtures" / "scenarios" / "real_regression"


@pytest.fixture
def gateway(scenario_dir: Path) -> ReplayToolGateway:
    return ReplayToolGateway(
        scenario_dir=scenario_dir,
        repo=REPO,
        forbidden=("merge_pull_request", "force_push", "delete_branch"),
    )


def test_repo_slug_matches_the_fixture_naming_rule() -> None:
    assert repo_slug(REPO) == "octo-org-harness-demo-repo"


async def test_get_job_logs_keeps_the_last_bytes(
    gateway: ReplayToolGateway, scenario_dir: Path
) -> None:
    """`content[-max_bytes:]`, never `f.read(max_bytes)`."""
    raw = (scenario_dir / "logs" / "job_601234567.txt").read_bytes()
    assert len(raw) > 65_536, "the fixture must be big enough for this to mean something"

    result = await gateway.invoke(
        ToolCall(
            call_id="tc_1", tool="get_job_logs",
            args={"job_id": 601234567, "max_bytes": 65_536},
        ),
        allow_decision("get_job_logs"),
    )

    assert result.ok
    assert result.data is not None
    content = result.data["content"]
    assert result.data["truncated"] is True

    # The tail survived...
    assert "assert 91 == 90" in content
    # ...and the head, which is what a wrong-direction truncation would have returned,
    # did not.
    assert "Current runner version" not in content
    assert content.encode("utf-8", errors="replace").endswith(
        raw[-64:].decode("utf-8", errors="replace").encode("utf-8")
    )


async def test_get_job_logs_untruncated_when_it_fits(gateway: ReplayToolGateway) -> None:
    result = await gateway.invoke(
        ToolCall(
            call_id="tc_2", tool="get_job_logs",
            args={"job_id": 601234567, "max_bytes": 50_000_000},
        ),
        allow_decision("get_job_logs"),
    )
    assert result.ok
    assert result.data is not None
    assert result.data["truncated"] is False
    assert "Current runner version" in result.data["content"]


async def test_missing_job_log_is_a_tool_error_not_an_exception(
    gateway: ReplayToolGateway,
) -> None:
    result = await gateway.invoke(
        ToolCall(call_id="tc_3", tool="get_job_logs", args={"job_id": 999}),
        allow_decision("get_job_logs"),
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == "not_found"


async def test_forbidden_tool_refused_even_when_the_decision_says_allow(
    gateway: ReplayToolGateway,
) -> None:
    """The pairing this check exists for: a hand-forged `allow` on a forbidden tool."""
    forged = PolicyDecision(
        tool="merge_pull_request",
        rule_id="totally-legitimate-rule",
        effect="allow",
        reason="a decision that never came from the engine",
        evaluated_at=datetime.now(UTC),
    )

    result = await gateway.invoke(
        ToolCall(call_id="tc_4", tool="merge_pull_request", args={"number": 1}), forged
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == "forbidden_by_policy"
    assert result.error.retryable is False


async def test_forbidden_refusal_reads_nothing(
    gateway: ReplayToolGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 4 of the contract: the refusal costs zero access to the recorded scenario."""
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("a forbidden tool must not touch the fixture directory")

    monkeypatch.setattr(Path, "read_text", explode)
    monkeypatch.setattr(Path, "read_bytes", explode)
    monkeypatch.setattr(Path, "is_file", explode)

    result = await gateway.invoke(
        ToolCall(call_id="tc_5", tool="delete_branch", args={"name": "main"}),
        allow_decision("delete_branch"),
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == "forbidden_by_policy"


async def test_recorded_api_responses_resolve(gateway: ReplayToolGateway) -> None:
    jobs = await gateway.invoke(
        ToolCall(
            call_id="tc_6", tool="list_workflow_run_jobs",
            args={"run_id": 501234567, "attempt": 1},
        ),
        allow_decision("list_workflow_run_jobs"),
    )
    assert jobs.ok
    assert jobs.data is not None
    assert jobs.data["jobs"][0]["id"] == 601234567

    compare = await gateway.invoke(
        ToolCall(
            call_id="tc_7", tool="compare_commits",
            args={
                "base": "8d4d0a89231f66f3b3910ad16e033041c898512d",
                "head": "e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df",
            },
        ),
        allow_decision("compare_commits"),
    )
    assert compare.ok
    assert compare.data is not None
    assert compare.data["files"][0]["filename"] == "src/pricing/discount.py"


async def test_missing_recording_is_not_found_not_an_empty_success(
    gateway: ReplayToolGateway,
) -> None:
    """A missing `api/` file is a fixture bug, and must not look like a cold start."""
    result = await gateway.invoke(
        ToolCall(call_id="tc_8", tool="get_commit", args={"sha": "deadbeef"}),
        allow_decision("get_commit"),
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == "not_found"


async def test_catalog_is_read_only_this_phase(gateway: ReplayToolGateway) -> None:
    catalog = gateway.catalog()
    assert {spec.side_effect for spec in catalog} == {"read"}
    assert all(spec.idempotent for spec in catalog)


def test_forbidden_has_no_default_and_must_be_passed_explicitly(scenario_dir: Path) -> None:
    """review.md finding 12: `forbidden` used to default to `()`, making the gateway's
    own copy of the authoritative safety re-check opt-in at construction — a caller that
    forgot the keyword silently refused nothing. It is now required, with no default, so
    a caller that forgets it gets a `TypeError` at construction rather than a gateway
    that runs unguarded.
    """
    with pytest.raises(TypeError):
        ReplayToolGateway(scenario_dir=scenario_dir, repo=REPO)  # type: ignore[call-arg]

    # The stated-decision escape hatch still works: an explicit empty tuple is a real
    # choice, not an accident, and construction must still succeed.
    unguarded = ReplayToolGateway(scenario_dir=scenario_dir, repo=REPO, forbidden=())
    assert unguarded.forbidden == frozenset()
