"""Step-5 plan Stage 1a: the transient retry policy is a setting.

`AppContext.build_orchestrator_for` used to build every agent with `RetryPolicy()`, so the
free tier's billed `503`s could only be retried Appendix B.1's way: four attempts,
sub-second jitter. The four `HARNESS_LLM_*` settings now reach every agent's policy, and
with none of them set the policy is still `RetryPolicy()`.

`tests/conftest.py` pins the four variables to B.1's defaults for every other test, so a
free-tier profile in the operator's `.env` changes nothing the suite counts. The tests here
set them on purpose.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.deps import FIXTURES_ROOT, SECRET_PATTERNS, AppContext, build_secret_registry
from src.harness import recovery as recovery_module
from src.harness.agent import LLMAgent
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.llm import LlmRequest, LlmUpstreamError, RawLlmResponse
from src.harness.observability import Redactor, TraceRecorder
from src.harness.recovery import RetryPolicy
from src.settings import Settings, get_settings
from tests.stubs import ScenarioStubLlm

REPO = "octo-org/harness-demo-repo"

FREE_TIER_PROFILE = {
    "HARNESS_LLM_TRANSIENT_MAX_ATTEMPTS": "2",
    "HARNESS_LLM_BACKOFF_BASE_S": "5",
    "HARNESS_LLM_BACKOFF_MAX_S": "15",
    "HARNESS_LLM_BACKOFF_JITTER": "none",
}
FREE_TIER_POLICY = RetryPolicy(
    transient_max_attempts=2, backoff_base_s=5.0, backoff_max_s=15.0, jitter="none"
)


class AlwaysUpstreamFailingLlm:
    """What `GeminiClient.generate` raises for a `503` -- on every call."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        self.calls += 1
        raise LlmUpstreamError("provider unreachable")


def make_context(settings: Settings, tmp_db_path: Path, llm: Any) -> AppContext:
    recorder = TraceRecorder(
        db_path=tmp_db_path,
        redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
    )
    return AppContext(
        settings=settings,
        recorder=recorder,
        context_manager=ContextManager(
            default_budget=ContextBudget(total_chars=settings.log_char_budget)
        ),
        llm=llm,
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )


def agent_policies(context: AppContext) -> dict[str, RetryPolicy]:
    gateway = context.build_replay_gateway(FIXTURES_ROOT / "real_regression", REPO)
    orchestrator = context.build_orchestrator_for(gateway)
    assert orchestrator.agents, "the orchestrator built no agents"
    # The model-backed agents; the Evaluator makes no model call and has no policy.
    return {
        key: agent.retry_policy
        for key, agent in orchestrator.agents.items()
        if isinstance(agent, LLMAgent)
    }


def test_unset_settings_give_every_agent_the_default_policy(
    tmp_path: Path, tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Out of the repository's cwd, so no `.env` is read; the pins conftest sets go too.
    monkeypatch.chdir(tmp_path)
    for name in FREE_TIER_PROFILE:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HARNESS_GEMINI_API_KEY", "unused-gemini-key-for-tests")
    monkeypatch.setenv("HARNESS_GITHUB_TOKEN", "unused-github-token-for-tests")
    monkeypatch.setenv("HARNESS_GITHUB_WEBHOOK_SECRET", "unused-webhook-secret-for-tests")
    get_settings.cache_clear()

    policies = agent_policies(make_context(get_settings(), tmp_db_path, ScenarioStubLlm()))

    assert len(policies) == 3, policies
    assert all(policy == RetryPolicy() for policy in policies.values()), policies


def test_the_free_tier_profile_reaches_every_agent(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in FREE_TIER_PROFILE.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    policies = agent_policies(make_context(get_settings(), tmp_db_path, ScenarioStubLlm()))

    assert len(policies) == 3, policies
    for key, policy in policies.items():
        assert policy == FREE_TIER_POLICY, (key, policy)
        # The schema retries and the timeout are not the profile's to change.
        assert policy.max_attempts == RetryPolicy().max_attempts
        assert policy.timeout_s == RetryPolicy().timeout_s


def test_under_the_profile_an_overloaded_investigator_costs_two_requests(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    # `recovery.py` uses `asyncio` for this one call; a shim records the retry loop's
    # sleeps and nobody else's (the orchestrator's heartbeat sleeps too).
    monkeypatch.setattr(recovery_module, "asyncio", SimpleNamespace(sleep=fake_sleep))
    settings = get_settings().model_copy(
        update={
            "llm_transient_max_attempts": 2,
            "llm_backoff_base_s": 5.0,
            "llm_backoff_max_s": 15.0,
            "llm_backoff_jitter": "none",
        }
    )
    llm = AlwaysUpstreamFailingLlm()
    context = make_context(settings, tmp_db_path, llm)
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)

    with TestClient(api_main.app) as client:
        response = client.post("/v1/replay/real_regression")
        assert response.status_code == 200, response.text
        body = response.json()
        trace = client.get(f"/v1/runs/{body['run_id']}/trace").json()

    assert body["status"] == "escalated"
    assert body["escalation"]["reason"] == "llm_upstream"
    attempts = [s for s in trace["spans"] if s["name"] == "llm.attempt"]
    assert len(attempts) == 2, [s["attributes"] for s in attempts]
    assert {s["attributes"]["schema"] for s in attempts} == {"InvestigationNotes"}
    assert llm.calls == 2
    # One sleep between the two attempts: backoff_delay(1) = min(15, 5 * 2**0), unjittered.
    assert slept == [5.0]
