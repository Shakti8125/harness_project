"""The remediate stage, end to end through the orchestrator, over the replay gateway.

Verify step 1 as amended (the cap denies, and the trace says why), the allow path with a
readable history, the forbidden-plan path, the derived-calls path, and the way a
Remediator model failure ends the run. Everything below the stubbed model is real.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.api.deps import SECRET_PATTERNS, build_secret_registry
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import RunRequest, TokenUsage
from src.harness.gateway import ToolCall, ToolGateway, ToolResult, ToolSpec
from src.harness.guardrails import RULE_DEFAULT, RULE_FORBIDDEN, PolicyDecision, PolicyEngine
from src.harness.llm import LlmRequest, LlmUpstreamError, RawLlmResponse
from src.harness.observability import Redactor, TraceRecorder
from src.harness.orchestrator import RunState
from src.integrations.cicd.agents.remediator import Remediator
from src.integrations.cicd.gateway_replay import ReplayToolGateway
from src.integrations.cicd.schemas import (
    Diagnosis,
    DiffSummary,
    FailureBundle,
    JobRef,
    PriorHistory,
    RemediationResult,
)
from src.integrations.cicd.wiring import build_orchestrator, load_forbidden, load_policy_spec
from src.settings import get_settings
from tests.stubs import ScenarioStubLlm, diagnosis_for, remediation_plan

REPO = "octo-org/harness-demo-repo"
FLAKY_HEAD = "4f1e2d3c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e"


def scenario_dir(repo_root: Path, name: str) -> Path:
    return repo_root / "fixtures" / "scenarios" / name


def recorder(tmp_db_path: Path) -> TraceRecorder:
    return TraceRecorder(
        db_path=tmp_db_path,
        redactor=Redactor(build_secret_registry(get_settings()), SECRET_PATTERNS),
    )


async def run_scenario(
    repo_root: Path, tmp_db_path: Path, name: str, llm: Any, *, gateway: ToolGateway | None = None
) -> tuple[Any, TraceRecorder]:
    directory = scenario_dir(repo_root, name)
    rec = recorder(tmp_db_path)
    await rec.initialize()
    gateway = gateway or ReplayToolGateway(
        scenario_dir=directory, repo=REPO, forbidden=load_forbidden(), dry_run=True
    )
    orchestrator = build_orchestrator(
        gateway=gateway,
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=120_000)),
        llm=llm,
        recorder=rec,
        engine=PolicyEngine(load_policy_spec()),
        escalation_threshold=0.70,
        investigator_model="stub",
        diagnostician_model="stub",
        remediator_model="stub",
    )
    webhook = json.loads((directory / "webhook.json").read_text(encoding="utf-8"))
    request = RunRequest(
        integration="cicd",
        subject=webhook,
        idempotency_key=f"cicd:test-{name}",
        mode="replay",
        replay_fixture=name,
        requested_by="test",
    )
    return await orchestrator.run(request), rec


# ---------------------------------------------------------------------------
# Verify step 1, as amended: the cap denies and the trace says which clause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["flaky_test", "infra_timeout"])
async def test_retry_is_denied_by_the_fail_closed_cap_and_the_run_escalates(
    repo_root: Path, tmp_db_path: Path, name: str
) -> None:
    """Phase 3: `run_scenario` builds the orchestrator with no memory store, which is the
    shape a hand-built pipeline has -- and the history is then reported unavailable, so
    the cap still fails closed at 999. The allow path with a real store is
    `test_memory_e2e.py`."""
    outcome, rec = await run_scenario(repo_root, tmp_db_path, name, ScenarioStubLlm())

    assert outcome.status == "escalated"
    assert outcome.escalation is not None
    assert outcome.escalation.reason == "policy_denied"
    assert "memory.retries_for_signature_24h: 999" in outcome.escalation.message

    remediation = RemediationResult.model_validate(outcome.final["remediation"])
    assert remediation.plan.action == "retry_job"
    assert remediation.status == "denied"
    assert remediation.executed == []
    decision = remediation.decisions[0]
    assert decision.tool == "rerun_failed_jobs"
    assert decision.effect == "deny"
    assert decision.rule_id == RULE_DEFAULT
    assert "retry-suspected-flaky" in decision.reason
    assert "fails {lt: 2" in decision.reason

    # The stage list agrees with the status, and the trace carries the decision.
    assert [s.status for s in outcome.stages] == ["ok", "ok", "ok"]
    assert "policy denied" in outcome.stages[-1].summary
    trace = await rec.read_trace(outcome.run_id)
    assert trace is not None
    decide_spans = [s for s in trace.spans if s.name == "policy.decide"]
    assert len(decide_spans) == 1
    assert decide_spans[0].component == "guardrails"
    assert decide_spans[0].attributes["rule_id"] == RULE_DEFAULT
    assert decide_spans[0].attributes["facts"]["memory.retries_for_signature_24h"] == 999  # type: ignore[index]
    # No gateway execution span: nothing ran.
    assert not [s for s in trace.spans if s.name == "remediation.execute"]


async def test_infra_timeout_has_the_deliberately_empty_diff(
    repo_root: Path, tmp_db_path: Path
) -> None:
    outcome, _ = await run_scenario(repo_root, tmp_db_path, "infra_timeout", ScenarioStubLlm())
    bundle = FailureBundle.model_validate(outcome.final["bundle"])
    assert bundle.diff.files == []
    assert bundle.diff.baseline_kind == "branch_green"
    assert bundle.cold_start is False
    diagnosis = Diagnosis.model_validate(outcome.final["diagnosis"])
    assert diagnosis.category == "infra_transient"
    # `empty_diff_contradiction` fires only for a real_regression verdict on this diff.
    assert "empty_diff_contradiction" not in {a.name for a in diagnosis.confidence_adjustments}


# ---------------------------------------------------------------------------
# The allow path: same scenario, a readable history under the cap
# ---------------------------------------------------------------------------


def job_ref(run_id: int = 501234890) -> JobRef:
    return JobRef(
        repo=REPO, workflow_name="CI", workflow_id=9001, run_id=run_id, run_attempt=1,
        job_id=601234890, job_name="test (3.12)", head_sha=FLAKY_HEAD,
        branch="main", event="push", started_at=datetime.now(UTC), completed_at=None,
        conclusion="failure",
    )


def state_with(
    diagnosis: dict[str, Any], *, retries: int, unavailable: bool, cold_start: bool = False
) -> RunState:
    bundle = FailureBundle(
        job=job_ref(),
        logs=[],
        diff=DiffSummary(
            baseline_kind="none" if cold_start else "branch_green",
            base_sha=None if cold_start else "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
            head_sha="4f1e2d3c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e",
        ),
        prior_history=PriorHistory(
            signature_id=None if unavailable else "sig_flaky_0001",
            retries_in_24h=retries,
            unavailable=unavailable,
        ),
        cold_start=cold_start,
        collected_at=datetime.now(UTC),
    )
    return RunState(
        run_id="run_01J8ZZZZZZZZZZZZZZZZZZZZZZ",
        request=RunRequest(
            integration="cicd", subject={}, idempotency_key="cicd:test-state",
            mode="replay", replay_fixture="flaky_test",
        ),
        artifacts={
            "bundle": bundle,
            "diagnosis": Diagnosis.model_validate(
                {**diagnosis, "final_confidence": diagnosis["self_confidence"]}
            ),
        },
        degraded=[],
        stages=[],
    )


class PlanStubLlm:
    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = plan

    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        assert "You are the Remediator" in req.prompt
        return RawLlmResponse(
            text=json.dumps(self.plan), tokens=TokenUsage(prompt=100, completion=50, total=150),
            finish_reason="STOP", model=req.model, latency_ms=1,
        )


class SpyGateway:
    """Wraps a gateway and records every call that reaches `invoke`."""

    integration = "cicd"

    def __init__(self, inner: ToolGateway) -> None:
        self.inner = inner
        self.calls: list[tuple[ToolCall, PolicyDecision]] = []

    def catalog(self) -> list[ToolSpec]:
        return self.inner.catalog()

    async def invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult:
        self.calls.append((call, decision))
        return await self.inner.invoke(call, decision)

    async def aclose(self) -> None:
        await self.inner.aclose()


def remediator(repo_root: Path, tmp_db_path: Path, llm: Any, gateway: ToolGateway) -> Remediator:
    return Remediator(
        llm=llm,
        model="stub",
        recorder=recorder(tmp_db_path),
        gateway=gateway,
        engine=PolicyEngine(load_policy_spec()),
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=120_000)),
    )


async def test_retry_executes_through_the_gateway_when_the_history_is_readable(
    repo_root: Path, tmp_db_path: Path
) -> None:
    """The half of Verify step 1 the live fixture cannot show until Phase 3."""
    gateway = SpyGateway(
        ReplayToolGateway(
            scenario_dir=scenario_dir(repo_root, "flaky_test"), repo=REPO,
            forbidden=load_forbidden(), dry_run=True,
        )
    )
    agent = remediator(
        repo_root, tmp_db_path,
        PlanStubLlm(remediation_plan("retry_job", run_id=501234890)), gateway,
    )
    state = state_with(diagnosis_for("flaky_test"), retries=0, unavailable=False)

    result = await agent.run(state)

    assert result.status == "ok" and result.output is not None
    output = result.output
    assert output.status == "executed"
    assert output.plan.action == "retry_job"
    assert output.decisions[0].rule_id == "retry-suspected-flaky"
    assert output.decisions[0].effect == "allow"
    assert output.decisions[0].obligations == ["record_observation", "annotate_run"]
    assert len(output.executed) == 1
    executed = output.executed[0]
    assert executed.tool == "rerun_failed_jobs"
    assert executed.ok is True
    assert executed.dry_run is True
    # The call the gateway saw is the harness-normalised one, not the model's.
    call, decision = gateway.calls[0]
    assert call.call_id != "tc_model000001"
    assert call.idempotency_key is not None
    assert call.idempotency_key.startswith(f"{state.run_id}:rerun_failed_jobs:")
    assert decision.effect == "allow"


async def test_a_second_retry_in_24h_is_denied(repo_root: Path, tmp_db_path: Path) -> None:
    gateway = SpyGateway(
        ReplayToolGateway(
            scenario_dir=scenario_dir(repo_root, "flaky_test"), repo=REPO,
            forbidden=load_forbidden(),
        )
    )
    agent = remediator(
        repo_root, tmp_db_path,
        PlanStubLlm(remediation_plan("retry_job", run_id=501234890)), gateway,
    )
    result = await agent.run(state_with(diagnosis_for("flaky_test"), retries=2, unavailable=False))
    assert result.output is not None and result.output.status == "denied"
    assert gateway.calls == []


async def test_cold_start_denies_auto_retry(repo_root: Path, tmp_db_path: Path) -> None:
    """Appendix D: no autonomous action against a repo never seen succeeding."""
    gateway = SpyGateway(
        ReplayToolGateway(
            scenario_dir=scenario_dir(repo_root, "flaky_test"), repo=REPO,
            forbidden=load_forbidden(),
        )
    )
    agent = remediator(
        repo_root, tmp_db_path,
        PlanStubLlm(remediation_plan("retry_job", run_id=501234890)), gateway,
    )
    result = await agent.run(
        state_with(diagnosis_for("flaky_test"), retries=0, unavailable=False, cold_start=True)
    )
    assert result.output is not None and result.output.status == "denied"
    assert "context.cold_start" in result.output.decisions[0].reason
    assert gateway.calls == []


# ---------------------------------------------------------------------------
# A hallucinated plan, and a plan with no calls
# ---------------------------------------------------------------------------


async def test_a_forbidden_tool_in_the_plan_is_denied_before_the_gateway_sees_it(
    repo_root: Path, tmp_db_path: Path
) -> None:
    gateway = SpyGateway(
        ReplayToolGateway(
            scenario_dir=scenario_dir(repo_root, "real_regression"), repo=REPO,
            forbidden=load_forbidden(),
        )
    )
    agent = remediator(repo_root, tmp_db_path, PlanStubLlm(remediation_plan("forbidden")), gateway)
    result = await agent.run(
        state_with(diagnosis_for("real_regression"), retries=0, unavailable=False)
    )

    assert result.output is not None
    assert result.output.status == "denied"
    assert result.output.decisions[0].rule_id == RULE_FORBIDDEN
    assert result.output.decisions[0].tool == "merge_pull_request"
    assert gateway.calls == [], "the first enforcement point stops it; the gateway never sees it"


async def test_a_forbidden_call_beside_a_derivable_action_still_denies_the_whole_plan(
    repo_root: Path, tmp_db_path: Path
) -> None:
    """Derivation replaces the model's calls -- except a forbidden one, which is carried
    into the judged plan so it is denied by name and the run escalates, rather than
    vanishing into a span attribute."""
    gateway = SpyGateway(
        ReplayToolGateway(
            scenario_dir=scenario_dir(repo_root, "flaky_test"), repo=REPO,
            forbidden=load_forbidden(),
        )
    )
    plan = remediation_plan("retry_job", run_id=501234890)
    plan["tool_calls"].append(
        {"call_id": "tc_model000009", "tool": "merge_pull_request", "args": {"number": 1},
         "idempotency_key": None}
    )
    rec = recorder(tmp_db_path)
    await rec.initialize()
    agent = Remediator(
        llm=PlanStubLlm(plan), model="stub", recorder=rec, gateway=gateway,
        engine=PolicyEngine(load_policy_spec()),
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=120_000)),
    )
    state = state_with(diagnosis_for("flaky_test"), retries=0, unavailable=False)
    with rec.run_scope(state.run_id):
        result = await agent.run(state)

    assert result.output is not None
    assert result.output.status == "denied"
    tools = [(d.tool, d.rule_id, d.effect) for d in result.output.decisions]
    assert ("rerun_failed_jobs", "retry-suspected-flaky", "allow") in tools
    assert ("merge_pull_request", RULE_FORBIDDEN, "deny") in tools
    assert gateway.calls == [], "one deny and nothing runs, not even the allowed retry"
    trace = await rec.read_trace(state.run_id)
    assert trace is not None
    plan_span = next(s for s in trace.spans if s.name == "remediation.plan")
    proposed = plan_span.attributes["proposed_tool_calls"]
    assert proposed == ["rerun_failed_jobs", "merge_pull_request"]
    assert plan_span.attributes["tool_calls_derived"] is True


async def test_calls_outside_the_action_are_dropped_and_recorded(
    repo_root: Path, tmp_db_path: Path
) -> None:
    """Review finding 2: `no_action` with a `rerun_failed_jobs` attached must not be judged
    (and, with a readable history, executed). The proposal survives only in the trace."""
    gateway = SpyGateway(
        ReplayToolGateway(
            scenario_dir=scenario_dir(repo_root, "flaky_test"), repo=REPO,
            forbidden=load_forbidden(),
        )
    )
    plan = remediation_plan("no_action")
    plan["tool_calls"] = [
        {"call_id": "tc_model000001", "tool": "rerun_failed_jobs",
         "args": {"run_id": 501234890, "attempt": 1}, "idempotency_key": None}
    ]
    rec = recorder(tmp_db_path)
    await rec.initialize()
    agent = Remediator(
        llm=PlanStubLlm(plan), model="stub", recorder=rec, gateway=gateway,
        engine=PolicyEngine(load_policy_spec()),
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=120_000)),
    )
    state = state_with(diagnosis_for("flaky_test"), retries=0, unavailable=False)
    with rec.run_scope(state.run_id):
        result = await agent.run(state)

    assert result.output is not None
    assert result.output.status == "no_action"
    assert result.output.plan.tool_calls == []
    assert result.output.decisions == []
    assert gateway.calls == []
    trace = await rec.read_trace(state.run_id)
    assert trace is not None
    plan_span = next(s for s in trace.spans if s.name == "remediation.plan")
    assert plan_span.attributes["dropped_tool_calls"] == ["rerun_failed_jobs"]


async def test_calls_are_derived_from_the_drafts_when_the_model_proposes_none(
    repo_root: Path, tmp_db_path: Path
) -> None:
    gateway = SpyGateway(
        ReplayToolGateway(
            scenario_dir=scenario_dir(repo_root, "real_regression"), repo=REPO,
            forbidden=load_forbidden(),
        )
    )
    rec = recorder(tmp_db_path)
    await rec.initialize()
    agent = Remediator(
        llm=PlanStubLlm(remediation_plan("open_fix_pr", with_calls=False)),
        model="stub", recorder=rec, gateway=gateway,
        engine=PolicyEngine(load_policy_spec()),
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=120_000)),
    )
    state = state_with(diagnosis_for("real_regression"), retries=0, unavailable=False)
    with rec.run_scope(state.run_id):
        result = await agent.run(state)

    assert result.output is not None
    plan = result.output.plan
    assert [c.tool for c in plan.tool_calls] == [
        "create_branch", "create_or_update_file", "open_pull_request",
    ]
    assert plan.tool_calls[0].args["from_sha"] == "4f1e2d3c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e"
    assert all(c.idempotency_key for c in plan.tool_calls)
    assert result.output.status == "awaiting_approval"
    assert result.output.pending_approval is not None
    assert result.output.pending_approval.plan == plan
    trace = await rec.read_trace(state.run_id)
    assert trace is not None
    plan_span = next(s for s in trace.spans if s.name == "remediation.plan")
    assert plan_span.attributes["tool_calls_derived"] is True


async def test_no_action_plan_completes_the_run(repo_root: Path, tmp_db_path: Path) -> None:
    outcome, _ = await run_scenario(
        repo_root, tmp_db_path, "real_regression", ScenarioStubLlm(plan_action="no_action")
    )
    assert outcome.status == "completed"
    remediation = RemediationResult.model_validate(outcome.final["remediation"])
    assert remediation.status == "no_action"
    assert remediation.decisions == []


# ---------------------------------------------------------------------------
# The model failing at the Remediator ends the run, and keeps the diagnosis
# ---------------------------------------------------------------------------


class RemediatorUnreachableLlm(ScenarioStubLlm):
    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        if "You are the Remediator" in req.prompt:
            raise LlmUpstreamError("provider unreachable")
        return await super().generate(req)


async def test_an_unreachable_model_at_the_remediator_escalates_and_keeps_the_diagnosis(
    repo_root: Path, tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.harness import recovery as recovery_module

    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(recovery_module.asyncio, "sleep", no_sleep)
    outcome, _ = await run_scenario(
        repo_root, tmp_db_path, "real_regression", RemediatorUnreachableLlm()
    )
    assert outcome.status == "escalated"
    assert outcome.escalation is not None and outcome.escalation.reason == "llm_upstream"
    assert "diagnosis" in outcome.final
    assert "remediation" not in outcome.final
    assert outcome.stages[-1].stage == "remediate"
    assert outcome.stages[-1].status == "escalate"


# ---------------------------------------------------------------------------
# An executed plan whose call failed escalates as tool_failure (Appendix B.2)
# ---------------------------------------------------------------------------


async def test_a_failed_execution_escalates_as_tool_failure(
    repo_root: Path, tmp_db_path: Path
) -> None:
    from src.harness.gateway import ToolError
    from src.integrations.cicd.wiring import remediation_suspend

    class BrokenGateway(SpyGateway):
        async def invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult:
            self.calls.append((call, decision))
            return ToolResult(
                call_id=call.call_id, tool=call.tool, ok=False, latency_ms=1,
                error=ToolError(kind="not_found", message="run 501234890 is gone",
                                retryable=False, http_status=404),
            )

    gateway = BrokenGateway(
        ReplayToolGateway(
            scenario_dir=scenario_dir(repo_root, "flaky_test"), repo=REPO,
            forbidden=load_forbidden(),
        )
    )
    agent = remediator(
        repo_root, tmp_db_path,
        PlanStubLlm(remediation_plan("retry_job", run_id=501234890)), gateway,
    )
    state = state_with(diagnosis_for("flaky_test"), retries=0, unavailable=False)
    result = await agent.run(state)
    assert result.output is not None
    assert result.output.status == "executed"
    assert result.output.executed[0].ok is False

    state.artifacts["remediation"] = result.output
    suspension = remediation_suspend(state)
    assert suspension is not None
    assert suspension.status == "escalated"
    assert suspension.escalate_as == "tool_failure"
    assert "rerun_failed_jobs" in suspension.reason and "not_found" in suspension.reason
    assert suspension.payload["tool"] == "rerun_failed_jobs"
