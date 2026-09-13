"""End-to-end replay: webhook in, cited and calibrated `Diagnosis` out, over HTTP.

PLAN.md Phase 1 Verify steps 2-4, run in-process.

**What is stubbed, and what deliberately is not.** Replay mode stubs the *gateway* — that
is what `fixtures/scenarios/` is — and the fixture format has no slot for a recorded model
response, so the model is the one boundary a replay cannot supply. It is stubbed *here, in
the test*, with a canned response per agent. Everything between the HTTP route and that
boundary is the real thing: the real orchestrator, the real stage gate, the real context
manager over the real 3,800-line fixture log, the real replay gateway reading the real
recorded API responses, and the real deterministic calibration.

That is the opposite of shipping a fake client in the product, which would make the
verification assert nothing. Here the stub is the *input* to the pipeline under test, and
what is asserted is what the pipeline did with it.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
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

NOTES = {
    "observations": [
        "tests/test_pricing.py::test_discount_applies fails with assert 91 == 90",
        "tests/test_pricing.py::test_checkout_total_applies_discount fails with assert 455 == 450",
        "The diff touches exactly one file, src/pricing/discount.py",
    ],
    "additional_tool_calls": [],
    "narrative": (
        "Two pricing tests fail on a run whose diff changes only the discount helper. "
        "The failing values are each one unit above the expected value."
    ),
}


def diagnosis(self_confidence: float, *, citations: bool = True) -> dict[str, object]:
    return {
        "reasoning": (
            "The log shows assert 91 == 90 from discount(100, 10), and the diff contains "
            "exactly one changed file, src/pricing/discount.py, whose commit message "
            "describes rounding savings up. Both point at the same edit independently."
        ),
        "category": "real_regression",
        "summary": "An off-by-one in discount() returns 91 instead of 90 for a 10% discount.",
        "self_confidence": self_confidence,
        "citations": (
            [
                {
                    "claim_kind": "quote_exists",
                    "locator": "log:job/601234567",
                    "quote": "assert 91 == 90",
                    "note": "the failing assertion",
                },
                {
                    "claim_kind": "file_in_diff",
                    "locator": "diff:src/pricing/discount.py",
                    "quote": "src/pricing/discount.py",
                    "note": "the only changed file",
                },
            ]
            if citations
            else []
        ),
        "suspected_commit_sha": HEAD_SHA,
        "suspected_test_ids": ["tests/test_pricing.py::test_discount_applies"],
        "suspected_package": None,
        "suggested_action": "open_fix_pr",
    }


class StubLlm:
    """Answers whichever agent asked, and records the prompts it was given.

    Dispatches on the prompt's own preamble rather than on call order, so a change to the
    pipeline's shape shows up as a failed assertion here instead of as two agents quietly
    receiving each other's output.
    """

    def __init__(
        self,
        self_confidence: float = 0.92,
        citations: bool = True,
        plan_action: str = "open_fix_pr",
    ) -> None:
        self.self_confidence = self_confidence
        self.citations = citations
        self.plan_action = plan_action
        self.prompts: list[str] = []

    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        self.prompts.append(req.prompt)
        if "You are the Investigator" in req.prompt:
            payload: object = NOTES
        elif "You are the Diagnostician" in req.prompt:
            payload = diagnosis(self.self_confidence, citations=self.citations)
        elif "You are the Remediator" in req.prompt:
            payload = remediation_plan(self.plan_action)
        else:  # pragma: no cover - a new agent would have to opt in here
            raise AssertionError("unrecognised prompt reached the stub model")
        return RawLlmResponse(
            text=json.dumps(payload),
            tokens=TokenUsage(prompt=1200, completion=300, total=1500),
            finish_reason="STOP",
            model=req.model,
            latency_ms=12,
        )


@pytest.fixture
def stub_llm() -> StubLlm:
    return StubLlm()


@pytest.fixture
def client(
    stub_llm: StubLlm, tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    settings = get_settings()
    recorder = TraceRecorder(
        db_path=tmp_db_path,
        redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
    )
    context = AppContext(
        settings=settings,
        recorder=recorder,
        context_manager=ContextManager(
            default_budget=ContextBudget(total_chars=settings.log_char_budget)
        ),
        llm=stub_llm,
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as test_client:
        yield test_client


def test_replay_produces_a_cited_diagnosis(client: TestClient) -> None:
    """Verify step 2: category, confidence and at least one citation."""
    response = client.post("/v1/replay/real_regression")
    assert response.status_code == 200
    body = response.json()

    # Phase 2: the regression's fix-PR plan needs a person, so the run's terminal
    # state is `awaiting_approval` rather than `completed`. The diagnosis is served
    # either way.
    assert body["status"] == "awaiting_approval"
    diagnosis_payload = body["final"]["diagnosis"]
    assert diagnosis_payload["category"] == "real_regression"
    assert diagnosis_payload["final_confidence"] >= 0.75
    assert len(diagnosis_payload["citations"]) >= 1


def test_replay_blames_the_right_commit(client: TestClient) -> None:
    """Verify step 3: the sha matches `scenario.yaml`'s `expected.commit`."""
    expected = _expected_commit()
    body = client.post("/v1/replay/real_regression").json()

    assert body["final"]["diagnosis"]["suspected_commit_sha"] == expected


