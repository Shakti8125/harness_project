# ruff: noqa: E501
"""Phase 5 audit finding 1: the scrub at rest must never rewrite an execution input.

The assignment-shaped heuristic (`password=…`, `api_key: …`, `token = …`) exists for log
lines and served bodies, where a false positive costs a few characters of display. Applied
*through* a base64 encoding it rewrote ordinary source lines inside a drafted file
(`DB_PASSWORD = os.environ.get("DB_PASSWORD")`), and the approval route then committed the
rewritten file. Two barriers now: the heuristic shapes are plain-text only, and a stored
plan whose arguments carry the placeholder is refused at execution rather than run.
"""

from __future__ import annotations

import asyncio
import base64
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.api.deps import HEURISTIC_SECRET_PATTERNS, SECRET_PATTERNS, build_secret_registry
from src.harness.gateway import ToolCall, ToolError, ToolResult, ToolSpec
from src.harness.guardrails import PolicyDecision
from src.harness.memory import ApprovalRecord, SqliteMemoryStore
from src.harness.observability import (
    REDACTION_PLACEHOLDER,
    Redactor,
    SecretRegistry,
    carries_redaction,
)
from src.harness.orchestrator import new_run_id
from src.integrations.cicd.remediation import execute_plan
from src.integrations.cicd.schemas import RemediationPlan
from src.settings import get_settings

TOKEN = "ghp_" + "L" * 36
SOURCE = (
    'import os\n\n'
    'DB_PASSWORD = os.environ.get("DB_PASSWORD")\n'
    'client = Client(api_key=settings.api_key)\n'
    'token = settings.github_token.get_secret_value()\n'
)


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def redactor(*secrets: str) -> Redactor:
    registry = SecretRegistry()
    for index, secret in enumerate(secrets):
        registry.register(f"s{index}", secret)
    return Redactor(registry, SECRET_PATTERNS, heuristic_patterns=HEURISTIC_SECRET_PATTERNS)


# --- the Redactor ---------------------------------------------------------------------


def test_assignment_shapes_are_scrubbed_in_plain_text() -> None:
    plain = redactor().scrub("password=hunter2hunter2 and api_key: abcdefghijkl and token = 0123456789")
    assert plain == f"{REDACTION_PLACEHOLDER} and {REDACTION_PLACEHOLDER} and {REDACTION_PLACEHOLDER}"


def test_assignment_shapes_are_not_scrubbed_through_base64() -> None:
    """The source lines the audit reproduced come back byte for byte."""
    encoded = b64(SOURCE)
    assert redactor().scrub(encoded) == encoded
    assert redactor().scrub({"content_b64": encoded})["content_b64"] == encoded  # type: ignore[index]


def test_a_registered_secret_and_a_vendor_token_are_still_scrubbed_through_base64() -> None:
    inner = f'{SOURCE}TOKEN = "{TOKEN}"\nOTHER = "{"AIza" + "Q" * 35}"\n'
    scrubbed = redactor(TOKEN).scrub(b64(inner))
    assert isinstance(scrubbed, str) and scrubbed != b64(inner)
    decoded = base64.b64decode(scrubbed).decode("utf-8")
    assert TOKEN not in decoded and "AIza" not in decoded
    assert decoded.count(REDACTION_PLACEHOLDER) == 2
    assert 'DB_PASSWORD = os.environ.get("DB_PASSWORD")' in decoded, "the ordinary lines beside it are untouched"


def test_the_heuristic_tier_is_the_assignment_shape_and_nothing_vendor_specific() -> None:
    assert len(HEURISTIC_SECRET_PATTERNS) == 1
    assert HEURISTIC_SECRET_PATTERNS[0].pattern.startswith("(?i)(api[_-]?key|token|password)")
    assert all(not p.pattern.startswith("(?i)(api") for p in SECRET_PATTERNS)
    assert isinstance(HEURISTIC_SECRET_PATTERNS[0], re.Pattern)


