"""review-2.md finding 4 (Phase 1 backlog): the model's optional, read-only
`additional_tool_calls` used to be unobservable end to end -- `executed: list[str]` was
populated and never read, `result.data` was discarded, and a refusal or a gateway failure
was visible only in a log line.

`Investigator.run` now records one `AdditionalToolCallOutcome` per optional call the model
named (at most 3, the cap on `InvestigationNotes.additional_tool_calls`), and files the list
onto `FailureBundle.additional_tool_outcomes` -- which reaches the served `RunOutcome`
(`final["bundle"]["additional_tool_outcomes"]`) because it is filed the same way the rest of
the bundle already was, over the real orchestrator, over HTTP, exactly as
`test_replay_e2e.py` drives the rest of this pipeline.

All three outcomes are exercised against the real `real_regression` fixture, no invented
gateway behaviour: `compare_commits` with the fixture's own recorded base/head resolves
("obtained"); `merge_pull_request` is a write tool outside this gateway's read-only catalog
and is refused before any file is touched ("refused"); `get_commit` with a sha nothing
recorded 404s ("failed", carrying the `ToolError`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.deps import SECRET_PATTERNS, AppContext, build_secret_registry
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import TokenUsage
from src.harness.llm import LlmRequest, RawLlmResponse
from src.harness.observability import Redactor, TraceRecorder
from src.settings import get_settings
from tests.stubs import remediation_plan

HEAD_SHA = "e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df"
BASE_SHA = "8d4d0a89231f66f3b3910ad16e033041c898512d"

_NOTES_WITH_THREE_OPTIONAL_CALLS = {
    "observations": ["tests/test_pricing.py::test_discount_applies fails"],
    "additional_tool_calls": [
        {
            "call_id": "tc_opt_obtained",
            "tool": "compare_commits",
            "args": {"base": BASE_SHA, "head": HEAD_SHA},
        },
        {
            "call_id": "tc_opt_refused",
            "tool": "merge_pull_request",
            "args": {"pr_number": 1},
        },
        {
            "call_id": "tc_opt_failed",
            "tool": "get_commit",
            "args": {"sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"},
        },
    ],
    "narrative": "One optional probe of each shape: obtained, refused, and failed.",
}

_DIAGNOSIS_PAYLOAD = {
    "reasoning": "The log shows assert 91 == 90; the diff changes discount().",
    "category": "real_regression",
    "summary": "Off-by-one in discount().",
    "self_confidence": 0.9,
    "citations": [
        {
            "claim_kind": "quote_exists",
            "locator": "log:job/601234567",
            "quote": "assert 91 == 90",
            "note": "the failing assertion",
        }
    ],
    "suspected_commit_sha": HEAD_SHA,
    "suspected_test_ids": [],
    "suspected_package": None,
    "suggested_action": "open_fix_pr",
}


class _StubLlm:
    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        if "You are the Investigator" in req.prompt:
            payload: object = _NOTES_WITH_THREE_OPTIONAL_CALLS
        elif "You are the Diagnostician" in req.prompt:
            payload = _DIAGNOSIS_PAYLOAD
        elif "You are the Remediator" in req.prompt:
            payload = remediation_plan()
        else:  # pragma: no cover - a new agent would have to opt in here
            raise AssertionError("unrecognised prompt reached the stub model")
        return RawLlmResponse(
            text=json.dumps(payload),
            tokens=TokenUsage(prompt=100, completion=50, total=150),
            finish_reason="STOP",
            model=req.model,
            latency_ms=1,
        )


@pytest.fixture
def client(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = get_settings()
    context = AppContext(
        settings=settings,
        recorder=TraceRecorder(
            db_path=tmp_db_path,
            redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
        ),
        context_manager=ContextManager(
            default_budget=ContextBudget(total_chars=settings.log_char_budget)
        ),
        llm=_StubLlm(),
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as test_client:
        yield test_client


def test_obtained_refused_and_failed_all_reach_the_served_bundle(
    client: TestClient,
) -> None:
    body = client.post("/v1/replay/real_regression").json()
    # A regression diagnosis now runs on into a fix-PR plan that needs approval; that is
    # the run's terminal state in this phase, and the bundle is served either way.
    assert body["status"] == "awaiting_approval"

    outcomes = body["final"]["bundle"]["additional_tool_outcomes"]
    assert len(outcomes) == 3
    by_tool = {entry["tool"]: entry for entry in outcomes}

    assert by_tool["compare_commits"]["outcome"] == "obtained"
    assert by_tool["compare_commits"]["error"] is None

    assert by_tool["merge_pull_request"]["outcome"] == "refused"
    assert by_tool["merge_pull_request"]["reason"] != ""

    assert by_tool["get_commit"]["outcome"] == "failed"
    assert by_tool["get_commit"]["error"] is not None
    assert by_tool["get_commit"]["error"]["kind"] == "not_found"


def test_optional_tool_failures_do_not_trip_gateway_degraded(client: TestClient) -> None:
    """B.2: a 404 on an optional read tool is data, not a run failure -- it must stay
    off `gateway_errors` (the field `gateway_degraded` actually reads) even though it
    is now visible on `additional_tool_outcomes`.
    """
    body = client.post("/v1/replay/real_regression").json()
    assert body["final"]["bundle"]["gateway_errors"] == []
    names = {row["name"] for row in body["final"]["diagnosis"]["confidence_adjustments"]}
    assert "gateway_degraded" not in names