def test_replay_meets_the_scenario_label(client: TestClient) -> None:
    """The fixture's own eval label, which is stricter than the Verify block."""
    body = client.post("/v1/replay/real_regression").json()
    diagnosis_payload = body["final"]["diagnosis"]

    assert diagnosis_payload["final_confidence"] >= 0.85
    assert diagnosis_payload["suggested_action"] == "open_fix_pr"
    assert body["final"]["bundle"]["diff"]["baseline_kind"] == "branch_green"
    assert body["final"]["bundle"]["cold_start"] is False

    quotes = " ".join(
        f"{citation['quote']} {citation['note']}"
        for citation in diagnosis_payload["citations"]
    )
    expected_any = ("discount", "test_discount_applies", "assert 91 == 90")
    assert any(needle in quotes for needle in expected_any)


def test_low_confidence_escalates(monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path) -> None:
    """Verify step 4: below the threshold the run escalates as `low_confidence`.

    The gate lives on the remediate stage, which has no agent this phase — so this also
    pins that the short-circuit fires from a stage that does not yet run.
    """
    stub = StubLlm(self_confidence=0.40)
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
        llm=stub,
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)

    with TestClient(api_main.app) as client:
        body = client.post("/v1/replay/real_regression").json()

    assert body["status"] == "escalated"
    assert body["escalation"]["reason"] == "low_confidence"
    # The diagnosis is still returned: an escalation is a request for a human, not a
    # discarded run.
    assert body["final"]["diagnosis"]["category"] == "real_regression"


def test_calibration_penalises_a_diagnosis_with_no_citations(
    monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
) -> None:
    """`no_citations` (-0.10) is applied, recorded, and visible in the outcome."""
    stub = StubLlm(self_confidence=0.92, citations=False)
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
        llm=stub,
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)

    with TestClient(api_main.app) as client:
        body = client.post("/v1/replay/real_regression").json()

    payload = body["final"]["diagnosis"]
    names = {row["name"]: row["delta"] for row in payload["confidence_adjustments"]}
    assert names["no_citations"] == pytest.approx(-0.10)
    assert payload["final_confidence"] == pytest.approx(0.82)


def test_the_model_saw_a_trimmed_log_that_still_carried_the_assertion(
    client: TestClient, stub_llm: StubLlm
) -> None:
    """The whole pipeline's point, asserted on the actual prompt text.

    The 455 KB fixture log cannot fit a 120 000-char budget, so it was trimmed — and the
    assertion the diagnosis rests on is in the prompt anyway.
    """
    client.post("/v1/replay/real_regression")

    investigator_prompt = next(p for p in stub_llm.prompts if "You are the Investigator" in p)
    assert "lines elided" in investigator_prompt, "expected the log to have been trimmed"
    assert "E       assert 91 == 90" in investigator_prompt
    assert "src/pricing/discount.py" in investigator_prompt


