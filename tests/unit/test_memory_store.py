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
    is_chain_member,
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


async def test_upsert_without_a_verdict_counts_the_sighting_only(
    store: SqliteMemoryStore,
) -> None:
    """Audit finding 4: a sighting whose verdict the caller does not vouch for (below the
    gate) raises `occurrences` and moves `last_seen_at`/`last_run_id`, and leaves
    `verdict_counts` and `last_verdict` alone -- so three such sightings dilute the
    dominant share rather than build it."""
    for run_id in ("run_" + "A" * 26, "run_" + "B" * 26, "run_" + "C" * 26):
        await store.upsert_signature(KEY, None, run_id)
    hit = await store.lookup(MemoryQuery(key=KEY))
    assert hit.record is not None
    assert hit.record.occurrences == 3
    assert hit.record.verdict_counts == {}
    assert hit.record.last_verdict is None
    assert hit.record.last_run_id == "run_" + "C" * 26
    assert dominant_verdict(hit.record.occurrences, hit.record.verdict_counts) is None

    # One vouched-for verdict among four sightings is a 25% share: still no prior.
    await store.upsert_signature(KEY, "flaky_test", "run_" + "D" * 26)
    hit = await store.lookup(MemoryQuery(key=KEY))
    assert hit.record is not None
    assert (hit.record.occurrences, hit.record.verdict_counts) == (4, {"flaky_test": 1})
    assert hit.record.last_verdict == "flaky_test"
    assert dominant_verdict(hit.record.occurrences, hit.record.verdict_counts) is None
    # A later unvouched sighting keeps the last vouched-for verdict.
    await store.upsert_signature(KEY, None, "run_" + "E" * 26)
    hit = await store.lookup(MemoryQuery(key=KEY))
    assert hit.record is not None and hit.record.last_verdict == "flaky_test"


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


def test_chain_membership_is_the_bare_key_or_a_numbered_takeover() -> None:
    """Audit finding 1: `<key>#fresh:<nonce>` is not in `<key>`'s chain; `<key>#2` is."""
    assert is_chain_member("cicd:K", "cicd:K")
    assert is_chain_member("cicd:K", "cicd:K#2")
    assert is_chain_member("cicd:K", "cicd:K#10")
    assert not is_chain_member("cicd:K", "cicd:K#fresh:1a2b3c4d")
    assert not is_chain_member("cicd:K", "cicd:K#fresh:1a2b3c4d#2")
    assert not is_chain_member("cicd:K", "cicd:K2")
    assert not is_chain_member("cicd:K", "cicd:K#")
    assert not is_chain_member("cicd:K", "cicd:K#2x")
    # A `_` in the key is a LIKE wildcard; membership is exact regardless.
    assert not is_chain_member("cicd:a_b", "cicd:aXb#2")


async def test_a_fresh_replay_row_never_answers_for_the_real_key(
    store: SqliteMemoryStore,
) -> None:
    """Audit finding 1 (a): a completed `fresh=true` replay, then the real webhook, then a
    redelivery -- the redelivery must see the real run `in_progress`, not the replay's
    finished outcome as `deduplicated`."""
    replay = await store.claim_run("cicd:K#fresh:0badcafe", "cicd")
    assert replay.acquired
    await store.save_run(outcome(replay.run_id))

    real = await store.claim_run("cicd:K", "cicd")
    assert real.acquired and real.took_over_from is None

    redelivery = await store.claim_run("cicd:K", "cicd")
    assert not redelivery.acquired
    assert redelivery.run_id == real.run_id, "the replay's row is not the real key's chain"
    assert redelivery.existing_status == "in_progress"
    assert redelivery.took_over_from is None


async def test_a_stale_fresh_replay_row_is_not_taken_over_by_the_real_key(
    store: SqliteMemoryStore, clock: Clock
) -> None:
    """Audit finding 1 (b): a replay left `in_progress` (process killed) goes stale; the
    real key's redelivery must hold on its own fresh row rather than take the replay over
    and start a second concurrent run for one idempotency key."""
    dead_replay = await store.claim_run("cicd:K#fresh:deadbeef", "cicd")
    clock.advance(seconds=HEARTBEAT_STALE_AFTER_S + 5)

    real = await store.claim_run("cicd:K", "cicd")
    assert real.acquired
    redelivery = await store.claim_run("cicd:K", "cicd")
    assert not redelivery.acquired
    assert redelivery.run_id == real.run_id
    assert redelivery.took_over_from is None
    # The dead replay row is untouched: still its own key, still in progress.
    stale = await store.get_run(dead_replay.run_id)
    assert stale is not None and stale.status == "in_progress"

    # And the replay key's own chain still works: it is stale, so it is taken over.
    takeover = await store.claim_run("cicd:K#fresh:deadbeef", "cicd")
    assert takeover.acquired and takeover.took_over_from == dead_replay.run_id


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


