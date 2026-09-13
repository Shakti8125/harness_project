"""PLAN.md Phase 3 Verify steps 2-4 over HTTP, plus the seams the phase wired.

Same shape as `test_replay_e2e.py`: the model is stubbed (`ScenarioStubLlm`), everything
else is real -- the routes, the claim protocol, the orchestrator and its heartbeat, the
replay gateway over the fixtures, the fingerprint, and a `SqliteMemoryStore` on the
per-test temp file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.deps import (
    FIXTURES_ROOT,
    SECRET_PATTERNS,
    AppContext,
    RunContext,
    build_secret_registry,
)
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import RunRequest
from src.harness.guardrails import Condition, PolicyEngine
from src.harness.memory import (
    MemoryQuery,
    MemoryStore,
    Observation,
    SignatureKey,
    SqliteMemoryStore,
    observation_id_for,
)
from src.harness.observability import Redactor, TraceRecorder
from src.harness.orchestrator import Orchestrator, StageSpec
from src.integrations.cicd.history import RETRY_TOOL
from src.integrations.cicd.wiring import load_policy_spec
from src.settings import get_settings
from tests.stubs import ScenarioStubLlm


def make_context(
    tmp_db_path: Path,
    *,
    memory: MemoryStore | None = None,
    engine: PolicyEngine | None = None,
    llm: Any | None = None,
    run_semaphore: asyncio.Semaphore | None = None,
    heartbeat_interval_s: float | None = None,
) -> AppContext:
    settings = get_settings()
    kwargs: dict[str, Any] = {}
    if memory is not None:
        kwargs["memory"] = memory
    if engine is not None:
        kwargs["engine"] = engine
    if heartbeat_interval_s is not None:
        kwargs["heartbeat_interval_s"] = heartbeat_interval_s
    return AppContext(
        settings=settings,
        recorder=TraceRecorder(
            db_path=tmp_db_path,
            redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
        ),
        context_manager=ContextManager(
            default_budget=ContextBudget(total_chars=settings.log_char_budget)
        ),
        llm=llm or ScenarioStubLlm(),
        run_semaphore=run_semaphore or asyncio.Semaphore(settings.max_concurrent_runs),
        **kwargs,
    )


@pytest.fixture
def context(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppContext:
    ctx = make_context(tmp_db_path)
    monkeypatch.setattr(api_main, "get_app_context", lambda: ctx)
    return ctx


@pytest.fixture
def client(context: AppContext) -> Iterator[TestClient]:
    with TestClient(api_main.app) as test_client:
        yield test_client


def replay(client: TestClient, scenario: str = "flaky_test", **params: Any) -> dict[str, Any]:
    response = client.post(f"/v1/replay/{scenario}", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def effect(body: dict[str, Any]) -> str:
    return str(body["final"]["remediation"]["decisions"][0]["effect"])


# ---------------------------------------------------------------------------
# Verify step 2: repeat flakiness is recognised across runs
# ---------------------------------------------------------------------------


def test_the_fourth_flaky_run_knows_it_has_seen_this_before(
    client: TestClient, tmp_db_path: Path
) -> None:
    """Three fresh replays, then the fourth reads `occurrences=3`, `likely_flaky`, +0.10."""
    for _ in range(3):
        replay(client, fresh=1)
    fourth = replay(client, fresh=1)

    prior = fourth["final"]["bundle"]["prior_history"]
    assert prior["occurrences"] == 3
    assert prior["prior_hint"] == "likely_flaky"
    assert prior["verdict_counts"] == {"flaky_test": 3}
    assert prior["unavailable"] is False
    assert prior["signature_id"] and prior["key"]["scope"] == "repo:octo-org/harness-demo-repo"
    assert len(prior["sample_run_ids"]) == 3
    adjustments = [
        a["delta"]
        for a in fourth["final"]["diagnosis"]["confidence_adjustments"]
        if a["name"] == "memory_agreement"
    ]
    assert adjustments == [0.10]
    assert fourth["degraded_components"] == []

    # The sqlite3 line of the Verify block, verbatim.
    with sqlite3.connect(tmp_db_path) as db:
        rows = db.execute(
            "select occurrences, last_verdict, verdict_counts from failure_signature"
        ).fetchall()
    assert rows == [(4, "flaky_test", '{"flaky_test":4}')]

    # The prompts carried the prior as a prior, not a verdict.
    stub = api_main.get_app_context().llm
    diagnostician_prompts = [p for p in stub.prompts if "You are the Diagnostician" in p]
    assert "seen 3 time(s) before" in diagnostician_prompts[-1]
    assert "Deterministic prior from those counts: likely_flaky" in diagnostician_prompts[-1]
    assert "prior, not evidence" in diagnostician_prompts[-1]


def test_a_pending_retry_is_resolved_from_the_rerun_and_infra_stays_pending(
    client: TestClient, context: AppContext,
) -> None:
    """Dispatch decision 10: the second sighting probes attempt 2 of the retried run."""
    first = replay(client)
    assert first["final"]["remediation"]["status"] == "executed"
    key = SignatureKey.model_validate(first["final"]["bundle"]["prior_history"]["key"])

    async def recent() -> list[Observation]:
        return (await context.store.lookup(MemoryQuery(key=key))).recent

    before = asyncio.run(recent())
    assert [(o.action_taken, o.action_outcome) for o in before] == [
        ("rerun_failed_jobs", "pending")
    ]

    second = replay(client)
    after = asyncio.run(recent())
    outcomes = {o.run_id: o.action_outcome for o in after}
    assert outcomes[first["run_id"]] == "passed_on_retry"
    assert outcomes[second["run_id"]] == "pending"
    assert second["final"]["bundle"]["prior_history"]["retries_in_24h"] == 1
    # The probe is a read tool call on the trace, and not a degradation.
    assert second["degraded_components"] == []

    # infra_timeout has no attempt-2 recording: its retry stays pending, no degradation.
    infra_first = replay(client, "infra_timeout")
    infra_second = replay(client, "infra_timeout")
    infra_key = SignatureKey.model_validate(
        infra_second["final"]["bundle"]["prior_history"]["key"]
    )
    infra_recent = asyncio.run(
        (lambda: context.store.lookup(MemoryQuery(key=infra_key)))()
    ).recent
    assert {o.action_outcome for o in infra_recent} == {"pending"}
    assert infra_first["run_id"] in {o.run_id for o in infra_recent}
    assert infra_second["degraded_components"] == []
    assert infra_second["final"]["bundle"]["prior_history"]["prior_hint"] == "unknown"


# ---------------------------------------------------------------------------
# Verify step 3: the retry cap actually bites
# ---------------------------------------------------------------------------


def test_the_retry_cap_bites_on_the_third_run(client: TestClient) -> None:
    """From a fresh database: allow, allow, deny, deny -- and the deny names the count."""
    bodies = [replay(client) for _ in range(4)]
    assert [effect(b) for b in bodies] == ["allow", "allow", "deny", "deny"]
    assert [b["status"] for b in bodies] == ["completed", "completed", "escalated", "escalated"]

    third = bodies[2]
    decision = third["final"]["remediation"]["decisions"][0]
    assert decision["rule_id"] == "<default>"
    assert "retry-suspected-flaky" in decision["reason"]
    assert "memory.retries_for_signature_24h: 2" in decision["reason"]
    assert third["escalation"]["reason"] == "policy_denied"
    assert third["final"]["bundle"]["prior_history"]["retries_in_24h"] == 2
    assert bodies[3]["final"]["bundle"]["prior_history"]["retries_in_24h"] == 2

    for body in bodies[:2]:
        assert body["final"]["remediation"]["status"] == "executed"
        executed = body["final"]["remediation"]["executed"]
        assert [r["tool"] for r in executed] == ["rerun_failed_jobs"]
        assert executed[0]["dry_run"] is True


def test_escalations_are_durable_on_the_db_channel(client: TestClient) -> None:
    for _ in range(3):
        last = replay(client)
    assert last["status"] == "escalated"
    assert "db" in last["escalation"]["channels"]
    items = client.get("/v1/escalations").json()
    assert items[0]["run_id"] == last["run_id"]
    assert items[0]["reason"] == "policy_denied"


# ---------------------------------------------------------------------------
# Verify step 4 (as amended): a memory outage degrades, the cap fails closed
# ---------------------------------------------------------------------------


def test_memory_outage_degrades_and_the_cap_fails_closed(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`HARNESS_FAULT_INJECT=sqlite_locked`: every store call fails after the B.3 ladder.

    The diagnosis is produced, `degraded_components == ["memory"]`, the prior is
    `unavailable` with the fail-closed 999, and the retry is *denied* -- so the run
    escalates `policy_denied` rather than completing. That is the honest reading of
    "degrades, does not fail" under Phase 2's deny-escalates decision (dispatch 11).
    """
    faulty = SqliteMemoryStore(
        tmp_db_path, fault_inject="sqlite_locked", retry_backoff_ms=(1, 2, 3)
    )
    ctx = make_context(tmp_db_path, memory=faulty)
    monkeypatch.setattr(api_main, "get_app_context", lambda: ctx)
    with TestClient(api_main.app) as client:
        body = replay(client)
        trace = client.get(f"/v1/runs/{body['run_id']}/trace").json()
        run_lookup = client.get(f"/v1/runs/{body['run_id']}")

    assert body["status"] == "escalated"
    assert body["degraded_components"] == ["memory"]
    assert body["escalation"]["reason"] == "policy_denied"
    assert "memory.retries_for_signature_24h: 999" in body["escalation"]["message"]
    prior = body["final"]["bundle"]["prior_history"]
    assert prior["unavailable"] is True
    assert prior["retries_in_24h"] == 999
    assert prior["key"] is not None, "the key is pure and is computed even when the store is down"
    assert body["final"]["diagnosis"]["category"] == "flaky_test"
    assert not [
        a for a in body["final"]["diagnosis"]["confidence_adjustments"]
        if a["name"] == "memory_agreement"
    ]
    assert "memory" in trace["degraded_components"]
    # The run could not be recorded, so the store answers 503, not 500 and not a lie.
    assert run_lookup.status_code == 503
    assert run_lookup.headers["content-type"] == "application/problem+json"
    assert run_lookup.headers["retry-after"] == "5"