def test_trace_is_persisted_and_readable(client: TestClient) -> None:
    """`GET /v1/runs/{id}/trace` — the read path A.12 requires."""
    body = client.post("/v1/replay/real_regression").json()
    run_id = body["run_id"]
    assert body["trace_url"] == f"/v1/runs/{run_id}/trace"

    trace = client.get(f"/v1/runs/{run_id}/trace")
    assert trace.status_code == 200
    payload = trace.json()

    assert payload["run_id"] == run_id
    names = {span["name"] for span in payload["spans"]}
    assert {"run", "agent.run", "llm.attempt"} <= names
    # Aggregated from the `llm` spans only: the same counts also appear on the enclosing
    # `agent` spans as a roll-up, and summing both would double every figure.
    assert payload["totals"]["total"] == 4500
    assert payload["totals"]["prompt"] == 3600
    assert payload["duration_ms"] >= 0

    # Every span belongs to this run, and the agent spans hang off the run span.
    run_span = next(span for span in payload["spans"] if span["name"] == "run")
    assert run_span["parent_span_id"] is None
    agent_spans = [span for span in payload["spans"] if span["name"] == "agent.run"]
    assert len(agent_spans) == 3
    assert all(span["parent_span_id"] == run_span["span_id"] for span in agent_spans)


def test_trace_for_an_unknown_run_is_a_problem_document(client: TestClient) -> None:
    response = client.get("/v1/runs/run_00000000000000000000000000/trace")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "Trace not found"


def test_unknown_scenario_is_404(client: TestClient) -> None:
    response = client.post("/v1/replay/no_such_scenario")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_scenario_name_cannot_escape_the_fixtures_root(client: TestClient) -> None:
    response = client.post("/v1/replay/..%2f..%2fsrc")
    assert response.status_code in (400, 404)


def test_async_run_is_accepted_then_readable(client: TestClient) -> None:
    """`POST /v1/runs` returns 202 with the id the run will actually be recorded under."""
    webhook = _webhook()
    response = client.post(
        "/v1/runs",
        json={
            "integration": "cicd",
            "subject": webhook,
            "idempotency_key": "cicd:test-async-run-key",
            "mode": "replay",
            "replay_fixture": "real_regression",
            "requested_by": "test",
        },
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert re.fullmatch(r"run_[0-9A-HJKMNP-TV-Z]{26}", run_id)

    # The background task finishes while the client drains; poll the run rather than
    # sleeping on a wall clock. A short pause per poll keeps the budget generous without
    # making the test slow when the run finishes quickly (a stage now includes the
    # fingerprint scan and the memory writes, and 200 back-to-back polls were not enough).
    for _ in range(400):
        outcome = client.get(f"/v1/runs/{run_id}").json()
        if outcome["status"] != "in_progress":
            break
        time.sleep(0.01)
    assert outcome["status"] == "awaiting_approval"
    assert outcome["run_id"] == run_id
    assert outcome["final"]["diagnosis"]["category"] == "real_regression"


def test_live_mode_is_refused_rather_than_half_working(client: TestClient) -> None:
    response = client.post(
        "/v1/runs",
        json={
            "integration": "cicd",
            "subject": _webhook(),
            "idempotency_key": "cicd:test-live-mode-key",
            "mode": "live",
        },
    )
    assert response.status_code == 501
    assert "replay" in response.json()["detail"]


def test_unknown_run_is_404(client: TestClient) -> None:
    response = client.get("/v1/runs/run_00000000000000000000000000")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def _scenario_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "fixtures/scenarios/real_regression"


def _webhook() -> dict[str, object]:
    return json.loads((_scenario_dir() / "webhook.json").read_text(encoding="utf-8"))


def _expected_commit() -> str:
    text = (_scenario_dir() / "scenario.yaml").read_text(encoding="utf-8")
    match = re.search(r"^\s*commit:\s*([0-9a-f]{40})\s*$", text, re.MULTILINE)
    assert match is not None, "scenario.yaml must pin expected.commit"
    return match.group(1)
