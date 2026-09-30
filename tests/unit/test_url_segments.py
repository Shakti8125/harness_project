# ruff: noqa: E501
"""SEC-03 and SEC-24: a model- or caller-supplied path, sha or ref never escapes the repo.

httpx resolves dot segments before it sends, so `contents/../../../user` used to go out
as `GET https://api.github.com/repos/user` with the token attached, and a `?` inside a
path became a real query parameter. Each refusal below must happen before any request:
in dry run the write tools' pre-check `GET`s still carry the token. `respx_mock` fails any
request nobody mocked, and each refusal test also asserts that nothing was sent.

The replay gateway turns the same values into file names under a scenario directory, and
on Windows `x/../../../real_regression/webhook` read another scenario's JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from src.harness.gateway import ToolCall
from src.harness.guardrails import PolicyDecision
from src.integrations.cicd.gateway_github import GitHubToolGateway
from src.integrations.cicd.gateway_replay import ReplayToolGateway
from src.integrations.cicd.wiring import load_forbidden

REPO = "octo-org/harness-demo-repo"
API = f"https://api.github.com/repos/{REPO}"
AGENT_BRANCH = "agent/fix/abcd1234"


def allow(tool: str) -> PolicyDecision:
    return PolicyDecision(
        tool=tool, rule_id="test", effect="allow", reason="test", evaluated_at=datetime.now(UTC)
    )


def call(tool: str, **args: object) -> ToolCall:
    return ToolCall(
        call_id="tc_0000000000ab", tool=tool, args=dict(args),  # type: ignore[arg-type]
        idempotency_key=None if tool in ("get_commit", "compare_commits", "get_file_contents") else "run:k",
    )


@pytest.fixture
async def gateway():  # type: ignore[no-untyped-def]
    gw = GitHubToolGateway(repo=REPO, token="ghp_" + "x" * 36, forbidden=load_forbidden(), dry_run=True)
    yield gw
    await gw.aclose()


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_file_contents", {"path": "../../../user", "ref": "main"}),
        ("get_file_contents", {"path": "x?ref=evil", "ref": "main"}),
        ("get_file_contents", {"path": "a/../../b", "ref": "main"}),
        ("get_file_contents", {"path": "src/app.py#frag", "ref": "main"}),
        ("get_file_contents", {"path": "src\\..\\..\\x", "ref": "main"}),
        ("get_file_contents", {"path": "src/app.py", "ref": "main?x=1"}),
        # The SEC-02 reproduction: a caller's head_sha reached `commits/{sha}`.
        ("get_commit", {"sha": "../../../victim/private/commits/main"}),
        ("compare_commits", {"base": "a" * 40, "head": "../../../victim/private"}),
        ("compare_commits", {"base": "main..evil", "head": "b" * 40}),
        ("create_or_update_file", {"path": "../../../x", "branch": AGENT_BRANCH, "content_b64": "eA==", "message": "m"}),
        ("create_or_update_file", {"path": "src/x.py", "branch": "../../x", "content_b64": "eA==", "message": "m"}),
        ("create_branch", {"name": "agent/fix/../../x", "from_sha": "a" * 40}),
    ],
)
async def test_a_segment_that_leaves_the_repository_is_refused_before_any_request(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter, tool: str, args: dict[str, object]
) -> None:
    result = await gateway.invoke(call(tool, **args), allow(tool))

    assert not result.ok and result.error is not None
    assert result.error.kind == "invalid_args", result.error
    assert len(respx_mock.calls) == 0


async def test_a_file_write_outside_the_agent_branch_is_refused_before_the_pre_check(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    """SEC-05's gateway half: `branch: "main"` would have committed straight to main."""
    result = await gateway.invoke(
        call("create_or_update_file", path="src/x.py", branch="main", content_b64="eA==", message="m"),
        allow("create_or_update_file"),
    )

    assert not result.ok and result.error is not None
    assert result.error.kind == "invalid_args"
    assert len(respx_mock.calls) == 0


