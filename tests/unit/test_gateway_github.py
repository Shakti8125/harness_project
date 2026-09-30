# ruff: noqa: E501
"""`GitHubToolGateway` against PLAN.md Appendix B.2, with every request mocked.

Each test names the row it pins. The sleep is injected so a "sleep to the reset" is
observed as a recorded duration, not waited for.
"""

from __future__ import annotations

import io
import time
import zipfile
from datetime import UTC, datetime

import httpx
import pytest
import respx

from src.harness.gateway import ToolCall
from src.harness.guardrails import PolicyDecision
from src.integrations.cicd.gateway_github import (
    LOG_DOWNLOAD_CAP_BYTES,
    SERVER_ERROR_RETRIES,
    TIMEOUT_RETRIES,
    GitHubToolGateway,
)
from src.integrations.cicd.wiring import load_forbidden

REPO = "octo-org/harness-demo-repo"
API = f"https://api.github.com/repos/{REPO}"


class Sleeps:
    def __init__(self) -> None:
        self.durations: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)


def allow(tool: str) -> PolicyDecision:
    return PolicyDecision(
        tool=tool, rule_id="read-only-always", effect="allow", reason="test",
        evaluated_at=datetime.now(UTC),
    )


def call(tool: str, **args: object) -> ToolCall:
    return ToolCall(call_id="tc_0000000000ab", tool=tool, args=dict(args))  # type: ignore[arg-type]


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
async def gateway(sleeps: Sleeps):  # type: ignore[no-untyped-def]
    gw = GitHubToolGateway(
        repo=REPO, token="ghp_" + "x" * 36, forbidden=load_forbidden(), dry_run=True, sleep=sleeps
    )
    yield gw
    await gw.aclose()


@pytest.fixture
async def live_gateway(sleeps: Sleeps):  # type: ignore[no-untyped-def]
    gw = GitHubToolGateway(
        repo=REPO, token="ghp_" + "x" * 36, forbidden=load_forbidden(), dry_run=False, sleep=sleeps
    )
    yield gw
    await gw.aclose()


# ---------------------------------------------------------------------------
# Read tools, happy path
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_workflow_run_jobs(gateway: GitHubToolGateway, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{API}/actions/runs/501/attempts/2/jobs").mock(
        return_value=httpx.Response(200, json={"total_count": 1, "jobs": [{"id": 9}]})
    )
    result = await gateway.invoke(call("list_workflow_run_jobs", run_id=501, attempt=2), allow("list_workflow_run_jobs"))
    assert result.ok and result.data == {"total_count": 1, "jobs": [{"id": 9}]}
    assert route.call_count == 1
    sent = route.calls[0].request
    assert sent.headers["authorization"].startswith("Bearer ")
    assert sent.headers["x-github-api-version"] == "2022-11-28"
    # Phase 3 audit finding 5: the rerun probe judges "every job passed" from this list,
    # so it asks for GitHub's largest page rather than the 30-job default.
    assert sent.url.params["per_page"] == "100"


@respx.mock
async def test_find_last_successful_run_drops_the_failing_head(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{API}/actions/workflows/9001/runs").mock(
        return_value=httpx.Response(
            200,
            json={"total_count": 2, "workflow_runs": [
                {"id": 2, "head_sha": "head"}, {"id": 1, "head_sha": "green"},
            ]},
        )
    )
    result = await gateway.invoke(
        call("find_last_successful_run", workflow_id=9001, branch="main", before="head"),
        allow("find_last_successful_run"),
    )
    assert result.ok and result.data is not None
    assert result.data["workflow_runs"] == [{"id": 1, "head_sha": "green"}]
    assert result.data["total_count"] == 1
    sent = respx_mock.calls[0].request
    assert sent.url.params["status"] == "success"
    assert sent.url.params["branch"] == "main"


# ---------------------------------------------------------------------------
# B.2 rows
# ---------------------------------------------------------------------------


