"""`review-2.md` finding 1's second half: the `status="ok"` hardcode at
`investigator.py:550` used to let a rate-limited Investigator sail through as a success,
so the Diagnostician ran next and hit the identical wall -- two agents' worth of
`transient_max_attempts` and provider-honoured sleeps for a guaranteed failure.

The fix (`cicd-integration`'s half of the bundle) is `_UPSTREAM_ERROR_KINDS` in
`investigator.py`: when the notes call's terminal `AgentError.kind` is one of
`llm_rate_limited`, `llm_timeout`, `llm_auth`, `llm_upstream` or `internal`,
`Investigator.run` returns the failed `AgentResult` `super().run` already built instead
of degrading and continuing. This file asserts the actually-observable consequence, per
the dispatch instruction: the failure status is reported (not merely that a sleep got
shorter), the run escalates on the reason the harness derives for that kind, and the
Diagnostician never runs at all.

A companion test pins the other half of the same `if`: `invalid_output` (a content-level
failure -- the model was reachable, its output just did not validate) must keep the old
degrade-and-continue behaviour, so the distinction between "the provider is unreachable"
and "the model said something we couldn't parse" cannot silently collapse in either
direction.

No live model call, no real sleep: `asyncio.sleep` is patched exactly as
`test_llm_upstream_escalation.py` does, so the four-attempt transient budget costs no
wall time here (test (c), the wall-clock assertion on the delay budget itself, lives in
`tests/unit/test_retry_delay_budget.py`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.harness import recovery as recovery_module
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import RunRequest, TokenUsage
from src.harness.guardrails import PolicyEngine
from src.harness.llm import LlmRateLimited, LlmRequest, RawLlmResponse
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.integrations.cicd.gateway_replay import ReplayToolGateway
from src.integrations.cicd.wiring import build_orchestrator, load_policy_spec
from tests.stubs import remediation_plan

REPO = "octo-org/harness-demo-repo"

VALID_DIAGNOSIS = {
    "reasoning": "The log shows assert 91 == 90 and the diff changes discount.py.",
    "category": "real_regression",
    "summary": "An off-by-one in discount().",
    "self_confidence": 0.9,
    "citations": [
        {
            "claim_kind": "quote_exists",
            "locator": "log:job/601234567",
            "quote": "assert 91 == 90",
            "note": "the failing assertion",
        },
    ],
    "suspected_commit_sha": None,
    "suspected_test_ids": [],
    "suspected_package": None,
    "suggested_action": "open_fix_pr",
}


class AlwaysRateLimitedForInvestigatorLlm:
    """Every Investigator call is rate limited, permanently -- exhausts the transient
    budget (`TRANSIENT_MAX_ATTEMPTS = 4`) and hands `retry_structured` a terminal
    `AgentError(kind="llm_rate_limited")`. If the Diagnostician is ever asked anything,
    that is the bug this file exists to catch, so it fails loudly rather than answering.
    """

    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        if "You are the Investigator" in req.prompt:
            raise LlmRateLimited("provider rate limited the request", retry_after_s=None)
        raise AssertionError(
            "the Diagnostician must never run after the Investigator's notes call "
            "exhausts on an upstream failure -- review-2.md finding 1"
        )


class PermanentlyInvalidNotesLlm:
    """The Investigator's notes call always returns text that cannot possibly validate
    against `InvestigationNotes` -- a content-level failure (`invalid_output`), not an
    upstream one. The control case: this must still degrade-and-continue, with the
    Diagnostician running normally on the deterministic collection alone.
    """

    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        if "You are the Investigator" in req.prompt:
            return RawLlmResponse(
                text="this is not json and never will validate",
                tokens=TokenUsage(prompt=100, completion=10, total=110),
                finish_reason="STOP",
                model=req.model,
                latency_ms=5,
            )
        if "You are the Remediator" in req.prompt:
            return RawLlmResponse(
                text=json.dumps(remediation_plan()),
                tokens=TokenUsage(prompt=1000, completion=200, total=1200),
                finish_reason="STOP",
                model=req.model,
                latency_ms=5,
            )
        if "You are the Diagnostician" in req.prompt:
            return RawLlmResponse(
                text=json.dumps(VALID_DIAGNOSIS),
                tokens=TokenUsage(prompt=1000, completion=200, total=1200),
                finish_reason="STOP",
                model=req.model,
                latency_ms=5,
            )
        raise AssertionError("unrecognised prompt reached the stub model")  # pragma: no cover


@pytest.fixture(autouse=True)
def _no_real_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(recovery_module.asyncio, "sleep", fake_sleep)


@pytest.fixture
def scenario_dir(repo_root: Path) -> Path:
    return repo_root / "fixtures" / "scenarios" / "real_regression"


def _make_recorder() -> TraceRecorder:
    return TraceRecorder(db_path=Path("unused.db"), redactor=Redactor(SecretRegistry(), ()))


async def _run(scenario_dir: Path, llm: object, idempotency_key: str) -> object:
    gateway = ReplayToolGateway(scenario_dir=scenario_dir, repo=REPO, forbidden=())
    orchestrator = build_orchestrator(
        gateway=gateway,
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=120_000)),
        llm=llm,
        recorder=_make_recorder(),
        escalation_threshold=0.70,
        investigator_model="stub-model",
        diagnostician_model="stub-model",
        remediator_model="stub-model",
        engine=PolicyEngine(load_policy_spec()),
    )
    webhook = json.loads((scenario_dir / "webhook.json").read_text(encoding="utf-8"))
    request = RunRequest(
        integration="cicd",
        subject=webhook,
        idempotency_key=idempotency_key,
        mode="replay",
        replay_fixture="real_regression",
        requested_by="test",
    )
    return await orchestrator.run(request)


async def test_investigator_upstream_exhaustion_ends_the_run_before_diagnostician_runs(
    scenario_dir: Path,
) -> None:
    outcome = await _run(
        scenario_dir,
        AlwaysRateLimitedForInvestigatorLlm(),
        "cicd:test-investigator-upstream-exhaustion",
    )

    # The failure status is actually reported -- not merely that a sleep got shorter.
    investigate_stages = [s for s in outcome.stages if s.stage == "investigate"]
    assert len(investigate_stages) == 1
    assert investigate_stages[0].status != "ok"

    # The run escalates on the reason the harness derives for `llm_rate_limited`
    # (`Orchestrator._OUTCOME_FOR_ERROR_KIND`): `rate_limited`, not a generic failure.
    assert outcome.status == "escalated"
    assert outcome.escalation is not None
    assert outcome.escalation.reason == "rate_limited"

    # The point of the whole fix: the Diagnostician never runs. If it had,
    # `AlwaysRateLimitedForInvestigatorLlm.generate` would have raised `AssertionError`
    # instead of the orchestrator observing a clean escalation -- so a failure here would
    # already have surfaced as an error, not a silent pass. Asserted directly too, for a
    # readable failure message.
    diagnose_stages = [s for s in outcome.stages if s.stage == "diagnose"]
    assert diagnose_stages == []
    assert not outcome.final


async def test_investigator_invalid_output_still_degrades_and_continues(
    scenario_dir: Path,
) -> None:
    """The control case: a content-level notes failure (the model was reachable, its
    output just never validated) must NOT trip the upstream short-circuit. The
    Investigator stage still succeeds on its deterministic collection, the run degrades
    with `investigator_notes`, and the Diagnostician runs normally.
    """
    outcome = await _run(
        scenario_dir,
        PermanentlyInvalidNotesLlm(),
        "cicd:test-investigator-invalid-output-degrades",
    )

    investigate_stages = [s for s in outcome.stages if s.stage == "investigate"]
    assert len(investigate_stages) == 1
    assert investigate_stages[0].status == "ok"
    assert "investigator_notes" in outcome.degraded_components

    diagnose_stages = [s for s in outcome.stages if s.stage == "diagnose"]
    assert len(diagnose_stages) == 1
    assert diagnose_stages[0].status == "ok"

    assert outcome.final is not None
    assert outcome.final["bundle"]["notes"] is None
