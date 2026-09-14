# ruff: noqa: E501
"""Appendix D's baseline resolution, in order: branch green -> default-branch green ->
head commit only -> none (Phase 5 audit finding 2: the chain stopped at step 1, so a
branch push with no green run on that branch was a cold start even when the default
branch had one -- and the demo repository's branch-push scenarios could never be a
`real_regression`).

The four tests PLAN.md names, plus the two edges the chain has to get right: the default
branch is not asked twice when the failing branch *is* the default branch, and a
`get_commit` that fails is a degraded diff, not a crash.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import RunRequest
from src.harness.gateway import ToolCall, ToolError, ToolResult, ToolSpec
from src.harness.guardrails import PolicyDecision
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.harness.orchestrator import RunState, new_run_id
from src.integrations.cicd.agents.investigator import Investigator, parse_subject
from src.integrations.cicd.catalog import CATALOG
from src.integrations.cicd.rendering import render_diff_summary
from src.integrations.cicd.wiring import INTEGRATION
from tests.stubs import ScenarioStubLlm

REPO = "octo-org/harness-demo-repo"
HEAD = "e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df"
BASE = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
PARENT = "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"

Handler = Callable[[dict[str, Any]], dict[str, Any] | ToolError]


class ScriptedGateway:
    """A `ToolGateway` whose every read is a canned answer keyed by tool name."""

    integration = "cicd"

    def __init__(self, handlers: dict[str, Handler]) -> None:
        self.handlers = handlers
        self.calls: list[ToolCall] = []

    def catalog(self) -> list[ToolSpec]:
        return list(CATALOG)

    async def invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult:
        self.calls.append(call)
        handler = self.handlers.get(call.tool)
        if handler is None:
            error = ToolError(kind="not_found", message=f"unscripted tool {call.tool!r}", retryable=False)
            return ToolResult(call_id=call.call_id, tool=call.tool, ok=False, error=error, latency_ms=0)
        answer = handler(dict(call.args))
        if isinstance(answer, ToolError):
            return ToolResult(call_id=call.call_id, tool=call.tool, ok=False, error=answer, latency_ms=0)
        return ToolResult(call_id=call.call_id, tool=call.tool, ok=True, data=answer, latency_ms=0)

    async def aclose(self) -> None:
        return None

    def named(self, tool: str) -> list[ToolCall]:
        return [call for call in self.calls if call.tool == tool]


def subject(branch: str = "demo/regression", default_branch: str = "main") -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_run": {
            "id": 777, "run_attempt": 1, "head_sha": HEAD, "head_branch": branch,
            "workflow_id": 9001, "name": "regression", "event": "push", "conclusion": "failure",
        },
        "repository": {"full_name": REPO, "default_branch": default_branch},
    }


def _files() -> list[dict[str, Any]]:
    return [{
        "filename": "src/pricing/discount.py", "status": "modified", "additions": 1, "deletions": 1,
        "patch": "@@ -1 +1 @@\n-    return price\n+    return price + 1",
    }]


def handlers(*, branch_green: bool, default_green: bool, parents: list[str] | None, commit_error: ToolError | None = None) -> dict[str, Handler]:
    def runs(args: dict[str, Any]) -> dict[str, Any]:
        green = branch_green if args.get("branch") == "demo/regression" else default_green
        return {"total_count": int(green), "workflow_runs": [{"id": 1, "head_sha": BASE, "conclusion": "success"}] if green else []}

    def commit(args: dict[str, Any]) -> dict[str, Any] | ToolError:
        if commit_error is not None:
            return commit_error
        return {"sha": args["sha"], "parents": [{"sha": sha} for sha in parents or []], "files": _files()}

    return {
        "list_workflow_run_jobs": lambda _: {"jobs": [{"id": 601, "name": "test", "conclusion": "failure", "labels": ["ubuntu-latest"]}]},
        "get_job_logs": lambda _: {"content": "FAILED tests/test_pricing.py::test_discount - assert 90 == 91\n", "truncated": False, "head_dropped": False, "total_bytes": 60},
        "find_last_successful_run": runs,
        "compare_commits": lambda args: {"files": _files(), "commits": [{"sha": HEAD}], "behind_by": 0},
        "get_commit": commit,
    }


async def collect(gateway: ScriptedGateway, body: dict[str, Any], tmp_db_path: Path) -> Any:
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    investigator = Investigator(
        gateway=gateway, context_manager=ContextManager(default_budget=ContextBudget(total_chars=20_000)),
        llm=ScenarioStubLlm(), model="stub", recorder=recorder, memory=None,
    )
    request = RunRequest(integration=INTEGRATION, subject=body, idempotency_key="cicd:baseline-test", mode="replay")
    state = RunState(run_id=new_run_id(), request=request, artifacts={}, degraded=[], stages=[])
    return await investigator.collect(state), state


def test_parse_subject_carries_the_default_branch() -> None:
    parsed = parse_subject(subject(default_branch="trunk"))
    assert parsed["branch"] == "demo/regression"
    assert parsed["default_branch"] == "trunk"
    # The webhook's own `workflow_run.repository` is enough when the top level is absent.
    nested = {"workflow_run": {**subject()["workflow_run"], "repository": {"full_name": REPO, "default_branch": "main"}}}
    assert parse_subject(nested)["default_branch"] == "main"


def test_resolves_branch_green(tmp_db_path: Path) -> None:
    gateway = ScriptedGateway(handlers(branch_green=True, default_green=True, parents=[PARENT]))
    collected, state = asyncio.run(collect(gateway, subject(), tmp_db_path))
    assert collected.diff.baseline_kind == "branch_green"
    assert collected.diff.base_sha == BASE and collected.cold_start is False
    assert [c.args["branch"] for c in gateway.named("find_last_successful_run")] == ["demo/regression"]
    assert gateway.named("get_commit") == []
    assert [f.path for f in collected.diff.files] == ["src/pricing/discount.py"]
    assert collected.degraded == []


def test_falls_back_to_default_green(tmp_db_path: Path) -> None:
    gateway = ScriptedGateway(handlers(branch_green=False, default_green=True, parents=[PARENT]))
    collected, state = asyncio.run(collect(gateway, subject(), tmp_db_path))
    assert collected.diff.baseline_kind == "default_green"
    assert collected.diff.base_sha == BASE
    assert collected.cold_start is False, "a default-branch green is a baseline, not a cold start"
    assert [c.args["branch"] for c in gateway.named("find_last_successful_run")] == ["demo/regression", "main"]
    compare = gateway.named("compare_commits")
    assert len(compare) == 1 and compare[0].args == {"base": BASE, "head": HEAD}
    assert [f.path for f in collected.diff.files] == ["src/pricing/discount.py"]
    assert collected.degraded == []
    assert "default_green" in render_diff_summary(collected.diff)


def test_head_commit_only_when_no_success(tmp_db_path: Path) -> None:
    gateway = ScriptedGateway(handlers(branch_green=False, default_green=False, parents=[PARENT]))
    collected, state = asyncio.run(collect(gateway, subject(), tmp_db_path))
    assert collected.diff.baseline_kind == "head_commit_only"
    assert collected.diff.base_sha == PARENT and collected.diff.head_sha == HEAD
    assert collected.cold_start is True
    assert [c.args["sha"] for c in gateway.named("get_commit")] == [HEAD]
    assert gateway.named("compare_commits") == []
    assert [f.path for f in collected.diff.files] == ["src/pricing/discount.py"]
    assert collected.diff.commit_shas == [HEAD]
    assert collected.degraded == []
    summary = render_diff_summary(collected.diff)
    assert "head commit only" in summary and "real_regression" in summary and "0.70" in summary


def test_none_on_initial_commit(tmp_db_path: Path) -> None:
    gateway = ScriptedGateway(handlers(branch_green=False, default_green=False, parents=[]))
    collected, state = asyncio.run(collect(gateway, subject(), tmp_db_path))
    assert collected.diff.baseline_kind == "none"
    assert collected.diff.base_sha is None and collected.diff.files == []
    assert collected.cold_start is True
    assert collected.degraded == []
    assert "No green baseline" in render_diff_summary(collected.diff)


def test_default_branch_is_not_asked_twice_when_it_is_the_failing_branch(tmp_db_path: Path) -> None:
    gateway = ScriptedGateway(handlers(branch_green=False, default_green=False, parents=[]))
    collected, _ = asyncio.run(collect(gateway, subject(branch="main"), tmp_db_path))
    assert [c.args["branch"] for c in gateway.named("find_last_successful_run")] == ["main"]
    assert collected.diff.baseline_kind == "none"


def test_a_failing_get_commit_is_a_degraded_diff_not_a_crash(tmp_db_path: Path) -> None:
    error = ToolError(kind="upstream_5xx", message="502 from GET commits", retryable=True, http_status=502)
    gateway = ScriptedGateway(handlers(branch_green=False, default_green=False, parents=[PARENT], commit_error=error))
    collected, state = asyncio.run(collect(gateway, subject(), tmp_db_path))
    assert collected.diff.baseline_kind == "none" and collected.cold_start is True
    assert "diff" in collected.degraded
    assert [e.kind for e in collected.gateway_errors] == ["upstream_5xx"]


@pytest.mark.parametrize("branch", ["demo/regression", "main"])
def test_the_baseline_call_carries_before(tmp_db_path: Path, branch: str) -> None:
    """Every `find_last_successful_run` in the chain excludes the failing sha itself."""
    gateway = ScriptedGateway(handlers(branch_green=False, default_green=False, parents=[]))
    asyncio.run(collect(gateway, subject(branch=branch), tmp_db_path))
    assert all(c.args["before"] == HEAD for c in gateway.named("find_last_successful_run"))
