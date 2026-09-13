"""`SqliteMemoryStore`: every A.5 method, the Appendix C claim protocol, the B.3 ladder.

Nothing here touches an agent, a gateway or the model. The clock is injected so the
24 h action window and the 120 s heartbeat staleness are exercised without sleeping.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.harness.contracts import EscalationRecord, RunOutcome, TokenUsage
from src.harness.memory import (
    ACTION_WINDOW_HOURS,
    ApprovalRecord,
    MemoryQuery,
    MemoryStoreError,
    Observation,
    SignatureKey,
    SqliteMemoryStore,
    dominant_verdict,
    has_outcome,
    observation_id_for,
    signature_id_for,
)
from src.harness.orchestrator import HEARTBEAT_STALE_AFTER_S, new_run_id

KEY = SignatureKey(scope="repo:octo-org/demo", subject_key="CI|test|t", fingerprint="f" * 64)
T0 = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def store(tmp_db_path: Path, clock: Clock) -> SqliteMemoryStore:
    s = SqliteMemoryStore(tmp_db_path, clock=clock)
    await s.initialize()
    return s


def outcome(run_id: str, status: str = "completed", *, escalation: bool = False) -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        integration="cicd",
        status=status,  # type: ignore[arg-type]
        created_at=T0,
        completed_at=T0,
        duration_ms=1,
        stages=[],
        total_tokens=TokenUsage(),
        final={"x": 1},
        escalation=EscalationRecord(
            escalation_id=f"esc_{run_id[-8:]}",
            reason="policy_denied",
            message="denied",
            payload={},
            channels=["log", "db"],
            delivered_at=T0,
        ) if escalation else None,
        trace_url=f"/v1/runs/{run_id}/trace",
    )


def observation(
    run_id: str, at: datetime, *, action: str | None = None, outcome_: str | None = None
) -> Observation:
    sid = signature_id_for(KEY)
    return Observation(
        observation_id=observation_id_for(sid, run_id),
        signature_id=sid,
        run_id=run_id,
        occurred_at=at,
        verdict="flaky_test",
        confidence=0.9,
        action_taken=action,
        action_outcome=outcome_,  # type: ignore[arg-type]
        commit_sha="4f1e2d3c",
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_signature_id_is_the_plan_formula() -> None:
    import hashlib

    raw = f"{KEY.scope}|{KEY.subject_key}|{KEY.fingerprint}".encode()
    assert signature_id_for(KEY) == hashlib.sha256(raw).hexdigest()[:32]


def test_observation_id_is_deterministic_per_signature_and_run() -> None:
    a = observation_id_for("sig", "run_01J8ZZZZZZZZZZZZZZZZZZZZZZ")
    assert a == observation_id_for("sig", "run_01J8ZZZZZZZZZZZZZZZZZZZZZZ")
    assert a != observation_id_for("sig", "run_01J8ZZZZZZZZZZZZZZZZZZZZZY")
    assert a.startswith("obs_")


@pytest.mark.parametrize(
    ("occurrences", "counts", "expected"),
    [
        (0, {}, None),
        (2, {"flaky_test": 2}, None),                       # below the minimum
        (3, {"flaky_test": 3}, "flaky_test"),
        (5, {"flaky_test": 3, "real_regression": 2}, "flaky_test"),   # 0.6 exactly
        (5, {"flaky_test": 2, "real_regression": 3}, "real_regression"),
        (10, {"flaky_test": 5, "real_regression": 5}, None),  # 0.5 < 0.6
    ],
)
def test_dominant_verdict(occurrences: int, counts: dict[str, int], expected: str | None) -> None:
    assert dominant_verdict(occurrences, counts) == expected


def test_has_outcome() -> None:
    obs = [observation("run_01J8ZZZZZZZZZZZZZZZZZZZZZZ", T0, action="x", outcome_="pending")]
    assert has_outcome(obs, "pending")
    assert not has_outcome(obs, "passed_on_retry")
    assert not has_outcome([], "pending")


# ---------------------------------------------------------------------------
# Signatures and observations
# ---------------------------------------------------------------------------


async def test_first_sighting_is_empty(store: SqliteMemoryStore) -> None:
    hit = await store.lookup(MemoryQuery(key=KEY))
    assert hit.record is None
    assert hit.recent == []
    assert hit.actions_in_window == {}
    assert hit.unavailable is False


async def test_upsert_counts_occurrences_and_verdicts(
    store: SqliteMemoryStore, clock: Clock
) -> None:
    r1 = new_run_id()
    sid = await store.upsert_signature(KEY, "flaky_test", r1)
    assert sid == signature_id_for(KEY)
    clock.advance(minutes=5)
    r2 = new_run_id()
    await store.upsert_signature(KEY, "flaky_test", r2)
    await store.upsert_signature(KEY, "real_regression", r2)

    hit = await store.lookup(MemoryQuery(key=KEY))
    assert hit.record is not None
    assert hit.record.occurrences == 3
    assert hit.record.verdict_counts == {"flaky_test": 2, "real_regression": 1}
    assert hit.record.last_verdict == "real_regression"
    assert hit.record.last_run_id == r2
    assert hit.record.first_seen_at == T0
    assert hit.record.last_seen_at == T0 + timedelta(minutes=5)
    assert (hit.record.scope, hit.record.subject_key, hit.record.fingerprint) == (
        KEY.scope, KEY.subject_key, KEY.fingerprint,
    )


async def test_record_observation_upserts_on_its_deterministic_id(
    store: SqliteMemoryStore,
) -> None:
    run_id = new_run_id()
    await store.upsert_signature(KEY, "flaky_test", run_id)
    await store.record_observation(observation(run_id, T0))
    await store.record_observation(
        observation(run_id, T0, action="rerun_failed_jobs", outcome_="pending")
    )

    hit = await store.lookup(MemoryQuery(key=KEY))
    assert len(hit.recent) == 1
    assert hit.recent[0].action_taken == "rerun_failed_jobs"
    assert hit.recent[0].action_outcome == "pending"
    assert hit.actions_in_window == {"rerun_failed_jobs": 1}


async def test_observation_needs_its_signature(store: SqliteMemoryStore) -> None:
    """The foreign key is on: an observation for a signature nobody upserted is refused."""
    with pytest.raises(MemoryStoreError):
        await store.record_observation(observation(new_run_id(), T0))


async def test_update_observation_outcome(store: SqliteMemoryStore) -> None:
    run_id = new_run_id()
    await store.upsert_signature(KEY, "flaky_test", run_id)
    obs = observation(run_id, T0, action="rerun_failed_jobs", outcome_="pending")
    await store.record_observation(obs)
    await store.update_observation_outcome(obs.observation_id, "passed_on_retry")
    hit = await store.lookup(MemoryQuery(key=KEY))
    assert hit.recent[0].action_outcome == "passed_on_retry"


async def test_actions_in_window_is_the_last_24_hours(
    store: SqliteMemoryStore, clock: Clock
) -> None:
    """The retry cap reads this: an action just outside the window does not count."""
    old, mid, new = new_run_id(), new_run_id(), new_run_id()
    await store.upsert_signature(KEY, "flaky_test", old)
    await store.record_observation(
        observation(old, T0 - timedelta(hours=ACTION_WINDOW_HOURS, minutes=1),
                    action="rerun_failed_jobs", outcome_="pending")
    )
    await store.record_observation(
        observation(mid, T0 - timedelta(hours=ACTION_WINDOW_HOURS - 1),
                    action="rerun_failed_jobs", outcome_="pending")
    )
    await store.record_observation(observation(new, T0, action="create_issue", outcome_=None))

    hit = await store.lookup(MemoryQuery(key=KEY))
    assert hit.actions_in_window == {"rerun_failed_jobs": 1, "create_issue": 1}
    # Every observation is within the 30-day lookback, newest first.
    assert [o.run_id for o in hit.recent] == [new, mid, old]

    clock.advance(hours=2)
    hit = await store.lookup(MemoryQuery(key=KEY))
    assert hit.actions_in_window == {"create_issue": 1}


async def test_lookback_and_limit(store: SqliteMemoryStore) -> None:
    ids = [new_run_id() for _ in range(4)]
    await store.upsert_signature(KEY, "flaky_test", ids[0])
    await store.record_observation(observation(ids[0], T0 - timedelta(days=40)))
    for i, run_id in enumerate(ids[1:], start=1):
        await store.record_observation(observation(run_id, T0 - timedelta(days=i)))

    hit = await store.lookup(MemoryQuery(key=KEY, lookback_days=30, limit=2))
    assert [o.run_id for o in hit.recent] == [ids[1], ids[2]]


# ---------------------------------------------------------------------------
# Runs: the claim protocol (Appendix C), heartbeat, outcomes, listing
# ---------------------------------------------------------------------------


async def test_claim_is_acquired_once(store: SqliteMemoryStore) -> None:
    first = await store.claim_run("cicd:k1", "cicd")
    assert first.acquired and first.existing_status is None and first.took_over_from is None
    placeholder = await store.get_run(first.run_id)
    assert placeholder is not None and placeholder.status == "in_progress"
    assert placeholder.trace_url == f"/v1/runs/{first.run_id}/trace"

    second = await store.claim_run("cicd:k1", "cicd")
    assert not second.acquired
    assert second.run_id == first.run_id
    assert second.existing_status == "in_progress"
    assert second.existing_outcome is not None
    assert second.existing_outcome.status == "in_progress"


async def test_claim_after_completion_returns_the_outcome(store: SqliteMemoryStore) -> None:
    first = await store.claim_run("cicd:k2", "cicd")
    await store.save_run(outcome(first.run_id, "escalated", escalation=True))
    again = await store.claim_run("cicd:k2", "cicd")
    assert not again.acquired
    assert again.existing_status == "escalated"
    assert again.existing_outcome is not None
    assert again.existing_outcome.run_id == first.run_id
    assert again.existing_outcome.final == {"x": 1}


async def test_stale_heartbeat_is_taken_over(store: SqliteMemoryStore, clock: Clock) -> None:
    """Appendix C: an `in_progress` row older than 120 s is superseded; the new row carries
    `key#2`, `attempt=2`, `superseded_run_id`; the old run reads back as failed."""
    first = await store.claim_run("cicd:k3", "cicd")
    clock.advance(seconds=HEARTBEAT_STALE_AFTER_S - 1)
    await store.heartbeat(first.run_id)
    clock.advance(seconds=HEARTBEAT_STALE_AFTER_S - 1)
    still = await store.claim_run("cicd:k3", "cicd")
    assert not still.acquired, "a heartbeat inside the window keeps the claim"

    clock.advance(seconds=2)
    takeover = await store.claim_run("cicd:k3", "cicd")
    assert takeover.acquired
    assert takeover.took_over_from == first.run_id
    assert takeover.existing_status == "in_progress"

    superseded = await store.get_run(first.run_id)
    assert superseded is not None
    assert superseded.status == "failed"
    assert superseded.escalation is not None
    assert superseded.escalation.reason == "run_timeout"
    again = await store.get_run(first.run_id)
    assert again is not None and again.escalation is not None
    assert again.escalation.escalation_id == superseded.escalation.escalation_id

    # The chain continues from the newest row: a third claim sees the takeover's run.
    third = await store.claim_run("cicd:k3", "cicd")
    assert not third.acquired and third.run_id == takeover.run_id
    clock.advance(seconds=HEARTBEAT_STALE_AFTER_S + 1)
    fourth = await store.claim_run("cicd:k3", "cicd")
    assert fourth.acquired and fourth.took_over_from == takeover.run_id


async def test_heartbeat_only_touches_in_progress_rows(
    store: SqliteMemoryStore, clock: Clock
) -> None:
    claim = await store.claim_run("cicd:k4", "cicd")
    await store.save_run(outcome(claim.run_id))
    clock.advance(seconds=HEARTBEAT_STALE_AFTER_S + 5)
    await store.heartbeat(claim.run_id)  # no error, no effect
    again = await store.claim_run("cicd:k4", "cicd")
    assert not again.acquired and again.existing_status == "completed"


async def test_save_run_for_an_unclaimed_run_is_total(store: SqliteMemoryStore) -> None:
    run_id = new_run_id()
    await store.save_run(outcome(run_id))
    await store.save_run(outcome(run_id, "escalated", escalation=True))
    stored = await store.get_run(run_id)
    assert stored is not None and stored.status == "escalated"
    escalations = await store.list_escalations()
    assert [(r, e.reason) for r, e in escalations] == [(run_id, "policy_denied")]


async def test_get_run_unknown_is_none(store: SqliteMemoryStore) -> None:
    assert await store.get_run(new_run_id()) is None


async def test_list_runs_is_newest_first_with_keyset_cursor_and_status(
    store: SqliteMemoryStore,
) -> None:
    ids = []
    for status in ("completed", "escalated", "completed"):
        claim = await store.claim_run(f"cicd:list-{len(ids)}", "cicd")
        await store.save_run(outcome(claim.run_id, status, escalation=status == "escalated"))
        ids.append(claim.run_id)

    page, cursor = await store.list_runs(limit=2)
    assert [r.run_id for r in page] == [ids[2], ids[1]]
    assert cursor == ids[1]
    rest, end = await store.list_runs(limit=2, cursor=cursor)
    assert [r.run_id for r in rest] == [ids[0]]
    assert end is None

    only, _ = await store.list_runs(status="escalated")
    assert [r.run_id for r in only] == [ids[1]]


async def test_list_escalations_newest_first_with_limit(store: SqliteMemoryStore) -> None:
    ids = []
    for _ in range(3):
        run_id = new_run_id()
        await store.save_run(outcome(run_id, "escalated", escalation=True))
        ids.append(run_id)
    rows = await store.list_escalations(limit=2)
    assert [r for r, _ in rows] == [ids[2], ids[1]]


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


def approval(approval_id: str = "apr_0001") -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=approval_id,
        run_id=new_run_id(),
        state="pending",
        plan={"plan": {"action": "open_fix_pr"}, "decisions": []},
        requested_at=T0,
        expires_at=T0 + timedelta(hours=24),
        context={"mode": "replay", "repo": "octo-org/demo", "scenario_dir": "x"},
    )


async def test_approval_round_trip_and_single_use(store: SqliteMemoryStore, clock: Clock) -> None:
    record = approval()
    await store.save_approval(record)
    assert await store.get_approval("apr_0001") == record
    assert await store.get_approval("apr_nope") is None

    clock.advance(minutes=1)
    decided, applied = await store.decide_approval("apr_0001", "approved", actor="me", note="ok")
    assert applied and decided.state == "approved"
    assert decided.decided_by == "me" and decided.decision_note == "ok"
    assert decided.decided_at == T0 + timedelta(minutes=1)
    assert decided.context == record.context

    again, applied_again = await store.decide_approval("apr_0001", "rejected", actor="you")
    assert not applied_again
    assert again.state == "approved" and again.decided_by == "me"


async def test_decide_unknown_approval_raises_key_error(store: SqliteMemoryStore) -> None:
    with pytest.raises(KeyError):
        await store.decide_approval("apr_missing", "expired")


async def test_concurrent_decisions_apply_exactly_once(store: SqliteMemoryStore) -> None:
    await store.save_approval(approval("apr_race"))
    results = await asyncio.gather(
        *(store.decide_approval("apr_race", "approved", actor=f"a{i}") for i in range(5))
    )
    assert sum(1 for _, applied in results if applied) == 1


# ---------------------------------------------------------------------------
# Appendix B.3: the ladder, the fault, the error type
# ---------------------------------------------------------------------------


async def test_fault_injection_exhausts_the_ladder_then_raises(tmp_db_path: Path) -> None:
    faulty = SqliteMemoryStore(
        tmp_db_path, fault_inject="sqlite_locked", retry_backoff_ms=(1, 2, 3)
    )
    await faulty.initialize()  # migrations are exempt: the process must boot
    with pytest.raises(MemoryStoreError, match="database is locked"):
        await faulty.lookup(MemoryQuery(key=KEY))
    with pytest.raises(MemoryStoreError):
        await faulty.claim_run("cicd:x", "cicd")


async def test_a_transient_lock_is_retried_and_succeeds(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two `database is locked` failures, then success: the ladder absorbs them."""
    store = SqliteMemoryStore(tmp_db_path, retry_backoff_ms=(1, 2, 3))
    await store.initialize()
    real_connect = store._connect
    failures = {"left": 2}

    def flaky_connect() -> object:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real_connect()

    monkeypatch.setattr(store, "_connect", flaky_connect)
    hit = await store.lookup(MemoryQuery(key=KEY))
    assert hit.record is None
    assert failures["left"] == 0


async def test_non_busy_errors_are_not_retried(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqliteMemoryStore(tmp_db_path, retry_backoff_ms=(1, 2, 3))
    await store.initialize()
    calls = {"n": 0}

    def broken_connect() -> object:
        calls["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_connect", broken_connect)
    with pytest.raises(MemoryStoreError, match="disk I/O error"):
        await store.lookup(MemoryQuery(key=KEY))
    assert calls["n"] == 1


def test_run_id_factory_is_injectable(tmp_db_path: Path) -> None:
    fixed: Callable[[], str] = lambda: "run_01J8ZZZZZZZZZZZZZZZZZZZZZZ"  # noqa: E731
    store = SqliteMemoryStore(tmp_db_path, run_id_factory=fixed)
    assert store._run_id_factory() == "run_01J8ZZZZZZZZZZZZZZZZZZZZZZ"