async def test_a_legitimate_path_still_resolves(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(f"{API}/contents/src/app/pricing.py").mock(
        return_value=httpx.Response(200, json={"sha": "f" * 40, "path": "src/app/pricing.py"})
    )

    result = await gateway.invoke(
        call("get_file_contents", path="src/app/pricing.py", ref="main"), allow("get_file_contents")
    )

    assert result.ok, result.error
    assert route.called
    assert route.calls.last.request.url.params["ref"] == "main"


async def test_a_path_with_a_space_is_percent_encoded(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(url__regex=rf"{API}/contents/.*").mock(
        return_value=httpx.Response(200, json={"sha": "f" * 40})
    )

    result = await gateway.invoke(
        call("get_file_contents", path="docs/my notes.md", ref="main"), allow("get_file_contents")
    )

    assert result.ok, result.error
    assert route.calls.last.request.url.raw_path.startswith(
        b"/repos/octo-org/harness-demo-repo/contents/docs/my%20notes.md"
    )


async def test_the_agent_branch_reaches_the_ref_lookup_intact(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    ref = respx_mock.get(f"{API}/git/ref/heads/{AGENT_BRANCH}").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    result = await gateway.invoke(
        call("create_branch", name=AGENT_BRANCH, from_sha="a" * 40), allow("create_branch")
    )

    assert result.ok and result.dry_run, result.error
    assert ref.called


async def test_the_replay_gateway_refuses_a_sha_that_walks_the_fixture_tree(repo_root: Path) -> None:
    gateway = ReplayToolGateway(
        scenario_dir=repo_root / "fixtures/scenarios/flaky_test", repo=REPO, forbidden=()
    )

    for tool, args in (
        ("get_commit", {"sha": "x/../../../real_regression/webhook"}),
        ("get_commit", {"sha": "x\\..\\..\\..\\real_regression\\webhook"}),
        ("compare_commits", {"base": "../..", "head": "a" * 40}),
        ("get_file_contents", {"path": "..\\..\\real_regression\\webhook", "ref": "main"}),
    ):
        result = await gateway.invoke(call(tool, **args), allow(tool))
        assert not result.ok and result.error is not None
        assert result.error.kind == "invalid_args", (tool, args, result.error)


def test_the_rules() -> None:
    from src.integrations.cicd import url_segments

    assert url_segments.sha("e2cdf1b") == "e2cdf1b"
    for bad in ("E2CDF1B", "e2cdf1", "g" * 40, "a" * 41, ""):
        with pytest.raises(ValueError):
            url_segments.sha(bad)
    assert url_segments.ref_path("agent/fix/ab#1") == "agent/fix/ab%231"
    for bad in ("", "/main", "main/", "a//b", "a..b", "x.lock", "a/.hidden", "@", "a@{1}",
                "sp ace", "t\tab", "a~1", "a^", "a:b", "a?", "a*", "a[b", "a\\b", "end."):
        with pytest.raises(ValueError):
            url_segments.ref_name(bad)
    assert url_segments.file_path("/src/app.py") == "src/app.py"
    assert url_segments.file_url_path("a b/c+d.py") == "a%20b/c%2Bd.py"
    for bad in ("", "/", "a//b", "a/./b", "a/../b", "..", "a\\b", "a?b", "a#b", "a%2e", "a\nb"):
        with pytest.raises(ValueError):
            url_segments.file_path(bad)


# ---------------------------------------------------------------------------
# The Stage 1e audit's findings 2 and 3 (docs/progress/phase-5/review-security.md)
# ---------------------------------------------------------------------------


async def test_the_replay_gateway_refuses_every_file_name_part_that_walks_the_tree(repo_root: Path) -> None:
    """Finding 2: only the sha and path were checked; a branch or a string id still walked."""
    gateway = ReplayToolGateway(
        scenario_dir=repo_root / "fixtures/scenarios/flaky_test", repo=REPO, forbidden=()
    )

    for tool, args in (
        ("find_last_successful_run", {"workflow_id": 9001, "branch": "x\\..\\..\\..\\real_regression\\webhook"}),
        ("find_last_successful_run", {"workflow_id": "9001-runs\\..\\..\\..\\real_regression\\webhook", "branch": "main"}),
        ("list_workflow_run_jobs", {"run_id": "x\\..\\..\\..\\real_regression\\api\\GET_repos-octo-org-harness-demo-repo-actions-runs-501234567", "attempt": 1}),
        ("list_workflow_run_jobs", {"run_id": 501234890, "attempt": "1-jobs\\..\\..\\x"}),
    ):
        result = await gateway.invoke(call(tool, **args), allow(tool))
        assert not result.ok and result.error is not None
        assert result.error.kind == "invalid_args", (tool, args, result.error)

    walked = ReplayToolGateway(
        scenario_dir=repo_root / "fixtures/scenarios/flaky_test",
        repo="x\\..\\..\\..\\real_regression\\webhook/y", forbidden=(),
    )
    result = await walked.invoke(call("get_commit", sha="a" * 40), allow("get_commit"))
    assert not result.ok and result.error is not None and result.error.kind == "invalid_args"


async def test_the_replay_gateway_still_serves_its_own_recordings(repo_root: Path) -> None:
    gateway = ReplayToolGateway(
        scenario_dir=repo_root / "fixtures/scenarios/flaky_test", repo=REPO, forbidden=()
    )

    result = await gateway.invoke(
        call("find_last_successful_run", workflow_id=9001, branch="main"), allow("find_last_successful_run")
    )

    assert result.ok, result.error


async def test_a_sha_with_a_trailing_newline_is_invalid_args_not_a_crash(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    """Finding 3: `$` matched before a trailing newline, and httpx then raised out of invoke."""
    result = await gateway.invoke(call("get_commit", sha="abcdef1\n"), allow("get_commit"))

    assert not result.ok and result.error is not None
    assert result.error.kind == "invalid_args"
    assert len(respx_mock.calls) == 0