# ---------------------------------------------------------------------------
# Redacted at write time, like the recorder
# ---------------------------------------------------------------------------


async def test_json_columns_are_scrubbed_before_they_reach_the_file(tmp_db_path: Path) -> None:
    """A registered secret and a credential-shaped string inside a `RunOutcome`'s `final`,
    an approval's plan and an escalation payload never reach the raw bytes of the file."""
    import re

    from src.harness.observability import Redactor, SecretRegistry

    secret = "sentinel-secret-value-9f8e7d6c"
    shaped = "ghp_" + "A" * 36
    redactor = Redactor(SecretRegistry([secret]), [re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")])
    store = SqliteMemoryStore(tmp_db_path, redactor=redactor)
    await store.initialize()

    run_id = new_run_id()
    leaky = outcome(run_id, "escalated", escalation=True).model_copy(
        update={
            "final": {"bundle": {"logs": [{"excerpt": f"token {secret} and {shaped}"}]}},
            "escalation": outcome(run_id, escalation=True).escalation.model_copy(  # type: ignore[union-attr]
                update={"payload": {"detail": shaped}}
            ),
        }
    )
    await store.save_run(leaky)
    record = approval("apr_leak").model_copy(
        update={"plan": {"plan": {"body": secret}}, "context": {"note": shaped}}
    )
    await store.save_approval(record)

    raw = tmp_db_path.read_bytes()
    for sidecar in ("-wal", "-shm"):
        path = tmp_db_path.with_name(tmp_db_path.name + sidecar)
        if path.exists():
            raw += path.read_bytes()
    assert secret.encode() not in raw
    assert shaped.encode() not in raw

    stored = await store.get_run(run_id)
    assert stored is not None
    excerpt = stored.final["bundle"]["logs"][0]["excerpt"]  # type: ignore[index, call-overload]
    assert excerpt == "token ***REDACTED*** and ***REDACTED***"
    assert (await store.get_approval("apr_leak")).plan == {"plan": {"body": "***REDACTED***"}}  # type: ignore[union-attr]


async def test_base64_file_content_is_scrubbed_through_the_encoding(tmp_db_path: Path) -> None:
    """Audit finding 8: a token inside a drafted file travels as `content_b64`, which no
    pattern matches. Neither the token nor its base64 spelling reaches the file bytes; a
    payload with nothing to remove is stored byte-for-byte, so re-executing it is exact."""
    import base64
    import re

    from src.harness.observability import Redactor, SecretRegistry

    shaped = "ghp_" + "B" * 36
    redactor = Redactor(SecretRegistry(), [re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")])
    store = SqliteMemoryStore(tmp_db_path, redactor=redactor)
    await store.initialize()

    leaky_b64 = base64.b64encode(f"API_TOKEN={shaped}\n".encode()).decode("ascii")
    clean_b64 = base64.b64encode(b"print('hello')\n").decode("ascii")
    record = approval("apr_b64").model_copy(
        update={
            "plan": {
                "tool_calls": [
                    {"tool": "create_or_update_file", "args": {"content_b64": leaky_b64}},
                    {"tool": "create_or_update_file", "args": {"content_b64": clean_b64}},
                ]
            }
        }
    )
    await store.save_approval(record)
    run_id = new_run_id()
    await store.save_run(
        outcome(run_id).model_copy(update={"final": {"plan": {"content_b64": leaky_b64}}})
    )

    raw = tmp_db_path.read_bytes()
    for sidecar in ("-wal", "-shm"):
        path = tmp_db_path.with_name(tmp_db_path.name + sidecar)
        if path.exists():
            raw += path.read_bytes()
    assert shaped.encode() not in raw
    assert leaky_b64.encode() not in raw, "the base64 spelling of the token is one decode away"
    assert clean_b64.encode() in raw, "content with nothing to remove is stored verbatim"

    stored = await store.get_approval("apr_b64")
    assert stored is not None
    calls = stored.plan["tool_calls"]
    assert isinstance(calls, list)
    scrubbed = base64.b64decode(calls[0]["args"]["content_b64"]).decode()  # type: ignore[index, call-overload]
    assert scrubbed == "API_TOKEN=***REDACTED***\n"
    assert calls[1]["args"]["content_b64"] == clean_b64  # type: ignore[index, call-overload]