@respx.mock
async def test_timeout_retries_twice_then_reports_timeout(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = respx_mock.get(f"{API}/commits/abc1234").mock(side_effect=httpx.ReadTimeout("slow"))
    result = await gateway.invoke(call("get_commit", sha="abc1234"), allow("get_commit"))
    assert not result.ok and result.error is not None
    assert result.error.kind == "timeout"
    assert result.error.retryable is True
    assert route.call_count == TIMEOUT_RETRIES + 1
    assert len(sleeps.durations) == TIMEOUT_RETRIES


@respx.mock
async def test_primary_rate_limit_sleeps_to_the_reset_and_retries_once(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter, sleeps: Sleeps
) -> None:
    reset = int(time.time()) + 30
    route = respx_mock.get(f"{API}/commits/abc1234").mock(
        side_effect=[
            httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}, json={"message": "API rate limit exceeded"}),
            httpx.Response(200, json={"sha": "abc1234"}),
        ]
    )
    result = await gateway.invoke(call("get_commit", sha="abc1234"), allow("get_commit"))
    assert result.ok and result.data == {"sha": "abc1234"}
    assert route.call_count == 2
    assert len(sleeps.durations) == 1
    assert 25 <= sleeps.durations[0] <= 31


@respx.mock
async def test_primary_rate_limit_beyond_60s_is_reported_not_slept(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter, sleeps: Sleeps
) -> None:
    reset = int(time.time()) + 600
    route = respx_mock.get(f"{API}/commits/abc1234").mock(
        return_value=httpx.Response(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}, json={}
        )
    )
    result = await gateway.invoke(call("get_commit", sha="abc1234"), allow("get_commit"))
    assert not result.ok and result.error is not None
    assert result.error.kind == "rate_limited"
    assert result.error.retry_after_s is not None and result.error.retry_after_s > 60
    assert result.error.http_status == 403
    assert route.call_count == 1
    assert sleeps.durations == []