def test_carries_redaction_sees_the_placeholder_plainly_nested_and_under_base64() -> None:
    assert carries_redaction(REDACTION_PLACEHOLDER)
    assert carries_redaction({"args": [1, {"content_b64": b64(f"x = {REDACTION_PLACEHOLDER}\n")}]})
    assert carries_redaction([b64("fine"), f"token {REDACTION_PLACEHOLDER}"])
    assert not carries_redaction({"content_b64": b64(SOURCE), "message": "fix", "n": 3, "ok": None})
    assert not carries_redaction("REDACTED") and not carries_redaction("***")


# --- the store round trip the audit reproduced -----------------------------------------


def _plan_with(content_b64: str) -> dict[str, object]:
    return {
        "rationale": "the config module reads its password from the environment", "action": "open_fix_pr", "tool_calls": [
            {"call_id": "tc_0000000000ab", "tool": "create_or_update_file", "idempotency_key": "cicd:x:create_or_update_file:1",
             "args": {"branch": "agent/fix", "path": "app/config.py", "content_b64": content_b64, "message": "fix config"}},
        ],
    }


def test_a_stored_approval_plan_keeps_its_source_lines(tmp_db_path: Path) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    store = SqliteMemoryStore(
        tmp_db_path,
        redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS, heuristic_patterns=HEURISTIC_SECRET_PATTERNS),
    )
    now = datetime.now(UTC)
    record = ApprovalRecord(
        approval_id="apr_source", run_id=new_run_id(), state="pending",
        plan=_plan_with(b64(SOURCE)), requested_at=now, expires_at=now + timedelta(days=1),  # type: ignore[arg-type]
    )

    async def round_trip() -> ApprovalRecord | None:
        await store.initialize()
        await store.save_approval(record)
        return await store.get_approval("apr_source")

    stored = asyncio.run(round_trip())
    assert stored is not None
    assert stored.plan["tool_calls"][0]["args"]["content_b64"] == b64(SOURCE)  # type: ignore[index]


# --- the execution guard -------------------------------------------------------------


class _Recording:
    integration = "cicd"

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    def catalog(self) -> list[ToolSpec]:
        return []

    async def invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult:
        self.calls.append(call)
        return ToolResult(call_id=call.call_id, tool=call.tool, ok=True, data={"committed": True}, latency_ms=1)

    async def aclose(self) -> None:
        return None


def _decision(tool: str) -> PolicyDecision:
    return PolicyDecision(tool=tool, rule_id="open-fix-pr", effect="allow", reason="approved", evaluated_at=datetime.now(UTC))


def test_execute_plan_refuses_an_argument_that_carries_the_placeholder() -> None:
    """A plan altered at rest is not the plan that was approved: nothing reaches the gateway."""
    poisoned = b64(f'TOKEN = "{REDACTION_PLACEHOLDER}"\n')
    plan = RemediationPlan.model_validate({
        **_plan_with(poisoned),
        "tool_calls": [
            {"call_id": "tc_0000000000aa", "tool": "create_branch", "idempotency_key": "cicd:x:create_branch:1", "args": {"name": "agent/fix", "from_sha": "e" * 40}},
            _plan_with(poisoned)["tool_calls"][0],  # type: ignore[index]
            {"call_id": "tc_0000000000ac", "tool": "open_pull_request", "idempotency_key": "cicd:x:open_pull_request:1", "args": {"head": "agent/fix", "base": "main", "title": "t", "body": "b"}},
        ],
    })
    gateway = _Recording()
    results = asyncio.run(execute_plan(gateway, plan, [_decision(c.tool) for c in plan.tool_calls]))
    assert [c.tool for c in gateway.calls] == ["create_branch"], "the branch went through; the file did not; the PR was never reached"
    assert [r.ok for r in results] == [True, False]
    error = results[1].error
    assert isinstance(error, ToolError) and error.kind == "invalid_args" and error.retryable is False
    assert "redaction" in error.message and "create_or_update_file" in error.message


def test_execute_plan_runs_a_clean_plan_untouched() -> None:
    plan = RemediationPlan.model_validate(_plan_with(b64(SOURCE)))
    gateway = _Recording()
    results = asyncio.run(execute_plan(gateway, plan, [_decision("create_or_update_file")]))
    assert [r.ok for r in results] == [True] and len(gateway.calls) == 1
    assert gateway.calls[0].args["content_b64"] == b64(SOURCE)
