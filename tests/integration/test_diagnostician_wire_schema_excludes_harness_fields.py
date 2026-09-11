"""review.md finding 4: `Diagnosis` asks the model for `final_confidence` and
`confidence_adjustments`, which Appendix A.11 marks "added by the harness after the
model returns, not requested from the model". Before the fix, `to_gemini_schema(Diagnosis)`
was used verbatim as the wire schema, so both fields reached the model as things it was
asked to fill in -- wrong the moment a second consumer reads `Diagnosis` without going
through the harness's `model_copy` overwrite.

The fix wraps `to_gemini_schema` in a Diagnostician-local `_diagnosis_schema` (wired in via
`LLMAgent`'s `schema_translator` injection point) that strips exactly those two keys from
the OUTGOING schema, while `output_model=Diagnosis` -- and therefore what `retry_structured`
validates the model's JSON against -- is untouched.

Both halves are pinned: the wire schema the model actually receives never asks for either
field (captured off the real `LlmRequest`, through the real orchestrator, over HTTP, exactly
as `test_replay_e2e.py` drives the rest of this pipeline), and the served `RunOutcome` still
carries both fields, populated by the harness's post-hoc calibration.
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
from src.harness.llm import LlmRequest, RawLlmResponse, to_gemini_schema
from src.harness.observability import Redactor, TraceRecorder
from src.integrations.cicd.agents.diagnostician import _diagnosis_schema
from src.integrations.cicd.schemas import Diagnosis
from src.settings import get_settings
from tests.stubs import remediation_plan

HEAD_SHA = "e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df"

_NOTES = {
    "observations": ["tests/test_pricing.py::test_discount_applies fails"],
    "additional_tool_calls": [],
    "narrative": "A pricing test fails on a diff that touches one file.",
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
    # Deliberately absent: `final_confidence` / `confidence_adjustments`. The model
    # response never carries them under the fix, and Pydantic's own defaults must fill
    # them in on the way back through `retry_structured` regardless.
}


class _SchemaCapturingLlm:
    """Answers whichever agent asked and records every outgoing `LlmRequest.schema`."""

    def __init__(self) -> None:
        self.requests: list[LlmRequest] = []

    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        self.requests.append(req)
        if "You are the Investigator" in req.prompt:
            payload: object = _NOTES
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

    def diagnostician_request(self) -> LlmRequest:
        return next(req for req in self.requests if "You are the Diagnostician" in req.prompt)


@pytest.fixture
def stub_llm() -> _SchemaCapturingLlm:
    return _SchemaCapturingLlm()


@pytest.fixture
def client(
    stub_llm: _SchemaCapturingLlm, tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
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
        llm=stub_llm,
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as test_client:
        yield test_client


def test_the_two_harness_added_fields_are_absent_from_the_wire_schema(
    client: TestClient, stub_llm: _SchemaCapturingLlm
) -> None:
    response = client.post("/v1/replay/real_regression")
    assert response.status_code == 200

    outgoing_schema = stub_llm.diagnostician_request().schema
    assert outgoing_schema is not None
    rendered = repr(outgoing_schema)

    assert "final_confidence" not in rendered
    assert "confidence_adjustments" not in rendered
    assert "final_confidence" not in outgoing_schema.get("propertyOrdering", [])
    assert "confidence_adjustments" not in outgoing_schema.get("propertyOrdering", [])
    assert "final_confidence" not in outgoing_schema.get("required", [])


def test_both_fields_are_still_present_and_populated_on_the_served_diagnosis(
    client: TestClient,
) -> None:
    """`output_model=Diagnosis` is unchanged: the harness still writes both fields
    post-hoc via `model_copy`, and the served `RunOutcome` must still carry them.
    """
    body = client.post("/v1/replay/real_regression").json()
    diagnosis_payload = body["final"]["diagnosis"]

    assert "final_confidence" in diagnosis_payload
    assert "confidence_adjustments" in diagnosis_payload
    # Populated, not left at the untouched Pydantic default -- the model reported 0.9
    # self_confidence and no adjustment here drives it to exactly 0.0.
    assert diagnosis_payload["final_confidence"] > 0.0


def test_the_harness_level_translator_is_unchanged_by_this_fix() -> None:
    """The medium finding's other half is explicitly harness-core's to close (the
    finding itself says so: "harness-core (`to_gemini_schema` has no way to exclude a
    field)"). `to_gemini_schema(Diagnosis)` must still end its `propertyOrdering` with
    both fields -- only the Diagnostician's own wrapped translator excludes them.
    """
    ordering = to_gemini_schema(Diagnosis)["propertyOrdering"]
    assert ordering[-2:] == ["final_confidence", "confidence_adjustments"]

    wrapped_ordering = _diagnosis_schema(Diagnosis)["propertyOrdering"]
    assert "final_confidence" not in wrapped_ordering
    assert "confidence_adjustments" not in wrapped_ordering
