"""Wave-3 audit finding 1: `gateway_degraded` must fire on a required read tool failing,
and ONLY on a required read tool failing.

Reproduces the reviewer's exact regression: an optional `additional_tool_calls` entry
the Investigator's model asks for, that 404s against the replay fixture, must never
touch `bundle.gateway_errors` (and so must never move `final_confidence` or add a
`gateway_degraded` adjustment) -- and, as the negative case a one-sided fix could still
pass, a genuinely failed REQUIRED read (the job log) still fires the -0.10 with the
`"a required read tool returned an error"` reason.

Drives the real orchestrator (`build_orchestrator`) over the real `real_regression`
fixture through a `ReplayToolGateway`, bypassing HTTP entirely -- no live model call, the
same stubbed-LLM shape `tests/integration/test_replay_e2e.py` uses.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import RunRequest, TokenUsage
from src.harness.guardrails import PolicyEngine
from src.harness.llm import LlmRequest, RawLlmResponse
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.integrations.cicd.gateway_replay import ReplayToolGateway
from src.integrations.cicd.wiring import build_orchestrator, load_policy_spec
from tests.stubs import remediation_plan

REPO = "octo-org/harness-demo-repo"
HEAD_SHA = "e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df"
JOB_ID = 601234567

BASE_NOTES = {
    "observations": [
        "tests/test_pricing.py::test_discount_applies fails with assert 91 == 90",
    ],
    "additional_tool_calls": [],
    "narrative": "A pricing test regresses on a diff that touches the discount helper.",
}

BASE_DIAGNOSIS = {
    "reasoning": (
        "The log shows assert 91 == 90 and the diff changes src/pricing/discount.py."
    ),
    "category": "real_regression",
    "summary": "An off-by-one in discount().",
    "self_confidence": 0.95,
    "citations": [
        {
            "claim_kind": "quote_exists",
            "locator": "log:job/601234567",
            "quote": "assert 91 == 90",
            "note": "the failing assertion",
        },
    ],
    "suspected_commit_sha": HEAD_SHA,
    "suspected_test_ids": ["tests/test_pricing.py::test_discount_applies"],
    "suspected_package": None,
    "suggested_action": "open_fix_pr",
}


class StubLlm:
    """Same dispatch-by-preamble shape as `test_replay_e2e.StubLlm`, duplicated locally
    so this file has no cross-test-module import dependency; kept intentionally minimal.
    """

    def __init__(self, notes: dict[str, object]) -> None:
        self.notes = notes

    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        if "You are the Investigator" in req.prompt:
            payload: object = self.notes
        elif "You are the Diagnostician" in req.prompt:
            payload = BASE_DIAGNOSIS
        elif "You are the Remediator" in req.prompt:
            payload = remediation_plan()
        else:  # pragma: no cover
            raise AssertionError("unrecognised prompt reached the stub model")
        return RawLlmResponse(
            text=json.dumps(payload),
            tokens=TokenUsage(prompt=1000, completion=200, total=1200),
            finish_reason="STOP",
            model=req.model,
            latency_ms=5,
        )


def _make_recorder() -> TraceRecorder:
    return TraceRecorder(
        db_path=Path("unused.db"), redactor=Redactor(SecretRegistry(), ())
    )


def _run_request() -> RunRequest:
    webhook = json.loads(
        (Path("fixtures/scenarios/real_regression/webhook.json")).read_text(
            encoding="utf-8"
        )
    )
    return RunRequest(
        integration="cicd",
        subject=webhook,
        idempotency_key="cicd:test-gateway-error-scoping",
        mode="replay",
        replay_fixture="real_regression",
        requested_by="test",
    )


async def _run(scenario_dir: Path, notes: dict[str, object]):
    gateway = ReplayToolGateway(scenario_dir=scenario_dir, repo=REPO, forbidden=())
    orchestrator = build_orchestrator(
        gateway=gateway,
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=120_000)),
        llm=StubLlm(notes),
        recorder=_make_recorder(),
        escalation_threshold=0.70,
        investigator_model="stub-model",
        diagnostician_model="stub-model",
        remediator_model="stub-model",
        engine=PolicyEngine(load_policy_spec()),
    )
    return await orchestrator.run(_run_request())


@pytest.fixture
def scenario_dir(repo_root: Path) -> Path:
    return repo_root / "fixtures" / "scenarios" / "real_regression"


async def test_an_optional_tool_call_404_never_fires_gateway_degraded(
    scenario_dir: Path,
) -> None:
    """The reviewer's exact regression: `get_file_contents` for a file the fixture does
    not carry a recorded response for -- a `not_found` `ToolError` on an OPTIONAL call.
    """
    notes = {
        **BASE_NOTES,
        "additional_tool_calls": [
            {
                "call_id": "tc_0000000000ab",
                "tool": "get_file_contents",
                "args": {"path": "src/pricing.py", "ref": HEAD_SHA},
            }
        ],
    }
    # Confirm the premise: no recorded fixture exists for this path, so the gateway
    # really does return `not_found` rather than the test accidentally passing vacuously.
    assert not (
        scenario_dir
        / "api"
        / "GET_repos-octo-org-harness-demo-repo-contents-src-pricing.py.json"
    ).is_file()

    outcome = await _run(scenario_dir, notes)

    diagnosis = outcome.final["diagnosis"]
    assert diagnosis["final_confidence"] == pytest.approx(0.95)
    assert diagnosis["confidence_adjustments"] == []
    bundle = outcome.final["bundle"]
    assert bundle["gateway_errors"] == []


async def test_a_genuinely_failed_required_read_still_fires_gateway_degraded(
    tmp_path: Path, scenario_dir: Path
) -> None:
    """The negative case a one-sided fix (silencing the row entirely) would miss: a
    REQUIRED deterministic-collection call -- the job log fetch -- failing must still
    apply the -0.10 adjustment with the exact reason string.
    """
    broken_scenario = tmp_path / "real_regression_missing_log"
    shutil.copytree(scenario_dir, broken_scenario)
    (broken_scenario / "logs" / f"job_{JOB_ID}.txt").unlink()

    outcome = await _run(broken_scenario, dict(BASE_NOTES))

    diagnosis = outcome.final["diagnosis"]
    bundle = outcome.final["bundle"]
    assert bundle["gateway_errors"], "the missing log fixture must produce a required error"
    assert bundle["gateway_errors"][0]["kind"] == "not_found"

    adjustments = {row["name"]: row for row in diagnosis["confidence_adjustments"]}
    assert adjustments["gateway_degraded"]["delta"] == pytest.approx(-0.10)
    assert (
        adjustments["gateway_degraded"]["reason"]
        == "a required read tool returned an error (not_found)"
    )
    assert diagnosis["final_confidence"] == pytest.approx(0.85)


async def test_a_refused_write_tool_request_carries_no_confidence_signal(
    scenario_dir: Path,
) -> None:
    """A model naming a write tool in `additional_tool_calls` is refused before any
    request reaches the gateway -- it must not appear in `gateway_errors` at all, and
    must not move `final_confidence` in either direction (the fix round's documented
    decision: no adjustment row covers "the model asked for something it wasn't allowed
    to have").
    """
    notes = {
        **BASE_NOTES,
        "additional_tool_calls": [
            {
                "call_id": "tc_0000000000cd",
                "tool": "merge_pull_request",
                "args": {"pr_number": 1},
            }
        ],
    }

    outcome = await _run(scenario_dir, notes)

    diagnosis = outcome.final["diagnosis"]
    bundle = outcome.final["bundle"]
    assert bundle["gateway_errors"] == []
    assert diagnosis["final_confidence"] == pytest.approx(0.95)
    assert diagnosis["confidence_adjustments"] == []
