# ruff: noqa: E501
"""`GitHubToolGateway`'s PR-writing and issue tools (PLAN.md Phase 5; Appendix C's
action-level idempotency; Appendix E's dry run), every request mocked.

Each test names the rule it pins. `dry_run=False` is set explicitly where a write is
supposed to happen -- the constructor default is true, and that default is itself under
test in the dry-run cases.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import httpx
import pytest
import respx

from src.harness.gateway import ToolCall
from src.harness.guardrails import PolicyDecision
from src.integrations.cicd.gateway_github import ISSUE_SIGNATURE_MARKER, GitHubToolGateway
from src.integrations.cicd.wiring import load_forbidden

REPO = "octo-org/harness-demo-repo"
API = f"https://api.github.com/repos/{REPO}"
BRANCH = "agent/fix/e2cdf1b4"
HEAD = "e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df"


def decision(tool: str) -> PolicyDecision:
    return PolicyDecision(
        tool=tool, rule_id="open-fix-pr", effect="require_approval", reason="test",
        evaluated_at=datetime.now(UTC),
    )


def call(tool: str, key: str | None = "run:tool:1", **args: object) -> ToolCall:
    return ToolCall(call_id="tc_0000000000ab", tool=tool, args=dict(args), idempotency_key=key)  # type: ignore[arg-type]


async def noop_sleep(_: float) -> None:
    return None


def gateway(*, dry_run: bool) -> GitHubToolGateway:
    return GitHubToolGateway(
        repo=REPO, token="ghp_" + "x" * 36, forbidden=load_forbidden(), dry_run=dry_run, sleep=noop_sleep,
    )


# ---------------------------------------------------------------------------
# create_branch
# ---------------------------------------------------------------------------


@respx.mock
async def test_create_branch_creates_when_absent(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/git/ref/heads/{BRANCH}").mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    post = respx_mock.post(f"{API}/git/refs").mock(
        return_value=httpx.Response(201, json={"ref": f"refs/heads/{BRANCH}", "object": {"sha": HEAD}})
    )
    gw = gateway(dry_run=False)
    result = await gw.invoke(call("create_branch", name=BRANCH, from_sha=HEAD), decision("create_branch"))
    assert result.ok and not result.cached and not result.dry_run
    assert result.data == {"ref": f"refs/heads/{BRANCH}", "sha": HEAD, "created": True, "dry_run": False}
    assert post.call_count == 1
    import json

    assert json.loads(post.calls[0].request.content) == {"ref": f"refs/heads/{BRANCH}", "sha": HEAD}
    await gw.aclose()


@respx.mock
async def test_create_branch_returns_an_existing_branch_as_cached(respx_mock: respx.MockRouter) -> None:
    """Appendix C: an existing branch is fetched and returned, `cached=True`, nothing written."""
    respx_mock.get(f"{API}/git/ref/heads/{BRANCH}").mock(
        return_value=httpx.Response(200, json={"ref": f"refs/heads/{BRANCH}", "object": {"sha": "abc123"}})
    )
    post = respx_mock.post(f"{API}/git/refs").mock(return_value=httpx.Response(201, json={}))
    gw = gateway(dry_run=False)
    result = await gw.invoke(call("create_branch", name=BRANCH, from_sha=HEAD), decision("create_branch"))
    assert result.ok and result.cached is True
    assert result.data is not None and result.data["already_exists"] is True and result.data["sha"] == "abc123"
    assert post.call_count == 0
    await gw.aclose()


@respx.mock
async def test_create_branch_treats_422_reference_already_exists_as_success(respx_mock: respx.MockRouter) -> None:
    """B.2 / Appendix C: a race between the pre-check and the write is still success."""
    ref = respx_mock.get(f"{API}/git/ref/heads/{BRANCH}")
    ref.side_effect = [
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(200, json={"ref": f"refs/heads/{BRANCH}", "object": {"sha": HEAD}}),
    ]
    respx_mock.post(f"{API}/git/refs").mock(
        return_value=httpx.Response(422, json={"message": "Reference already exists"})
    )
    gw = gateway(dry_run=False)
    result = await gw.invoke(call("create_branch", name=BRANCH, from_sha=HEAD), decision("create_branch"))
    assert result.ok and result.cached is True
    assert result.data is not None and result.data["sha"] == HEAD
    await gw.aclose()


@respx.mock
async def test_create_branch_dry_run_pre_checks_then_sends_nothing(respx_mock: respx.MockRouter) -> None:
    """Appendix E: a dry run performs the reads and returns `would_have`."""
    probe = respx_mock.get(f"{API}/git/ref/heads/{BRANCH}").mock(return_value=httpx.Response(404, json={}))
    post = respx_mock.post(f"{API}/git/refs").mock(return_value=httpx.Response(201, json={}))
    gw = gateway(dry_run=True)
    result = await gw.invoke(call("create_branch", name=BRANCH, from_sha=HEAD), decision("create_branch"))
    assert result.ok and result.dry_run is True
    assert result.data is not None and result.data["would_have"] == {"name": BRANCH, "from_sha": HEAD}
    assert probe.call_count == 1 and post.call_count == 0
    await gw.aclose()


@respx.mock
async def test_a_404_on_a_write_is_a_tool_failure_not_data(respx_mock: respx.MockRouter) -> None:
    """B.2: 404 on a write tool -> `not_found` error -> the run escalates `tool_failure`
    (the caller's job); here: the result is an error, not a success with empty data."""
    respx_mock.get(f"{API}/git/ref/heads/{BRANCH}").mock(return_value=httpx.Response(404, json={}))
    respx_mock.post(f"{API}/git/refs").mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    gw = gateway(dry_run=False)
    result = await gw.invoke(call("create_branch", name=BRANCH, from_sha=HEAD), decision("create_branch"))
    assert not result.ok and result.error is not None
    assert result.error.kind == "not_found" and result.error.http_status == 404
    await gw.aclose()


# ---------------------------------------------------------------------------
# create_or_update_file
# ---------------------------------------------------------------------------

CONTENT = base64.b64encode(b"def discount(): ...\n").decode()


@respx.mock
async def test_update_file_passes_the_current_blob_sha(respx_mock: respx.MockRouter) -> None:
    """Appendix C: the current sha rides on the PUT so a concurrent writer is detected."""
    respx_mock.get(f"{API}/contents/src/pricing/discount.py", params={"ref": BRANCH}).mock(
        return_value=httpx.Response(200, json={"sha": "blob0001", "path": "src/pricing/discount.py"})
    )
    put = respx_mock.put(f"{API}/contents/src/pricing/discount.py").mock(
        return_value=httpx.Response(200, json={"commit": {"sha": "c0mm1t"}, "content": {"sha": "blob0002"}})
    )
    gw = gateway(dry_run=False)
    result = await gw.invoke(
        call("create_or_update_file", branch=BRANCH, path="src/pricing/discount.py", content_b64=CONTENT, message="Fix"),
        decision("create_or_update_file"),
    )
    assert result.ok and not result.dry_run
    assert result.data == {
        "path": "src/pricing/discount.py", "branch": BRANCH, "commit_sha": "c0mm1t",
        "content_sha": "blob0002", "written": True, "dry_run": False,
    }
    import json

    sent = json.loads(put.calls[0].request.content)
    assert sent == {"message": "Fix", "content": CONTENT, "branch": BRANCH, "sha": "blob0001"}
    await gw.aclose()


@respx.mock
async def test_create_file_omits_the_sha_for_a_new_file(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/contents/new.py", params={"ref": BRANCH}).mock(return_value=httpx.Response(404, json={}))
    put = respx_mock.put(f"{API}/contents/new.py").mock(
        return_value=httpx.Response(201, json={"commit": {"sha": "c0mm1t"}, "content": {"sha": "blobnew"}})
    )
    gw = gateway(dry_run=False)
    result = await gw.invoke(
        call("create_or_update_file", branch=BRANCH, path="new.py", content_b64=CONTENT, message="Add"),
        decision("create_or_update_file"),
    )
    assert result.ok
    import json

    assert "sha" not in json.loads(put.calls[0].request.content)
    await gw.aclose()


@respx.mock
async def test_update_file_409_is_a_hard_stop(respx_mock: respx.MockRouter) -> None:
    """Appendix C: a 409 means someone else wrote it -- do not clobber, do not retry."""
    respx_mock.get(f"{API}/contents/x.py", params={"ref": BRANCH}).mock(
        return_value=httpx.Response(200, json={"sha": "stale"})
    )
    put = respx_mock.put(f"{API}/contents/x.py").mock(
        return_value=httpx.Response(409, json={"message": "x.py does not match stale"})
    )
    gw = gateway(dry_run=False)
    result = await gw.invoke(
        call("create_or_update_file", branch=BRANCH, path="x.py", content_b64=CONTENT, message="m"),
        decision("create_or_update_file"),
    )
    assert not result.ok and result.error is not None
    assert result.error.http_status == 409 and result.error.retryable is False
    assert "not clobbering" in result.error.message
    assert put.call_count == 1, "no retry on a conflict"
    await gw.aclose()


@respx.mock
async def test_update_file_dry_run_never_echoes_the_content(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/contents/x.py", params={"ref": BRANCH}).mock(return_value=httpx.Response(200, json={"sha": "blob1"}))
    put = respx_mock.put(f"{API}/contents/x.py").mock(return_value=httpx.Response(200, json={}))
    gw = gateway(dry_run=True)
    result = await gw.invoke(
        call("create_or_update_file", branch=BRANCH, path="x.py", content_b64=CONTENT, message="m"),
        decision("create_or_update_file"),
    )
    assert result.ok and result.dry_run
    assert result.data is not None
    assert result.data["current_sha"] == "blob1"
    assert "content_b64" not in result.data["would_have"]
    assert result.data["would_have"]["content_bytes"] == len(CONTENT)
    assert CONTENT not in str(result.data)
    assert put.call_count == 0
    await gw.aclose()


# ---------------------------------------------------------------------------
# open_pull_request
# ---------------------------------------------------------------------------


def _pull(number: int = 7) -> dict[str, object]:
    return {
        "number": number, "html_url": f"https://github.com/{REPO}/pull/{number}", "state": "open",
        "draft": True, "head": {"ref": BRANCH}, "base": {"ref": "main"},
    }


@respx.mock
async def test_open_pull_request_returns_the_open_pr_for_the_same_head(respx_mock: respx.MockRouter) -> None:
    """Appendix C: never two PRs for one signature."""
    listing = respx_mock.get(f"{API}/pulls").mock(return_value=httpx.Response(200, json=[_pull(7)]))
    post = respx_mock.post(f"{API}/pulls").mock(return_value=httpx.Response(201, json=_pull(8)))
    gw = gateway(dry_run=False)
    result = await gw.invoke(
        call("open_pull_request", head=BRANCH, base="main", title="t", body="b", draft=True, labels=["agent-generated"]),
        decision("open_pull_request"),
    )
    assert result.ok and result.cached is True
    assert result.data is not None and result.data["number"] == 7 and result.data["already_exists"] is True
    assert post.call_count == 0
    assert listing.calls[0].request.url.params["head"] == f"octo-org:{BRANCH}"
    assert listing.calls[0].request.url.params["state"] == "open"
    await gw.aclose()


@respx.mock
async def test_open_pull_request_opens_a_draft_and_labels_it(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/pulls").mock(return_value=httpx.Response(200, json=[]))
    post = respx_mock.post(f"{API}/pulls").mock(return_value=httpx.Response(201, json=_pull(9)))
    labels = respx_mock.post(f"{API}/issues/9/labels").mock(return_value=httpx.Response(200, json=[]))
    gw = gateway(dry_run=False)
    result = await gw.invoke(
        call("open_pull_request", head=BRANCH, base="main", title="t", body="b", draft=False, labels=["agent-generated"]),
        decision("open_pull_request"),
    )
    assert result.ok and not result.cached
    assert result.data is not None and result.data["number"] == 9 and result.data["opened"] is True
    import json

    sent = json.loads(post.calls[0].request.content)
    assert sent["draft"] is True, "PLAN's draft_only obligation: the gateway never opens a ready PR"
    assert sent["head"] == BRANCH and sent["base"] == "main"
    assert json.loads(labels.calls[0].request.content) == {"labels": ["agent-generated"]}
    await gw.aclose()


@respx.mock
async def test_open_pull_request_422_already_exists_returns_the_existing_pr(respx_mock: respx.MockRouter) -> None:
    listing = respx_mock.get(f"{API}/pulls")
    listing.side_effect = [httpx.Response(200, json=[]), httpx.Response(200, json=[_pull(3)])]
    respx_mock.post(f"{API}/pulls").mock(
        return_value=httpx.Response(422, json={"message": "Validation Failed", "errors": [{"message": f"A pull request already exists for octo-org:{BRANCH}."}]})
    )
    gw = gateway(dry_run=False)
    result = await gw.invoke(
        call("open_pull_request", head=BRANCH, base="main", title="t", body="b"), decision("open_pull_request"),
    )
    # GitHub puts the phrase under `errors[].message` while `message` says only
    # "Validation Failed"; `_message_of` reads both, so Appendix C's rule fires.
    assert result.ok and result.cached is True
    assert result.data is not None and result.data["number"] == 3
    await gw.aclose()


@respx.mock
async def test_open_pull_request_422_with_the_phrase_in_message_is_success(respx_mock: respx.MockRouter) -> None:
    listing = respx_mock.get(f"{API}/pulls")
    listing.side_effect = [httpx.Response(200, json=[]), httpx.Response(200, json=[_pull(3)])]
    respx_mock.post(f"{API}/pulls").mock(
        return_value=httpx.Response(422, json={"message": f"A pull request already exists for octo-org:{BRANCH}."})
    )
    gw = gateway(dry_run=False)
    result = await gw.invoke(
        call("open_pull_request", head=BRANCH, base="main", title="t", body="b"), decision("open_pull_request"),
    )
    assert result.ok and result.cached is True
    assert result.data is not None and result.data["number"] == 3
    await gw.aclose()


# ---------------------------------------------------------------------------
# create_issue
# ---------------------------------------------------------------------------


@respx.mock
async def test_create_issue_comments_on_the_open_issue_for_the_signature(respx_mock: respx.MockRouter) -> None:
    marker = ISSUE_SIGNATURE_MARKER.format(signature_id="sig_abc")
    respx_mock.get(f"{API}/issues").mock(
        return_value=httpx.Response(200, json=[
            {"number": 4, "html_url": f"https://github.com/{REPO}/issues/4", "title": "old", "body": f"earlier\n\n{marker}"},
            {"number": 5, "html_url": "x", "title": "a pr", "body": marker, "pull_request": {"url": "..."}},
        ])
    )
    comment = respx_mock.post(f"{API}/issues/4/comments").mock(return_value=httpx.Response(201, json={}))
    post = respx_mock.post(f"{API}/issues").mock(return_value=httpx.Response(201, json={}))
    gw = gateway(dry_run=False)
    result = await gw.invoke(
        call("create_issue", title="t", body="again", labels=["agent-triage"], signature_id="sig_abc"),
        decision("create_issue"),
    )
    assert result.ok and result.cached is True
    assert result.data is not None and result.data["number"] == 4 and result.data["commented"] is True
    assert comment.call_count == 1 and post.call_count == 0
    await gw.aclose()


@respx.mock
async def test_create_issue_files_a_new_issue_with_the_marker(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/issues").mock(return_value=httpx.Response(200, json=[]))
    post = respx_mock.post(f"{API}/issues").mock(
        return_value=httpx.Response(201, json={"number": 10, "html_url": f"https://github.com/{REPO}/issues/10"})
    )
    gw = gateway(dry_run=False)
    result = await gw.invoke(
        call("create_issue", title="t", body="b", labels=["agent-triage"], signature_id="sig_abc"),
        decision("create_issue"),
    )
    assert result.ok and not result.cached
    assert result.data is not None and result.data["number"] == 10 and result.data["filed"] is True
    import json

    sent = json.loads(post.calls[0].request.content)
    assert sent["body"].endswith(ISSUE_SIGNATURE_MARKER.format(signature_id="sig_abc"))
    assert sent["labels"] == ["agent-triage"]
    await gw.aclose()


@respx.mock
async def test_create_issue_dry_run_reports_what_it_found(respx_mock: respx.MockRouter) -> None:
    listing = respx_mock.get(f"{API}/issues").mock(return_value=httpx.Response(200, json=[]))
    post = respx_mock.post(f"{API}/issues").mock(return_value=httpx.Response(201, json={}))
    gw = gateway(dry_run=True)
    result = await gw.invoke(call("create_issue", title="t", body="b", signature_id="sig_abc"), decision("create_issue"))
    assert result.ok and result.dry_run and result.data is not None
    assert result.data["would_have"]["title"] == "t" and result.data["existed"] is False
    assert listing.call_count == 1 and post.call_count == 0
    await gw.aclose()


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


@respx.mock
async def test_a_repeated_write_inside_one_gateway_is_served_from_the_key(respx_mock: respx.MockRouter) -> None:
    """Appendix C: the per-run dict of completed writes stops a retry from double-firing."""
    respx_mock.get(f"{API}/git/ref/heads/{BRANCH}").mock(return_value=httpx.Response(404, json={}))
    post = respx_mock.post(f"{API}/git/refs").mock(
        return_value=httpx.Response(201, json={"ref": f"refs/heads/{BRANCH}", "object": {"sha": HEAD}})
    )
    gw = gateway(dry_run=False)
    first = await gw.invoke(call("create_branch", key="k1", name=BRANCH, from_sha=HEAD), decision("create_branch"))
    second = await gw.invoke(call("create_branch", key="k1", name=BRANCH, from_sha=HEAD), decision("create_branch"))
    assert first.ok and second.ok and second.cached is True and second.data == first.data
    assert post.call_count == 1
    await gw.aclose()


@respx.mock
async def test_forbidden_write_is_refused_before_any_request(respx_mock: respx.MockRouter) -> None:
    catch_all = respx_mock.route(host="api.github.com").mock(return_value=httpx.Response(200, json={}))
    gw = gateway(dry_run=False)
    forged = PolicyDecision(
        tool="merge_pull_request", rule_id="open-fix-pr", effect="allow", reason="forged",
        evaluated_at=datetime.now(UTC),
    )
    result = await gw.invoke(call("merge_pull_request", number=1), forged)
    assert not result.ok and result.error is not None and result.error.kind == "forbidden_by_policy"
    assert catch_all.call_count == 0
    await gw.aclose()


@pytest.mark.parametrize("tool", ["create_branch", "create_or_update_file", "open_pull_request", "create_issue"])
@respx.mock
async def test_missing_arguments_are_invalid_args_without_a_request(tool: str, respx_mock: respx.MockRouter) -> None:
    catch_all = respx_mock.route(host="api.github.com").mock(return_value=httpx.Response(200, json={}))
    gw = gateway(dry_run=False)
    result = await gw.invoke(call(tool), decision(tool))
    assert not result.ok and result.error is not None and result.error.kind == "invalid_args"
    assert catch_all.call_count == 0
    await gw.aclose()
