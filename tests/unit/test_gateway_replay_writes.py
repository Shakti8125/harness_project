# ruff: noqa: E501
"""`ReplayToolGateway`'s synthesized writes and its `gateway.invoke` spans (Phase 5)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path

from src.api.deps import FIXTURES_ROOT, SECRET_PATTERNS
from src.harness.gateway import ToolCall
from src.harness.guardrails import PolicyDecision
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.integrations.cicd.gateway_replay import ReplayToolGateway
from src.integrations.cicd.wiring import load_forbidden

REPO = "octo-org/harness-demo-repo"
SCENARIO = FIXTURES_ROOT / "real_regression"
HEAD = "e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df"


def decision(tool: str, effect: str = "allow") -> PolicyDecision:
    return PolicyDecision(
        tool=tool, rule_id="test", effect=effect, reason="test",  # type: ignore[arg-type]
        evaluated_at=datetime.now(UTC),
    )


def call(tool: str, key: str | None = None, **args: object) -> ToolCall:
    return ToolCall(call_id="tc_0000000000ab", tool=tool, args=dict(args), idempotency_key=key)  # type: ignore[arg-type]


def gateway(*, dry_run: bool = True, recorder: TraceRecorder | None = None) -> ReplayToolGateway:
    return ReplayToolGateway(
        scenario_dir=SCENARIO, repo=REPO, forbidden=load_forbidden(), dry_run=dry_run, recorder=recorder
    )


async def test_every_implemented_write_answers_the_live_shape() -> None:
    gw = gateway(dry_run=True)
    branch = await gw.invoke(call("create_branch", name="agent/fix/e2cdf1b4", from_sha=HEAD), decision("create_branch"))
    assert branch.ok and branch.dry_run
    assert branch.data is not None and branch.data["ref"] == "refs/heads/agent/fix/e2cdf1b4"
    assert branch.data["sha"] == HEAD and branch.data["created"] is False

    content = base64.b64encode(b"x = 1\n").decode()
    written = await gw.invoke(
        call("create_or_update_file", branch="agent/fix/e2cdf1b4", path="x.py", content_b64=content, message="m"),
        decision("create_or_update_file"),
    )
    assert written.ok and written.data is not None
    assert len(written.data["commit_sha"]) == 40 and written.data["path"] == "x.py"
    assert content not in str(written.data)

    pull = await gw.invoke(
        call("open_pull_request", head="agent/fix/e2cdf1b4", base="main", title="t", body="b", labels=["agent-generated"]),
        decision("open_pull_request"),
    )
    assert pull.ok and pull.data is not None
    assert pull.data["draft"] is True and pull.data["html_url"] == f"https://github.com/{REPO}/pull/{pull.data['number']}"
    assert pull.data["labels"] == ["agent-generated"] and pull.data["opened"] is False

    issue = await gw.invoke(call("create_issue", title="t", body="b", labels=["agent-triage"]), decision("create_issue"))
    assert issue.ok and issue.data is not None
    assert issue.data["html_url"].endswith(f"/issues/{issue.data['number']}")
    for result in (branch, written, pull, issue):
        assert result.data is not None and "synthesized" in str(result.data["note"])


async def test_synthesized_identifiers_are_deterministic_across_gateways() -> None:
    args = {"head": "agent/fix/e2cdf1b4", "base": "main", "title": "t", "body": "b"}
    one = await gateway().invoke(call("open_pull_request", **args), decision("open_pull_request"))
    two = await gateway().invoke(call("open_pull_request", **args), decision("open_pull_request"))
    assert one.data == two.data
    other = await gateway().invoke(call("open_pull_request", **{**args, "title": "u"}), decision("open_pull_request"))
    assert other.data is not None and one.data is not None
    assert other.data["number"] != one.data["number"]


async def test_dry_run_false_reports_the_write_as_done() -> None:
    gw = gateway(dry_run=False)
    branch = await gw.invoke(call("create_branch", name="b", from_sha=HEAD), decision("create_branch"))
    assert branch.ok and not branch.dry_run
    assert branch.data is not None and branch.data["created"] is True


async def test_a_repeated_write_is_served_from_its_key() -> None:
    gw = gateway()
    first = await gw.invoke(call("create_branch", key="k", name="b", from_sha=HEAD), decision("create_branch"))
    second = await gw.invoke(call("create_branch", key="k", name="b", from_sha=HEAD), decision("create_branch"))
    assert second.cached is True and second.data == first.data


async def test_missing_arguments_are_invalid_args() -> None:
    result = await gateway().invoke(call("create_branch", name="b"), decision("create_branch"))
    assert not result.ok and result.error is not None and result.error.kind == "invalid_args"
    assert "from_sha" in result.error.message


async def test_invoke_records_a_gateway_span_including_a_refusal(tmp_db_path: Path) -> None:
    """Phase 5: with a recorder, every invoke is a `gateway.invoke` span in the live
    gateway's shape -- reads, writes, and the forbidden refusal alike."""
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), SECRET_PATTERNS))
    await recorder.initialize()
    run_id = "run_01J8ABCDEFGHJKMNPQRSTVWXYZ"
    gw = gateway(recorder=recorder)
    with recorder.run_scope(run_id):
        await gw.invoke(call("compare_commits", base="8d4d0a89231f66f3b3910ad16e033041c898512d", head=HEAD), decision("compare_commits"))
        await gw.invoke(call("create_branch", key="k", name="b", from_sha=HEAD), decision("create_branch", "require_approval"))
        await gw.invoke(call("merge_pull_request", number=1), decision("merge_pull_request"))
        await gw.invoke(call("get_job_logs", job_id=999), decision("get_job_logs"))
    trace = await recorder.read_trace(run_id)
    assert trace is not None
    spans = [s for s in trace.spans if s.name == "gateway.invoke"]
    assert [s.component for s in spans] == ["gateway"] * 4
    by_tool = {str(s.attributes["tool"]): s.attributes for s in spans}
    assert by_tool["compare_commits"]["side_effect"] == "read" and by_tool["compare_commits"]["ok"] is True
    assert by_tool["create_branch"]["side_effect"] == "write" and by_tool["create_branch"]["dry_run"] is True
    assert by_tool["create_branch"]["rule_id"] == "test"
    assert by_tool["merge_pull_request"]["ok"] is False
    assert by_tool["merge_pull_request"]["error_kind"] == "forbidden_by_policy"
    assert by_tool["get_job_logs"]["ok"] is False and by_tool["get_job_logs"]["error_kind"] == "not_found"


async def test_without_a_recorder_nothing_is_written(tmp_db_path: Path) -> None:
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), SECRET_PATTERNS))
    await recorder.initialize()
    run_id = "run_01J8ABCDEFGHJKMNPQRSTVWXYZ"
    with recorder.run_scope(run_id):
        await gateway().invoke(call("create_branch", name="b", from_sha=HEAD), decision("create_branch"))
    assert await recorder.read_trace(run_id) is None
