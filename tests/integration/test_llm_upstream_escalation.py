"""The user's `llm_upstream` ruling (harness-core.md), end to end.

`llm_upstream` is now its own member of `EscalationRecord.reason` and its own row in
`Orchestrator._OUTCOME_FOR_ERROR_KIND`, mapped to `("escalated", "llm_upstream")` rather
than collapsing into the generic `("escalated", "tool_failure")` a `ToolError` produces.
Driven through the real orchestrator: a stub LLM whose `generate()` always raises
`LlmUpstreamError` exhausts the transient retry budget and the run must escalate as
`llm_upstream`, not `tool_failure` -- distinguishing "the model was unreachable" from "a
tool call failed" is the entire point of the new member, per B.1's last row.

`asyncio.sleep` is patched so the four-attempt transient budget does not spend real wall
time on backoff.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.harness import recovery as recovery_module
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import RunRequest
from src.harness.guardrails import PolicyEngine
from src.harness.llm import LlmRequest, LlmUpstreamError, RawLlmResponse
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.integrations.cicd.gateway_replay import ReplayToolGateway
from src.integrations.cicd.wiring import build_orchestrator, load_policy_spec

REPO = "octo-org/harness-demo-repo"


class AlwaysUpstreamFailingLlm:
    """Every call raises the exact exception `GeminiClient.generate` raises for a 503/504
    or a connection failure -- see `classify_provider_error`'s last non-timeout branch.
    """

    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        raise LlmUpstreamError("provider unreachable")


@pytest.fixture(autouse=True)
def _no_real_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(recovery_module.asyncio, "sleep", fake_sleep)


@pytest.fixture
def scenario_dir(repo_root: Path) -> Path:
    return repo_root / "fixtures" / "scenarios" / "real_regression"


async def test_a_persistently_unreachable_provider_escalates_as_llm_upstream(
    scenario_dir: Path,
) -> None:
    gateway = ReplayToolGateway(scenario_dir=scenario_dir, repo=REPO, forbidden=())
    orchestrator = build_orchestrator(
        gateway=gateway,
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=120_000)),
        llm=AlwaysUpstreamFailingLlm(),
        recorder=TraceRecorder(
            db_path=Path("unused.db"), redactor=Redactor(SecretRegistry(), ())
        ),
        engine=PolicyEngine(load_policy_spec()),
        escalation_threshold=0.70,
        investigator_model="stub-model",
        diagnostician_model="stub-model",
        remediator_model="stub-model",
    )
    webhook = json.loads(
        (scenario_dir / "webhook.json").read_text(encoding="utf-8")
    )
    request = RunRequest(
        integration="cicd",
        subject=webhook,
        idempotency_key="cicd:test-llm-upstream-escalation",
        mode="replay",
        replay_fixture="real_regression",
        requested_by="test",
    )

    outcome = await orchestrator.run(request)

    assert outcome.status == "escalated"
    assert outcome.escalation is not None
    assert outcome.escalation.reason == "llm_upstream"
    assert outcome.escalation.reason != "tool_failure"
