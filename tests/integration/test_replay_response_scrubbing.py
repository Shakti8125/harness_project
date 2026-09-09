"""Wave-3 audit finding 3, at the real HTTP boundary: `POST /v1/replay/{scenario}`.

`test_serialize_run_outcome.py` pins the transformation as a pure function; this file
drives the actual FastAPI route (the real orchestrator, the real replay gateway, the real
`ContextManager` over the real fixture log) with a stubbed LLM -- same shape as
`test_replay_e2e.py`, so zero live model calls and zero quota spent -- and asserts on
`response.json()` / `response.text` exactly as a caller would see them.

Includes the regression the reviewer's report named explicitly but could not yet test
(no fixture carries a credential today): a `ghp_`-shaped token planted into a *copy* of
the log fixture (never the real one under `fixtures/`, which stays untouched — this test
writes only under `tmp_path`) must not appear anywhere in the served response text, on
either the log-excerpt path or the diff-patch path.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.deps import SECRET_PATTERNS, AppContext, build_secret_registry
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.observability import Redactor, TraceRecorder
from src.settings import get_settings
from tests.integration.test_replay_e2e import StubLlm

SECRET_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # 36 chars after ghp_


@pytest.fixture
def client(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
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
        llm=StubLlm(),
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    return TestClient(api_main.app)


def test_excerpt_and_patch_are_absent_from_the_real_response(client: TestClient) -> None:
    response = client.post("/v1/replay/real_regression")
    assert response.status_code == 200
    body = response.json()

    log_entry = body["final"]["bundle"]["logs"][0]
    assert "excerpt" not in log_entry
    assert isinstance(log_entry["excerpt_length"], int) and log_entry["excerpt_length"] > 0
    assert len(log_entry["excerpt_sha256"]) == 64

    for file_entry in body["final"]["bundle"]["diff"]["files"]:
        assert "patch" not in file_entry
        assert isinstance(file_entry["path"], str)
        assert isinstance(file_entry["status"], str)
        assert isinstance(file_entry["additions"], int)
        assert isinstance(file_entry["deletions"], int)

    # The harness's actual designed evidence surface must survive byte-for-byte.
    citations = body["final"]["diagnosis"]["citations"]
    assert citations
    assert all(citation["quote"] for citation in citations)

    # Unaffected by this change, but shares the response body worth re-checking here.
    assert body["status"] == "completed"
    assert body["final"]["diagnosis"]["category"] == "real_regression"
    assert body["final"]["diagnosis"]["suggested_action"] == "open_fix_pr"
    assert body["run_id"]
    assert body["trace_url"]
    assert body["stages"]


def test_response_is_materially_smaller_than_an_unscrubbed_dump(client: TestClient) -> None:
    response = client.post("/v1/replay/real_regression")
    assert len(response.content) < 20_000


@pytest.fixture
def scenario_with_a_pasted_token(tmp_path: Path, repo_root: Path) -> Path:
    """A private copy of `real_regression`, never the real fixture, with a `ghp_`-shaped
    token planted right beside the anchored assertion line so it survives the context
    budget's trim (PLAN.md:862-866's Phase-5 fixture, brought forward)."""
    source = repo_root / "fixtures" / "scenarios" / "real_regression"
    poisoned = tmp_path / "real_regression_with_a_secret"
    shutil.copytree(source, poisoned)

    log_path = poisoned / "logs" / "job_601234567.txt"
    text = log_path.read_text(encoding="utf-8")
    marker = "E       assert 91 == 90"
    assert marker in text, "fixture layout changed; the injection anchor moved"
    injected = text.replace(
        marker,
        f"{marker}\nleaked credential in CI output: {SECRET_TOKEN}",
        1,
    )
    log_path.write_text(injected, encoding="utf-8")
    return poisoned


async def _run_against(scenario_dir: Path) -> dict[str, object]:
    from src.harness.contracts import RunRequest
    from src.integrations.cicd.gateway_replay import ReplayToolGateway
    from src.integrations.cicd.wiring import build_orchestrator

    settings = get_settings()
    recorder = TraceRecorder(
        db_path=Path("unused.db"),
        redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
    )
    gateway = ReplayToolGateway(
        scenario_dir=scenario_dir, repo="octo-org/harness-demo-repo", forbidden=()
    )
    orchestrator = build_orchestrator(
        gateway=gateway,
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=120_000)),
        llm=StubLlm(),
        recorder=recorder,
        escalation_threshold=0.70,
        investigator_model="stub-model",
        diagnostician_model="stub-model",
    )
    webhook = json.loads((scenario_dir / "webhook.json").read_text(encoding="utf-8"))
    request = RunRequest(
        integration="cicd",
        subject=webhook,
        idempotency_key="cicd:test-secret-leak-regression",
        mode="replay",
        replay_fixture="real_regression",
        requested_by="test",
    )
    outcome = await orchestrator.run(request)
    from src.api.main import _serialize_run_outcome

    return _serialize_run_outcome(outcome)


async def test_a_pasted_token_in_the_log_never_reaches_the_served_response(
    scenario_with_a_pasted_token: Path,
) -> None:
    body = await _run_against(scenario_with_a_pasted_token)
    served_text = json.dumps(body)

    # Confirm the premise: the token really was in what the Investigator read, so a
    # missing assertion below is the scrub working, not the token never having been
    # collected in the first place.
    assert SECRET_TOKEN not in served_text

    log_entry = body["final"]["bundle"]["logs"][0]
    assert "excerpt" not in log_entry
    assert "excerpt_length" in log_entry and "excerpt_sha256" in log_entry


async def test_the_premise_the_token_was_actually_in_scope_pre_scrub(
    scenario_with_a_pasted_token: Path,
) -> None:
    """Without this, the test above could pass vacuously (token never collected, budgeted
    out, or otherwise absent from the pipeline's evidence for reasons unrelated to the
    scrub). Confirms the raw, pre-serialisation `RunOutcome` really does carry it."""
    from src.harness.contracts import RunRequest
    from src.integrations.cicd.gateway_replay import ReplayToolGateway
    from src.integrations.cicd.wiring import build_orchestrator

    settings = get_settings()
    recorder = TraceRecorder(
        db_path=Path("unused.db"),
        redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
    )
    gateway = ReplayToolGateway(
        scenario_dir=scenario_with_a_pasted_token,
        repo="octo-org/harness-demo-repo",
        forbidden=(),
    )
    orchestrator = build_orchestrator(
        gateway=gateway,
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=120_000)),
        llm=StubLlm(),
        recorder=recorder,
        escalation_threshold=0.70,
        investigator_model="stub-model",
        diagnostician_model="stub-model",
    )
    webhook = json.loads(
        (scenario_with_a_pasted_token / "webhook.json").read_text(encoding="utf-8")
    )
    request = RunRequest(
        integration="cicd",
        subject=webhook,
        idempotency_key="cicd:test-secret-leak-premise",
        mode="replay",
        replay_fixture="real_regression",
        requested_by="test",
    )
    outcome = await orchestrator.run(request)
    unscrubbed = json.dumps(outcome.model_dump(mode="json"))

    assert SECRET_TOKEN in unscrubbed, (
        "the injected token never reached the raw RunOutcome -- the scrub test above "
        "would be vacuous"
    )