def test_a_write_failure_after_diagnosis_degrades_but_keeps_the_diagnosis(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ReadOnlyStore(SqliteMemoryStore):
        async def upsert_signature(
            self, key: SignatureKey, verdict: str | None, run_id: str
        ) -> str:
            raise RuntimeError("write side is broken")

    store = ReadOnlyStore(tmp_db_path)
    ctx = make_context(tmp_db_path, memory=store)
    monkeypatch.setattr(api_main, "get_app_context", lambda: ctx)
    with TestClient(api_main.app) as client:
        body = replay(client)
    assert body["final"]["diagnosis"]["category"] == "flaky_test"
    assert body["degraded_components"] == ["memory"]
    # The read side worked: a first sighting, readable, so the retry was allowed.
    assert body["final"]["bundle"]["prior_history"]["unavailable"] is False
    assert effect(body) == "allow"


# ---------------------------------------------------------------------------
# Appendix D: cold start
# ---------------------------------------------------------------------------


def test_cold_start_replay(client: TestClient) -> None:
    """The Appendix D check: kind none, cold true, not real_regression, retry denied."""
    body = replay(client, "cold_start")
    bundle = body["final"]["bundle"]
    assert bundle["diff"]["baseline_kind"] == "none"
    assert bundle["cold_start"] is True
    assert bundle["diff"]["files"] == []
    assert bundle["gateway_errors"] == []
    assert body["final"]["diagnosis"]["category"] != "real_regression"
    assert effect(body) == "deny"
    decision = body["final"]["remediation"]["decisions"][0]
    assert "context.cold_start: True" in decision["reason"]
    assert "retry-suspected-flaky" in decision["reason"]
    assert body["status"] == "escalated"
    fired = {a["name"] for a in body["final"]["diagnosis"]["confidence_adjustments"]}
    assert "cold_start" in fired


# ---------------------------------------------------------------------------
# Appendix C: the claim protocol over HTTP
# ---------------------------------------------------------------------------


def test_replay_with_fresh_false_dedupes_a_redelivery(client: TestClient) -> None:
    first = replay(client, fresh=0)
    assert first["status"] == "completed"
    stub = api_main.get_app_context().llm
    calls_before = len(stub.prompts)

    second = replay(client, fresh=0)
    assert second["status"] == "deduplicated"
    assert second["original_run_id"] == first["run_id"]
    assert second["run_id"] == first["run_id"]
    assert second["final"] == first["final"]
    assert len(stub.prompts) == calls_before, "a deduplicated redelivery spends no model call"

    # `fresh=true` (the default) is a genuinely new run under a nonce-suffixed key.
    third = replay(client)
    assert third["status"] == "completed" and third["run_id"] != first["run_id"]


def test_create_run_dedupes_by_idempotency_key(client: TestClient, repo_root: Path) -> None:
    webhook = json.loads(
        (repo_root / "fixtures/scenarios/real_regression/webhook.json").read_text("utf-8")
    )
    request = {
        "integration": "cicd",
        "subject": webhook,
        "idempotency_key": "cicd:dedupe-me",
        "mode": "replay",
        "replay_fixture": "real_regression",
        "requested_by": "test",
    }
    accepted = client.post("/v1/runs", json=request)
    assert accepted.status_code == 202
    run_id = accepted.json()["run_id"]

    # While in progress with a fresh heartbeat: 202 naming the same run.
    again = client.post("/v1/runs", json=request)
    assert again.status_code in (200, 202)
    assert again.json()["run_id"] == run_id

    for _ in range(400):
        outcome = client.get(f"/v1/runs/{run_id}").json()
        if outcome["status"] != "in_progress":
            break
        asyncio.run(asyncio.sleep(0.01))
    assert outcome["status"] == "awaiting_approval"

    # Finished with a pending approval: 200, the same outcome and approval id.
    redelivered = client.post("/v1/runs", json=request)
    assert redelivered.status_code == 200
    body = redelivered.json()
    assert body["run_id"] == run_id
    assert body["status"] == "awaiting_approval"
    assert (
        body["final"]["remediation"]["pending_approval"]["approval_id"]
        == outcome["final"]["remediation"]["pending_approval"]["approval_id"]
    )


def test_list_runs_pages_with_a_cursor(client: TestClient) -> None:
    ids = [replay(client)["run_id"] for _ in range(3)]
    page = client.get("/v1/runs", params={"limit": 2}).json()
    assert [item["run_id"] for item in page["items"]] == [ids[2], ids[1]]
    assert page["next_cursor"] == ids[1]
    rest = client.get("/v1/runs", params={"limit": 2, "cursor": page["next_cursor"]}).json()
    assert [item["run_id"] for item in rest["items"]] == [ids[0]]
    assert rest["next_cursor"] is None


def test_readyz_reports_the_migrations(client: TestClient) -> None:
    body = client.get("/readyz").json()
    assert body["migrations_applied"] is True


# ---------------------------------------------------------------------------
# Approvals re-query memory (handoff §4); the policy_denied-on-approve branch is pinned
# ---------------------------------------------------------------------------


def _engine_gating_pr_on_retries() -> PolicyEngine:
    """The real policy, with `open-fix-pr` additionally requiring no retry in 24 h --
    a movable memory fact on a `require_approval` rule, which the shipped policy has
    none of, so the branch review finding 3 left unpinned becomes reachable."""
    spec = load_policy_spec()
    rules = [
        rule.model_copy(
            update={"when": {**rule.when, "memory.retries_for_signature_24h": Condition(lt=1)}}
        )
        if rule.id == "open-fix-pr" else rule
        for rule in spec.rules
    ]
    return PolicyEngine(spec.model_copy(update={"rules": rules}))


def test_approval_re_evaluates_against_fresh_memory_and_can_deny(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_context(tmp_db_path, engine=_engine_gating_pr_on_retries())
    monkeypatch.setattr(api_main, "get_app_context", lambda: ctx)
    with TestClient(api_main.app) as client:
        body = replay(client, "real_regression")
        assert body["status"] == "awaiting_approval"
        approval_id = body["final"]["remediation"]["pending_approval"]["approval_id"]
        prior = body["final"]["bundle"]["prior_history"]
        key = SignatureKey.model_validate(prior["key"])
        assert prior["retries_in_24h"] == 0

        # Memory moves between suspension and approval: a retry of this signature lands.
        async def record_a_retry() -> None:
            sid = await ctx.store.upsert_signature(key, "real_regression", body["run_id"])
            await ctx.store.record_observation(
                Observation(
                    observation_id=observation_id_for(sid, "run_01J8ZZZZZZZZZZZZZZZZZZZZZZ"),
                    signature_id=sid,
                    run_id="run_01J8ZZZZZZZZZZZZZZZZZZZZZZ",
                    occurred_at=datetime.now(UTC),
                    verdict="real_regression",
                    confidence=0.9,
                    action_taken="rerun_failed_jobs",
                    action_outcome="pending",
                    commit_sha=None,
                )
            )

        asyncio.run(record_a_retry())

        decided = client.post(
            f"/v1/approvals/{approval_id}", json={"decision": "approve", "actor": "reviewer"}
        )
        assert decided.status_code == 200
        payload = decided.json()
        assert payload["state"] == "approved"
        assert payload["executed"] == []
        assert {d["effect"] for d in payload["decisions"]} == {"deny"}
        assert "memory.retries_for_signature_24h: 1" in payload["decisions"][0]["reason"]

        settled = client.get(f"/v1/runs/{body['run_id']}").json()
        assert settled["status"] == "escalated"
        assert settled["escalation"]["reason"] == "policy_denied"
        assert settled["escalation"]["payload"]["stage"] == "approval"
        assert settled["final"]["remediation"]["status"] == "denied"

        # Single-use, still: a second decision is a 409 with the recorded state.
        again = client.post(
            f"/v1/approvals/{approval_id}", json={"decision": "reject", "actor": "x"}
        )
        assert again.status_code == 409 and again.json()["state"] == "approved"


def test_approvals_survive_a_new_process(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the `approval` table: a second context over the same file decides it."""
    first = make_context(tmp_db_path)
    monkeypatch.setattr(api_main, "get_app_context", lambda: first)
    with TestClient(api_main.app) as client:
        body = replay(client, "real_regression")
        approval_id = body["final"]["remediation"]["pending_approval"]["approval_id"]

    second = make_context(tmp_db_path)
    monkeypatch.setattr(api_main, "get_app_context", lambda: second)
    with TestClient(api_main.app) as client:
        assert client.get(f"/v1/runs/{body['run_id']}").json()["status"] == "awaiting_approval"
        decided = client.post(
            f"/v1/approvals/{approval_id}", json={"decision": "reject", "actor": "x"}
        )
        assert decided.status_code == 200 and decided.json()["state"] == "rejected"
        assert client.get(f"/v1/runs/{body['run_id']}").json()["status"] == "completed"


# ---------------------------------------------------------------------------
# The orchestrator's heartbeat
# ---------------------------------------------------------------------------


class HeartbeatSpy:
    def __init__(self) -> None:
        self.beats: list[str] = []

    async def heartbeat(self, run_id: str) -> None:
        self.beats.append(run_id)


async def test_the_orchestrator_heartbeats_while_a_run_is_active(tmp_db_path: Path) -> None:
    from pydantic import BaseModel

    from src.harness.agent import Agent
    from src.harness.contracts import AgentResult, TokenUsage

    class Out(BaseModel):
        ok: bool = True

    class SlowAgent:
        key = "slow"

        async def run(self, state: Any) -> AgentResult[Out]:
            await asyncio.sleep(0.05)
            return AgentResult[Out](
                agent="slow", status="ok", output=Out(), latency_ms=50, tokens=TokenUsage()
            )

    spy = HeartbeatSpy()
    agents: dict[str, Agent[Any]] = {"slow": SlowAgent()}  # type: ignore[dict-item]
    orchestrator = Orchestrator(
        stages=[StageSpec(name="slow", agent_key="slow", output_model=Out)],
        agents=agents,
        recorder=TraceRecorder(
            db_path=tmp_db_path, redactor=Redactor(build_secret_registry(get_settings()), [])
        ),
        memory=spy,  # type: ignore[arg-type]  # only `heartbeat` is used
        heartbeat_interval_s=0.01,
    )
    outcome = await orchestrator.run(
        RunRequest(integration="cicd", subject={}, idempotency_key="cicd:heartbeat-test")
    )
    assert outcome.status == "completed"
    assert spy.beats and set(spy.beats) == {outcome.run_id}
    beats_after = len(spy.beats)
    await asyncio.sleep(0.05)
    assert len(spy.beats) == beats_after, "the heartbeat task is cancelled with the run"


async def test_a_queued_run_keeps_its_claim_alive(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    """Audit finding 2: the claim is written in the route, the orchestrator heartbeats only
    once `run()` starts, and between the two sits the wait for a concurrency slot. A burst
    that queues a run past the staleness window must not hand its claim to the next
    redelivery -- the queued original would still run, and the failure would be triaged
    twice. Here every slot is taken, the staleness window is 150 ms, and the redelivery
    arrives 400 ms later."""
    store = SqliteMemoryStore(tmp_db_path, stale_after_s=0.15)
    await store.initialize()
    semaphore = asyncio.Semaphore(1)
    context = make_context(
        tmp_db_path, memory=store, run_semaphore=semaphore, heartbeat_interval_s=0.02
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    webhook = json.loads(
        (repo_root / "fixtures/scenarios/flaky_test/webhook.json").read_text("utf-8")
    )
    request = RunRequest(
        integration="cicd", subject=webhook, idempotency_key="cicd:queued",
        mode="replay", replay_fixture="flaky_test", requested_by="test",
    )
    run_context = RunContext(
        mode="replay", repo="octo-org/harness-demo-repo",
        scenario_dir=FIXTURES_ROOT / "flaky_test",
    )

    await semaphore.acquire()  # someone else holds the only slot
    claim = await store.claim_run("cicd:queued", "cicd")
    assert claim.acquired
    queued = asyncio.create_task(
        api_main._execute(context, request, run_context, claim.run_id)
    )
    try:
        await asyncio.sleep(0.4)
        redelivery = await store.claim_run("cicd:queued", "cicd")
        assert not redelivery.acquired, "the queued run's claim went stale while it waited"
        assert redelivery.run_id == claim.run_id
        assert redelivery.existing_status == "in_progress"
        assert redelivery.took_over_from is None
    finally:
        queued.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await queued
        semaphore.release()


# ---------------------------------------------------------------------------
# Audit finding 3: a rerun that failed again is read by the next sighting
# ---------------------------------------------------------------------------


def test_a_failed_rerun_withholds_the_flaky_prior_and_the_bonus(
    client: TestClient, context: AppContext
) -> None:
    """Two sightings execute their retries; then the newest retry the harness knows of
    failed again. The third sighting must read that -- no `likely_flaky`, no +0.10, and the
    prompt says the rerun failed -- although the counts still say flaky three times."""
    first = replay(client)
    second = replay(client)
    assert first["final"]["remediation"]["status"] == "executed"
    assert second["final"]["remediation"]["status"] == "executed"
    key = SignatureKey.model_validate(second["final"]["bundle"]["prior_history"]["key"])

    seeded_run = "run_01J8FA1EDAGA1N000000000000"

    async def a_rerun_failed_again() -> None:
        sid = await context.store.upsert_signature(key, "flaky_test", seeded_run)
        await context.store.record_observation(
            Observation(
                observation_id=observation_id_for(sid, seeded_run),
                signature_id=sid,
                run_id=seeded_run,
                occurred_at=datetime.now(UTC),
                verdict="flaky_test",
                confidence=0.95,
                action_taken=RETRY_TOOL,
                action_outcome="failed_again",
                commit_sha=None,
            )
        )

    asyncio.run(a_rerun_failed_again())

    third = replay(client)
    prior = third["final"]["bundle"]["prior_history"]
    assert prior["occurrences"] == 3 and prior["verdict_counts"] == {"flaky_test": 3}
    assert prior["last_retry_outcome"] == "failed_again"
    assert prior["prior_hint"] == "unknown"
    assert not [
        a for a in third["final"]["diagnosis"]["confidence_adjustments"]
        if a["name"] == "memory_agreement"
    ], "the counts agree with the verdict, the last rerun does not"
    stub = api_main.get_app_context().llm
    diagnostician_prompts = [p for p in stub.prompts if "You are the Diagnostician" in p]
    assert "most recent automatic retry of it FAILED AGAIN" in diagnostician_prompts[-1]

    # The older, resolved passes are still on record; they just no longer outrank the failure.
    async def outcomes() -> set[str | None]:
        hit = await context.store.lookup(MemoryQuery(key=key))
        return {o.action_outcome for o in hit.recent}

    assert "passed_on_retry" in asyncio.run(outcomes())


# ---------------------------------------------------------------------------
# Audit finding 4: verdicts the gate refused do not build the prior
# ---------------------------------------------------------------------------


def test_three_gated_verdicts_do_not_lift_a_fourth_over_the_gate(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three sightings at a confidence the harness declined to act on, then a fourth just
    under the gate: without the fix the three refused verdicts made a `flaky_test` prior,
    the bonus lifted the fourth to 0.75, and a retry executed on the strength of verdicts
    the harness itself would not act on."""
    stub = ScenarioStubLlm(self_confidence=0.45)
    ctx = make_context(tmp_db_path, llm=stub)
    monkeypatch.setattr(api_main, "get_app_context", lambda: ctx)
    with TestClient(api_main.app) as client:
        for _ in range(3):
            gated = replay(client)
            assert gated["status"] == "escalated"
            assert gated["escalation"]["reason"] == "low_confidence"
            assert gated["final"]["diagnosis"]["final_confidence"] < 0.70

        with sqlite3.connect(tmp_db_path) as db:
            rows = db.execute(
                "select occurrences, last_verdict, verdict_counts from failure_signature"
            ).fetchall()
        assert rows == [(3, None, "{}")], "sightings counted, verdicts not vouched for"

        stub.self_confidence = 0.65  # +0.10 would clear the gate and the rule's 0.75
        fourth = replay(client)
    diagnosis = fourth["final"]["diagnosis"]
    assert 0.60 <= diagnosis["final_confidence"] < 0.70, diagnosis["final_confidence"]
    assert not [
        a for a in diagnosis["confidence_adjustments"] if a["name"] == "memory_agreement"
    ]
    assert fourth["final"]["bundle"]["prior_history"]["prior_hint"] == "unknown"
    assert fourth["status"] == "escalated"
    assert fourth["escalation"]["reason"] == "low_confidence"
    # Every sighting is on record with its confidence, gated or not.
    with sqlite3.connect(tmp_db_path) as db:
        confidences = [r[0] for r in db.execute("select confidence from observation").fetchall()]
    assert len(confidences) == 4 and all(c < 0.70 for c in confidences)


# ---------------------------------------------------------------------------
# Audit finding 7: an approved plan's write is recorded like an automatic one
# ---------------------------------------------------------------------------


def _engine_requiring_approval_for_retries() -> PolicyEngine:
    """The shipped policy with `retry-suspected-flaky` flipped to `require_approval` -- the
    reviewer's scenario for finding 7, and a flip an operator could make in `policy.yaml`."""
    spec = load_policy_spec()
    rules = [
        rule.model_copy(update={"effect": "require_approval"})
        if rule.id == "retry-suspected-flaky" else rule
        for rule in spec.rules
    ]
    return PolicyEngine(spec.model_copy(update={"rules": rules}))


def test_an_approved_retry_is_recorded_and_counted_by_the_cap(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With retries behind approval, the write executes in the approval route, not the
    Remediator; dispatch decision 9's "every executed write is recorded" must hold there
    too, or the flip switches the 2/24 h cap off: every approved retry would execute and
    `actions_in_window` would stay 0."""
    ctx = make_context(tmp_db_path, engine=_engine_requiring_approval_for_retries())
    monkeypatch.setattr(api_main, "get_app_context", lambda: ctx)
    with TestClient(api_main.app) as client:
        first = replay(client)
        assert first["status"] == "awaiting_approval"
        key = SignatureKey.model_validate(first["final"]["bundle"]["prior_history"]["key"])

        async def hit() -> Any:
            return await ctx.store.lookup(MemoryQuery(key=key))

        before = asyncio.run(hit())
        assert [(o.run_id, o.action_taken) for o in before.recent] == [(first["run_id"], None)]
        assert before.actions_in_window == {}

        def approve(body: dict[str, Any]) -> dict[str, Any]:
            approval_id = body["final"]["remediation"]["pending_approval"]["approval_id"]
            decided = client.post(
                f"/v1/approvals/{approval_id}", json={"decision": "approve", "actor": "reviewer"}
            )
            assert decided.status_code == 200, decided.text
            payload: dict[str, Any] = decided.json()
            return payload

        approved = approve(first)
        assert [r["tool"] for r in approved["executed"]] == ["rerun_failed_jobs"]
        after = asyncio.run(hit())
        assert [(o.run_id, o.action_taken, o.action_outcome) for o in after.recent] == [
            (first["run_id"], "rerun_failed_jobs", "pending")
        ]
        assert after.actions_in_window == {"rerun_failed_jobs": 1}

        # A second approved retry is the window's second; the third sighting is capped.
        second = replay(client)
        assert second["status"] == "awaiting_approval"
        assert second["final"]["bundle"]["prior_history"]["retries_in_24h"] == 1
        approve(second)
        third = replay(client)
        assert third["final"]["bundle"]["prior_history"]["retries_in_24h"] == 2
        assert third["status"] == "escalated"
        assert third["escalation"]["reason"] == "policy_denied"
        assert "memory.retries_for_signature_24h: 2" in third["escalation"]["message"]
