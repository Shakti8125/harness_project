# ruff: noqa: E501
"""`GET /runs/{run_id}/view` and the component set -- PLAN.md Phase 5 Verify step 1,
in-process over the stub model.

The literal step is a browser and a live run; this pins what the page must contain for
that run: >= 12 spans, every stage's confidence and itemised adjustments, the policy card
quoting rule `open-fix-pr` with effect `require_approval`, and the eight components on
the trace.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.deps import SECRET_PATTERNS, AppContext, build_secret_registry
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.observability import Redactor, TraceRecorder
from src.integrations.cicd.wiring import FAULT_FABRICATE_CITATION
from src.settings import get_settings
from tests.stubs import ScenarioStubLlm

EXPECTED_COMPONENTS = [
    "agent", "context_manager", "evaluator", "gateway", "guardrails", "llm", "memory", "orchestrator",
]


def make_client(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch, *, fault: str | None = None, llm: Any | None = None
) -> TestClient:
    settings = get_settings().model_copy(update={"fault_inject": fault})
    recorder = TraceRecorder(
        db_path=tmp_db_path, redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS)
    )
    context = AppContext(
        settings=settings,
        recorder=recorder,
        context_manager=ContextManager(
            default_budget=ContextBudget(total_chars=settings.log_char_budget), recorder=recorder
        ),
        llm=llm or ScenarioStubLlm(),
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    return TestClient(api_main.app)


@pytest.fixture
def client(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with make_client(tmp_db_path, monkeypatch) as test_client:
        yield test_client


def test_verify_step_1_trace_view_and_component_set(client: TestClient) -> None:
    body = client.post("/v1/replay/real_regression").json()
    run_id = body["run_id"]

    trace = client.get(f"/v1/runs/{run_id}/trace").json()
    assert len(trace["spans"]) >= 12
    # `jq '[.spans[].component] | unique'`
    assert sorted({s["component"] for s in trace["spans"]}) == EXPECTED_COMPONENTS
    names = {s["name"] for s in trace["spans"]}
    assert {
        "run", "stage", "agent.run", "prompt.render", "llm.attempt", "context.assemble",
        "gateway.invoke", "memory.lookup", "memory.upsert_signature", "memory.record_observation",
        "evaluation.claim", "remediation.plan", "policy.decide",
    } <= names

    page = client.get(f"/runs/{run_id}/view")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    html = page.text
    assert run_id in html
    # The waterfall draws every span.
    assert html.count('class="row"') == len(trace["spans"])
    for component in EXPECTED_COMPONENTS:
        assert f"c-{component}" in html
    # Every stage, with the diagnosis's confidence and each adjustment itemised.
    for stage in ("investigate", "diagnose", "evaluate", "remediate"):
        assert f'id="{stage}"' in html
    diagnosis = body["final"]["diagnosis"]
    assert f"self-reported <b>{diagnosis['self_confidence']:.2f}</b>" in html
    assert f"final <b>{diagnosis['final_confidence']:.2f}</b>" in html
    for adjustment in diagnosis["confidence_adjustments"]:
        assert adjustment["name"] in html
    # The policy card quotes the rule and the effect.
    assert "open-fix-pr" in html and "require_approval" in html
    assert "- id: open-fix-pr" in html and "effect: require_approval" in html
    # Citations beside their verdicts.
    for citation in diagnosis["citations"]:
        assert citation["quote"] in html
    assert 'class="pill verified">verified' in html
    # Header numbers agree with the JSON body.
    assert str(body["total_tokens"]["total"]) in html
    assert f"/v1/runs/{run_id}/trace" in html


def test_view_of_an_escalated_run_shows_the_reason_and_the_gated_stage(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_client(tmp_db_path, monkeypatch, fault=FAULT_FABRICATE_CITATION) as client:
        body = client.post("/v1/replay/real_regression").json()
        assert body["status"] == "escalated"
        html = client.get(f"/runs/{body['run_id']}/view").text
    assert "evidence_refuted" in html
    assert body["escalation"]["message"] in html
    assert 'class="pill refuted">refuted' in html
    assert "quote not found" in html
    assert 'class="pill gated">gated' in html
    assert "1 of 1 claim(s) refuted" in html


def test_view_of_an_unknown_run_is_a_problem_document(client: TestClient) -> None:
    response = client.get("/runs/run_00000000000000000000000000/view")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_view_serves_what_the_json_serves_never_the_raw_log(client: TestClient) -> None:
    """The page is rendered from the digested, scrubbed body: the log excerpt is a length
    and a digest on the JSON route, and it is not on the page either."""
    run_id = client.post("/v1/replay/real_regression").json()["run_id"]
    # The stored run, not the synchronous reply: the store scrubs the excerpt at rest (the
    # fixture's pasted token becomes the placeholder), so the digest `GET /v1/runs/{id}`
    # serves describes the stored text, and the page is rendered from that same body.
    body = client.get(f"/v1/runs/{run_id}").json()
    html = client.get(f"/runs/{run_id}/view").text
    excerpt = body["final"]["bundle"]["logs"][0]
    assert "excerpt_sha256" in excerpt
    assert str(excerpt["excerpt_length"]) in html
    assert "##[group]Run pip install" not in html
    # And the fixture's pasted token, which the log carries, is nowhere on the page.
    assert "ghp_" not in html