@respx.mock
async def test_secondary_limit_honours_retry_after_up_to_twice(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = respx_mock.get(f"{API}/commits/abc1234").mock(
        return_value=httpx.Response(403, headers={"retry-after": "7"}, json={"message": "abuse"})
    )
    result = await gateway.invoke(call("get_commit", sha="abc1234"), allow("get_commit"))
    assert not result.ok and result.error is not None
    assert result.error.kind == "rate_limited"
    assert result.error.retry_after_s == 7
    assert route.call_count == 3
    assert sleeps.durations == [7.0, 7.0]


@respx.mock
async def test_401_is_auth_with_no_retry(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = respx_mock.get(f"{API}/commits/abc1234").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )
    result = await gateway.invoke(call("get_commit", sha="abc1234"), allow("get_commit"))
    assert not result.ok and result.error is not None
    assert result.error.kind == "auth"
    assert result.error.retryable is False
    assert route.call_count == 1
    assert sleeps.durations == []
    assert "ghp_" not in result.error.message


@respx.mock
async def test_403_scope_is_auth_naming_the_accepted_scopes(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{API}/commits/abc1234").mock(
        return_value=httpx.Response(
            403,
            headers={"x-accepted-oauth-scopes": "repo"},
            json={"message": "Resource not accessible by personal access token"},
        )
    )
    result = await gateway.invoke(call("get_commit", sha="abc1234"), allow("get_commit"))
    assert not result.ok and result.error is not None
    assert result.error.kind == "auth"
    assert "repo" in result.error.message
    assert "not accessible" in result.error.message


@respx.mock
async def test_404_on_a_read_is_not_found_data(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{API}/compare/aaaaaaa...bbbbbbb").mock(return_value=httpx.Response(404, json={}))
    result = await gateway.invoke(call("compare_commits", base="aaaaaaa", head="bbbbbbb"), allow("compare_commits"))
    assert not result.ok and result.error is not None
    assert result.error.kind == "not_found"
    assert result.error.http_status == 404


@respx.mock
async def test_5xx_retries_three_times_with_backoff(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = respx_mock.get(f"{API}/commits/abc1234").mock(return_value=httpx.Response(502))
    result = await gateway.invoke(call("get_commit", sha="abc1234"), allow("get_commit"))
    assert not result.ok and result.error is not None
    assert result.error.kind == "upstream_5xx"
    assert result.error.retryable is True
    assert route.call_count == SERVER_ERROR_RETRIES + 1
    assert len(sleeps.durations) == SERVER_ERROR_RETRIES
    assert all(0 <= d <= 8.0 for d in sleeps.durations)


@respx.mock
async def test_malformed_body_keeps_bounded_evidence(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{API}/commits/abc1234").mock(
        return_value=httpx.Response(200, content=b"<html>" + b"x" * 5000)
    )
    result = await gateway.invoke(call("get_commit", sha="abc1234"), allow("get_commit"))
    assert not result.ok and result.error is not None
    assert result.error.kind == "malformed"
    assert len(result.error.message) < 700


@respx.mock
async def test_log_redirect_is_followed_once_without_the_token_and_the_tail_is_kept(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{API}/actions/jobs/77/logs").mock(
        return_value=httpx.Response(302, headers={"location": "https://blob.example/logs/77?sig=abc"})
    )
    blob = respx_mock.get("https://blob.example/logs/77").mock(
        return_value=httpx.Response(200, content=b"A" * 100 + b"THE END")
    )
    result = await gateway.invoke(call("get_job_logs", job_id=77, max_bytes=20), allow("get_job_logs"))
    assert result.ok and result.data is not None
    assert result.data["content"] == "A" * 13 + "THE END"
    assert result.data["head_dropped"] is True
    assert result.data["total_bytes"] == 107
    assert "authorization" not in blob.calls[0].request.headers


@respx.mock
async def test_zip_log_is_extracted_and_a_corrupt_one_is_malformed(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("1_Set up job.txt", "first\n")
        archive.writestr("2_Run tests.txt", "second\n")
    respx_mock.get(f"{API}/actions/jobs/78/logs").mock(
        return_value=httpx.Response(200, content=buffer.getvalue())
    )
    result = await gateway.invoke(call("get_job_logs", job_id=78), allow("get_job_logs"))
    assert result.ok and result.data is not None
    assert result.data["content"] == "first\n\nsecond\n"

    respx_mock.get(f"{API}/actions/jobs/79/logs").mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04garbage")
    )
    corrupt = await gateway.invoke(call("get_job_logs", job_id=79), allow("get_job_logs"))
    assert not corrupt.ok and corrupt.error is not None and corrupt.error.kind == "malformed"


def test_log_cap_is_twenty_megabytes() -> None:
    assert LOG_DOWNLOAD_CAP_BYTES == 20 * 1024 * 1024


# ---------------------------------------------------------------------------
# rerun_failed_jobs (Appendix C)
# ---------------------------------------------------------------------------


@respx.mock
async def test_rerun_dry_run_reads_but_never_posts(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    lookup = respx_mock.get(f"{API}/actions/runs/501").mock(
        return_value=httpx.Response(200, json={"id": 501, "run_attempt": 1})
    )
    post = respx_mock.post(f"{API}/actions/runs/501/rerun-failed-jobs").mock(
        return_value=httpx.Response(201)
    )
    result = await gateway.invoke(call("rerun_failed_jobs", run_id=501, attempt=1), allow("rerun_failed_jobs"))
    assert result.ok and result.dry_run is True
    assert result.data is not None and result.data["rerun_requested"] is False
    assert lookup.call_count == 1
    assert post.call_count == 0


@respx.mock
async def test_rerun_live_posts_once_and_caches_by_idempotency_key(
    live_gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{API}/actions/runs/501").mock(
        return_value=httpx.Response(200, json={"id": 501, "run_attempt": 1})
    )
    post = respx_mock.post(f"{API}/actions/runs/501/rerun-failed-jobs").mock(
        return_value=httpx.Response(201)
    )
    keyed = ToolCall(
        call_id="tc_0000000000ab", tool="rerun_failed_jobs", args={"run_id": 501, "attempt": 1},
        idempotency_key="run_x:rerun_failed_jobs:deadbeef",
    )
    first = await live_gateway.invoke(keyed, allow("rerun_failed_jobs"))
    assert first.ok and first.dry_run is False and first.cached is False
    assert first.data is not None and first.data["rerun_requested"] is True

    again = await live_gateway.invoke(
        keyed.model_copy(update={"call_id": "tc_0000000000cd"}), allow("rerun_failed_jobs")
    )
    assert again.ok and again.cached is True
    assert again.call_id == "tc_0000000000cd"
    assert post.call_count == 1


@respx.mock
async def test_rerun_already_in_progress_is_success(
    live_gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{API}/actions/runs/501").mock(
        return_value=httpx.Response(200, json={"id": 501, "run_attempt": 1})
    )
    respx_mock.post(f"{API}/actions/runs/501/rerun-failed-jobs").mock(
        return_value=httpx.Response(
            403, json={"message": "This workflow is already in progress and cannot be re-run"}
        )
    )
    result = await live_gateway.invoke(call("rerun_failed_jobs", run_id=501, attempt=1), allow("rerun_failed_jobs"))
    assert result.ok and result.cached is True
    assert result.data is not None and result.data["already_in_progress"] is True


@respx.mock
async def test_rerun_no_ops_when_the_attempt_already_advanced(
    live_gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{API}/actions/runs/501").mock(
        return_value=httpx.Response(200, json={"id": 501, "run_attempt": 2})
    )
    post = respx_mock.post(f"{API}/actions/runs/501/rerun-failed-jobs").mock(
        return_value=httpx.Response(201)
    )
    result = await live_gateway.invoke(call("rerun_failed_jobs", run_id=501, attempt=1), allow("rerun_failed_jobs"))
    assert result.ok and result.cached is True
    assert result.data is not None and result.data["already_advanced"] is True
    assert post.call_count == 0


@respx.mock
async def test_unimplemented_write_tools_answer_a_tool_error_not_a_raise(
    live_gateway: GitHubToolGateway, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Since Phase 5 every catalog write has a body, so the not-implemented path is
    exercised by narrowing `IMPLEMENTED_WRITE_TOOLS` for the test: a catalog write tool
    without a body still answers a `ToolError`, never raises, and sends nothing."""
    from src.integrations.cicd import gateway_github

    monkeypatch.setattr(
        gateway_github, "IMPLEMENTED_WRITE_TOOLS", frozenset({"rerun_failed_jobs"})
    )
    catch_all = respx_mock.route(host="api.github.com").mock(return_value=httpx.Response(200, json={}))
    for tool in ("create_branch", "create_or_update_file", "open_pull_request", "create_issue"):
        result = await live_gateway.invoke(call(tool), allow(tool))
        assert not result.ok and result.error is not None
        assert result.error.kind == "unknown"
        assert result.error.retryable is False
        assert tool in result.error.message
    assert catch_all.call_count == 0


@respx.mock
async def test_unknown_tool_is_invalid_args(gateway: GitHubToolGateway, respx_mock: respx.MockRouter) -> None:
    result = await gateway.invoke(call("teleport"), allow("teleport"))
    assert not result.ok and result.error is not None and result.error.kind == "invalid_args"
    assert len(respx_mock.calls) == 0


@respx.mock
async def test_missing_argument_is_invalid_args(gateway: GitHubToolGateway, respx_mock: respx.MockRouter) -> None:
    result = await gateway.invoke(call("get_commit"), allow("get_commit"))
    assert not result.ok and result.error is not None and result.error.kind == "invalid_args"
    assert len(respx_mock.calls) == 0


# ---------------------------------------------------------------------------
# Review findings 1 and 4 (Phase 2 audit)
# ---------------------------------------------------------------------------


@respx.mock
async def test_a_transport_error_on_the_log_blob_host_is_returned_not_raised(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    """Finding 1: `invoke` never raises for a remote failure -- the blob host included."""
    respx_mock.get(f"{API}/actions/jobs/77/logs").mock(
        return_value=httpx.Response(302, headers={"location": "https://blob.example/logs/77"})
    )
    respx_mock.get("https://blob.example/logs/77").mock(side_effect=httpx.ConnectError("reset"))
    result = await gateway.invoke(call("get_job_logs", job_id=77), allow("get_job_logs"))
    assert not result.ok and result.error is not None
    assert result.error.kind == "unknown"
    assert result.error.retryable is True
    assert "ConnectError" in result.error.message


@respx.mock
async def test_a_malformed_rate_limit_reset_is_still_a_rate_limit(
    gateway: GitHubToolGateway, respx_mock: respx.MockRouter, sleeps: Sleeps
) -> None:
    """Finding 4: a non-numeric `x-ratelimit-reset` was classified `invalid_args`."""
    route = respx_mock.get(f"{API}/commits/abc1234").mock(
        side_effect=[
            httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "soon"}, json={}),
            httpx.Response(200, json={"sha": "abc1234"}),
        ]
    )
    result = await gateway.invoke(call("get_commit", sha="abc1234"), allow("get_commit"))
    assert result.ok
    assert route.call_count == 2
    assert sleeps.durations == [60.0]


@respx.mock
async def test_rerun_already_running_phrasings_all_count_as_success(
    live_gateway: GitHubToolGateway, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{API}/actions/runs/501").mock(
        return_value=httpx.Response(200, json={"id": 501, "run_attempt": 1})
    )
    respx_mock.post(f"{API}/actions/runs/501/rerun-failed-jobs").mock(
        return_value=httpx.Response(403, json={"message": "This workflow is already running"})
    )
    result = await live_gateway.invoke(
        call("rerun_failed_jobs", run_id=501, attempt=1), allow("rerun_failed_jobs")
    )
    assert result.ok and result.cached is True
