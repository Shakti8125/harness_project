"""Cross-run memory.

The models and the ``MemoryStore`` Protocol are a frozen transcription of PLAN.md
Appendix A.5, amended additively in Phase 3 (see "Amendments" below). Memory is keyed on
``scope`` / ``subject_key`` / ``fingerprint`` -- three opaque strings the integration
computes -- so the store never learns what kind of thing it is remembering. Verdicts
are plain strings for the same reason: their vocabulary belongs to the integration.

Numeric constants mirror PLAN.md "Concrete numbers in one place".

**Amendments (Phase 3, `docs/progress/phase-3/dispatch.md` decisions 4, 5 and 7).**
The ``run`` and ``approval`` tables exist to serve the run and approval routes, and the
two in-process registries they replace said so in their own docstrings. The Protocol
therefore also carries the read side (``get_run``, ``list_runs``, ``list_escalations``)
and the approval side (``save_approval``, ``get_approval``, ``decide_approval``) with
``ApprovalRecord`` as its opaque-JSON carrier, plus three pure helpers -- the id
derivations PLAN.md fixes as formulas and the numeric half of the flakiness prior. The
words for what a dominant verdict *means* stay in the integration.

**Memory is a soft dependency** (PLAN.md Phase 3). Every ``SqliteMemoryStore`` method
raises ``MemoryStoreError`` when the store is genuinely unusable -- after the Appendix
B.3 ladder (``busy_timeout``, then three retries at 100/200/400 ms) has been exhausted --
and the caller is expected to degrade, never to propagate. The store itself does not
decide what "degraded" means for a run; that is the caller's vocabulary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from src.harness.contracts import EscalationRecord, RunId, RunOutcome, TokenUsage
from src.harness.errors import HarnessError
from src.harness.observability import Redactor
from src.harness.orchestrator import HEARTBEAT_STALE_AFTER_S, new_run_id
from src.harness.storage import apply_migrations, connect

logger = logging.getLogger("harness.memory")

# SQLite contention handling: busy_timeout, then app-level retries at these delays.
SQLITE_BUSY_TIMEOUT_MS: Final[int] = 5_000
WRITE_RETRY_BACKOFF_MS: Final[tuple[int, ...]] = (100, 200, 400)

# Prior-strength threshold: a signature counts as having an established prior once it
# has been seen at least this many times and one verdict holds at least this share.
PRIOR_MIN_OCCURRENCES: Final[int] = 3
PRIOR_DOMINANT_VERDICT_SHARE: Final[float] = 0.6

# Window over which `MemoryHit.actions_in_window` counts side-effecting actions.
ACTION_WINDOW_HOURS: Final[int] = 24

#: The one fault this store knows how to inject (`Settings.fault_inject`): every
#: connection raises SQLite's own "database is locked" before the B.3 ladder, so the
#: ladder is exercised and each call degrades in roughly the sum of its delays.
FAULT_SQLITE_LOCKED: Final[str] = "sqlite_locked"

#: Prefix of the synthesised idempotency key `save_run` writes for a run that was never
#: claimed (an orchestrator driven outside the API). The column is NOT NULL UNIQUE and
#: the outcome does not carry a key; naming the situation beats inventing a plausible one.
UNCLAIMED_KEY_PREFIX: Final[str] = "unclaimed:"

#: Row status of a run another claim took over (Appendix C). Not a `RunOutcome.status`
#: member -- A.1 is frozen -- so `get_run` serves such a row as `failed`.
_STATUS_SUPERSEDED: Final[str] = "superseded"
_STATUS_IN_PROGRESS: Final[str] = "in_progress"


class SignatureKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str                             # caller-defined namespace for the subject
    subject_key: str                       # integration-defined
    fingerprint: str


class SignatureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signature_id: str
    scope: str
    subject_key: str
    fingerprint: str
    first_seen_at: datetime
    last_seen_at: datetime
    occurrences: int
    verdict_counts: dict[str, int]
    last_verdict: str | None
    last_run_id: RunId | None


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str
    signature_id: str
    run_id: RunId
    occurred_at: datetime
    verdict: str
    confidence: float
    action_taken: str | None
    action_outcome: Literal["passed_on_retry", "failed_again", "pending"] | None
    commit_sha: str | None


class MemoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: SignatureKey
    lookback_days: int = 30
    limit: int = 20


class MemoryHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: SignatureRecord | None                # None on a first-ever sighting
    recent: list[Observation]
    actions_in_window: dict[str, int]             # action name -> count over the window
    unavailable: bool = False                     # True when the store is degraded


class RunClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    acquired: bool
    run_id: RunId
    existing_status: str | None
    existing_outcome: RunOutcome | None
    took_over_from: RunId | None = None


class ApprovalRecord(BaseModel):
    """One row of the `approval` table. Amendment to A.5 (dispatch decision 4).

    `plan` and `context` are opaque JSON: the first is whatever the integration's plan
    model dumps to, the second whatever the API layer needs to act on the approval later
    (how to rebuild the gateway). The store reads neither.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    run_id: RunId
    state: Literal["pending", "approved", "rejected", "expired"]
    plan: dict[str, JsonValue]
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    context: dict[str, JsonValue] = {}


class MemoryStore(Protocol):
    """Durable store for signatures, observations, run outcomes and run claims."""

    async def lookup(self, q: MemoryQuery) -> MemoryHit: ...

    async def upsert_signature(
        self, key: SignatureKey, verdict: str | None, run_id: RunId
    ) -> str: ...

    async def record_observation(self, obs: Observation) -> None: ...

    async def update_observation_outcome(self, observation_id: str, outcome: str) -> None: ...

    async def save_run(self, outcome: RunOutcome) -> None: ...

    async def claim_run(self, idempotency_key: str, integration: str) -> RunClaim: ...

    async def heartbeat(self, run_id: RunId) -> None: ...

    # -- amendments (Phase 3) -------------------------------------------------------

    async def get_run(self, run_id: RunId) -> RunOutcome | None: ...

    async def list_runs(
        self, *, limit: int = 50, status: str | None = None, cursor: str | None = None
    ) -> tuple[list[RunOutcome], str | None]: ...

    async def list_escalations(
        self, *, limit: int = 50
    ) -> list[tuple[RunId, EscalationRecord]]: ...

    async def save_approval(self, record: ApprovalRecord) -> None: ...

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None: ...

    async def decide_approval(
        self,
        approval_id: str,
        state: Literal["approved", "rejected", "expired"],
        *,
        actor: str | None = None,
        note: str | None = None,
    ) -> tuple[ApprovalRecord, bool]: ...


class MemoryStoreError(HarnessError):
    """The store could not complete an operation after the B.3 ladder was exhausted.

    Raised, not returned, because a store call has no result envelope to carry a failure
    in; callers wrap every call and degrade. The cause is chained.
    """


# ---------------------------------------------------------------------------
# Pure helpers -- the formulas PLAN.md fixes, and the numeric half of the prior
# ---------------------------------------------------------------------------


def signature_id_for(key: SignatureKey) -> str:
    """PLAN.md Phase 3 step 5: `sha256(f"{scope}|{subject_key}|{fingerprint}")[:32]`."""
    raw = f"{key.scope}|{key.subject_key}|{key.fingerprint}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def observation_id_for(signature_id: str, run_id: RunId) -> str:
    """One observation per signature per run, by construction.

    Deterministic so that a second write for the same (signature, run) -- the integration
    recording the verdict first and the action later -- lands on the same row, and a retry
    inside a run cannot insert twice.
    """
    return "obs_" + hashlib.sha256(f"{signature_id}|{run_id}".encode()).hexdigest()[:24]


def dominant_verdict(
    occurrences: int,
    verdict_counts: Mapping[str, int],
    *,
    min_occurrences: int = PRIOR_MIN_OCCURRENCES,
    min_share: float = PRIOR_DOMINANT_VERDICT_SHARE,
) -> str | None:
    """The verdict that dominates a signature's history, if one does.

    The numeric half of PLAN.md's flakiness prior: at least `min_occurrences` sightings and
    one verdict holding at least `min_share` of them. Takes the two numbers rather than a
    `SignatureRecord` because the same rule is applied to the record at lookup time and to
    the prior a bundle carries later. What the dominant verdict *means* -- which verdicts
    imply which hint -- is the integration's vocabulary, not this module's.
    """
    if occurrences < min_occurrences or not verdict_counts:
        return None
    verdict, count = max(verdict_counts.items(), key=lambda item: item[1])
    return verdict if count / occurrences >= min_share else None


def has_outcome(recent: Sequence[Observation], outcome: str) -> bool:
    """True when any observation in `recent` recorded `outcome` for its action."""
    return any(obs.action_outcome == outcome for obs in recent)


def is_chain_member(idempotency_key: str, candidate: str) -> bool:
    """Whether `candidate` is `idempotency_key` itself or one of its takeover rows.

    Appendix C spells a takeover as `<key>#<n>` with `n` the attempt number, and nothing
    else: a key that merely starts with `<key>#` -- the API's `<key>#fresh:<nonce>` replay
    claims, or an unrelated key that happens to share the prefix -- is a different run
    and must never stand in for the chain (audit finding 1).
    """
    if candidate == idempotency_key:
        return True
    prefix = f"{idempotency_key}#"
    return candidate.startswith(prefix) and candidate[len(prefix):].isdecimal()


# ---------------------------------------------------------------------------
# The SQLite implementation
# ---------------------------------------------------------------------------


def _ts(value: datetime) -> str:
    """One fixed-width ISO 8601 UTC spelling, so string comparison is time comparison."""
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


class SqliteMemoryStore:
    """`MemoryStore` over one SQLite file, shared with the trace recorder.

    PLAN.md Phase 3, "Decision (SQLite, single writer, WAL)": WAL is set by the migration
    runner (persistent in the file), `busy_timeout` and `synchronous=NORMAL` by every
    connection (`storage.connect`), and one process-wide lock serialises writes. No
    connection is held between calls, the same trade the recorder makes: a file open per
    call, and nothing to reason about across concurrent runs.

    The lock is created lazily per event loop rather than once in `__init__`: an
    `asyncio.Lock` binds to the loop it first waits on, and the composition root builds
    this object before any loop runs (and the test client runs the app on a different
    loop from the test). One loop per process in production makes this the same thing as
    the plan's "one process-wide lock".

    **Redacted at write time, like the recorder.** `outcome_json` carries the whole
    `RunOutcome` -- the integration's raw log excerpts and diff patches included -- and
    `plan_json` carries drafted file contents; the HTTP boundary digests those, the file
    would otherwise keep them verbatim. With a `redactor`, every JSON column this class
    writes (`outcome_json`, `plan_json`, `context_json`, the escalation `payload_json`) is
    scrubbed first, so the same registered secrets and credential shapes that never reach
    a span never reach the file either. Nothing here decides *what* is a secret.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        run_id_factory: Callable[[], RunId] = new_run_id,
        stale_after_s: float = HEARTBEAT_STALE_AFTER_S,
        clock: Callable[[], datetime] | None = None,
        fault_inject: str | None = None,
        retry_backoff_ms: Sequence[int] = WRITE_RETRY_BACKOFF_MS,
        redactor: Redactor | None = None,
    ) -> None:
        self.db_path = db_path
        self.redactor = redactor
        self._run_id_factory = run_id_factory
        self._stale_after = timedelta(seconds=stale_after_s)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._fault = fault_inject
        self._backoff_ms = tuple(retry_backoff_ms)
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

    # -- plumbing -----------------------------------------------------------------
    def _dump(self, model: BaseModel) -> str:
        """The model as JSON text, scrubbed through the redactor when one is configured."""
        payload: JsonValue = model.model_dump(mode="json")
        if self.redactor is not None:
            payload = self.redactor.scrub(payload)
        return _dumps(payload)

    def _dump_json(self, value: dict[str, JsonValue]) -> str:
        scrubbed: JsonValue = self.redactor.scrub(value) if self.redactor is not None else value
        return _dumps(scrubbed)

    async def initialize(self) -> None:
        """Apply the migrations. Exempt from fault injection: the process must boot."""
        await apply_migrations(self.db_path)

    def _write_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[Any]:
        if self._fault == FAULT_SQLITE_LOCKED:
            raise sqlite3.OperationalError("database is locked")
        async with connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            yield db

    async def _with_retries(self, op: Callable[[], Any]) -> Any:
        """Appendix B.3: after `busy_timeout`, three application retries, then give up."""
        for attempt in range(len(self._backoff_ms) + 1):
            try:
                return await op()
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or attempt == len(self._backoff_ms):
                    raise MemoryStoreError(f"memory store unavailable: {exc}") from exc
                delay_ms = self._backoff_ms[attempt]
                logger.warning(
                    "memory: %s; retrying in %d ms (%d of %d)",
                    exc, delay_ms, attempt + 1, len(self._backoff_ms),
                )
                await asyncio.sleep(delay_ms / 1000)
            except sqlite3.DatabaseError as exc:
                raise MemoryStoreError(f"memory store failed: {exc}") from exc
        raise AssertionError("unreachable")  # pragma: no cover

    async def _read(self, op: Callable[[Any], Any]) -> Any:
        async def attempt() -> Any:
            async with self._connect() as db:
                return await op(db)

        return await self._with_retries(attempt)

    async def _write(self, op: Callable[[Any], Any]) -> Any:
        async def attempt() -> Any:
            async with self._connect() as db:
                result = await op(db)
                await db.commit()
                return result

        async with self._write_lock():
            return await self._with_retries(attempt)

    # -- signatures and observations ------------------------------------------------
    async def lookup(self, q: MemoryQuery) -> MemoryHit:
        signature_id = signature_id_for(q.key)
        now = self._clock()
        recent_since = _ts(now - timedelta(days=q.lookback_days))
        window_since = _ts(now - timedelta(hours=ACTION_WINDOW_HOURS))

        async def op(db: Any) -> MemoryHit:
            async with db.execute(
                "SELECT * FROM failure_signature WHERE signature_id = ?", (signature_id,)
            ) as cursor:
                row = await cursor.fetchone()
            record = _record_from(row) if row is not None else None
            async with db.execute(
                "SELECT * FROM observation WHERE signature_id = ? AND occurred_at >= ?"
                " ORDER BY occurred_at DESC, rowid DESC LIMIT ?",
                (signature_id, recent_since, q.limit),
            ) as cursor:
                rows = await cursor.fetchall()
            async with db.execute(
                "SELECT action_taken, COUNT(*) AS n FROM observation"
                " WHERE signature_id = ? AND action_taken IS NOT NULL AND occurred_at >= ?"
                " GROUP BY action_taken",
                (signature_id, window_since),
            ) as cursor:
                actions = await cursor.fetchall()
            return MemoryHit(
                record=record,
                recent=[_observation_from(r) for r in rows],
                actions_in_window={str(a["action_taken"]): int(a["n"]) for a in actions},
            )

        result: MemoryHit = await self._read(op)
        return result

    async def upsert_signature(
        self, key: SignatureKey, verdict: str | None, run_id: RunId
    ) -> str:
        """Count one sighting of `key`, and its verdict when the caller vouches for one.

        `verdict=None` records the sighting -- `occurrences`, `last_seen_at`,
        `last_run_id` -- without a verdict: `verdict_counts` and `last_verdict` are left
        as they were. That is how a verdict the caller itself would not act on (audit
        finding 4: one below the confidence gate) dilutes the dominant share instead of
        building it. The observation row still carries every verdict with its confidence.
        """
        signature_id = signature_id_for(key)
        now = _ts(self._clock())

        async def op(db: Any) -> None:
            async with db.execute(
                "SELECT occurrences, verdict_counts, last_verdict FROM failure_signature"
                " WHERE signature_id = ?",
                (signature_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.execute(
                    "INSERT INTO failure_signature (signature_id, scope, subject_key,"
                    " fingerprint, first_seen_at, last_seen_at, occurrences, verdict_counts,"
                    " last_verdict, last_run_id) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (
                        signature_id, key.scope, key.subject_key, key.fingerprint,
                        now, now, _dumps({verdict: 1} if verdict is not None else {}),
                        verdict, run_id,
                    ),
                )
                return
            counts = _loads_counts(row["verdict_counts"])
            last_verdict = row["last_verdict"]
            if verdict is not None:
                counts[verdict] = counts.get(verdict, 0) + 1
                last_verdict = verdict
            await db.execute(
                "UPDATE failure_signature SET occurrences = ?, verdict_counts = ?,"
                " last_seen_at = ?, last_verdict = ?, last_run_id = ? WHERE signature_id = ?",
                (
                    int(row["occurrences"]) + 1, _dumps(counts), now, last_verdict, run_id,
                    signature_id,
                ),
            )

        await self._write(op)
        return signature_id

    async def record_observation(self, obs: Observation) -> None:
        """Insert, or replace every field of the row with the same `observation_id`.

        The integration writes the verdict first and the action later under one
        deterministic id (`observation_id_for`), so this is an upsert on purpose.
        """

        async def op(db: Any) -> None:
            await db.execute(
                "INSERT INTO observation (observation_id, signature_id, run_id, occurred_at,"
                " verdict, confidence, action_taken, action_outcome, commit_sha)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(observation_id) DO UPDATE SET"
                " signature_id = excluded.signature_id, run_id = excluded.run_id,"
                " occurred_at = excluded.occurred_at, verdict = excluded.verdict,"
                " confidence = excluded.confidence, action_taken = excluded.action_taken,"
                " action_outcome = excluded.action_outcome, commit_sha = excluded.commit_sha",
                (
                    obs.observation_id, obs.signature_id, obs.run_id, _ts(obs.occurred_at),
                    obs.verdict, obs.confidence, obs.action_taken, obs.action_outcome,
                    obs.commit_sha,
                ),
            )

        await self._write(op)

    async def update_observation_outcome(self, observation_id: str, outcome: str) -> None:
        async def op(db: Any) -> None:
            await db.execute(
                "UPDATE observation SET action_outcome = ? WHERE observation_id = ?",
                (outcome, observation_id),
            )

        await self._write(op)

    # -- runs ---------------------------------------------------------------------
    async def save_run(self, outcome: RunOutcome) -> None:
        """Write the outcome onto its claimed row, or create the row for an unclaimed run.

        Also files the outcome's `EscalationRecord`, when it has one, on the `escalation`
        table under the `db` channel (dispatch decision 13).
        """
        now = _ts(self._clock())

        async def op(db: Any) -> None:
            await db.execute(
                "INSERT INTO run (run_id, idempotency_key, integration, status, created_at,"
                " heartbeat_at, completed_at, outcome_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id) DO UPDATE SET status = excluded.status,"
                " completed_at = excluded.completed_at, outcome_json = excluded.outcome_json",
                (
                    outcome.run_id, f"{UNCLAIMED_KEY_PREFIX}{outcome.run_id}",
                    outcome.integration, outcome.status, _ts(outcome.created_at), now,
                    _ts(outcome.completed_at) if outcome.completed_at else None,
                    self._dump(outcome),
                ),
            )
            if outcome.escalation is not None:
                record = outcome.escalation
                await db.execute(
                    "INSERT INTO escalation (escalation_id, run_id, reason, payload_json,"
                    " created_at, channel, delivered_at) VALUES (?, ?, ?, ?, ?, 'db', ?)"
                    " ON CONFLICT(escalation_id) DO UPDATE SET reason = excluded.reason,"
                    " payload_json = excluded.payload_json, delivered_at = excluded.delivered_at",
                    (
                        record.escalation_id, outcome.run_id, record.reason,
                        self._dump(record), now, now,
                    ),
                )

        await self._write(op)

    async def claim_run(self, idempotency_key: str, integration: str) -> RunClaim:
        """Appendix C's claim protocol, including the stale-heartbeat takeover."""
        now = self._clock()
        run_id = self._run_id_factory()

        async def op(db: Any) -> RunClaim:
            cursor = await db.execute(
                "INSERT INTO run (run_id, idempotency_key, integration, status, created_at,"
                " heartbeat_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(idempotency_key) DO NOTHING",
                (run_id, idempotency_key, integration, _STATUS_IN_PROGRESS, _ts(now), _ts(now)),
            )
            if cursor.rowcount == 1:
                return RunClaim(
                    acquired=True, run_id=run_id, existing_status=None, existing_outcome=None
                )
            # The newest row in the key's takeover chain: the bare key, or `key#n`. The
            # LIKE over-matches (`_`/`%` are wildcards, and any `key#...` suffix qualifies),
            # so membership is decided in Python: audit finding 1 was a `key#fresh:<nonce>`
            # row -- a demo replay -- answering for the real key's chain.
            async with db.execute(
                "SELECT * FROM run WHERE idempotency_key = ? OR idempotency_key LIKE ?",
                (idempotency_key, f"{idempotency_key}#%"),
            ) as select:
                candidates = await select.fetchall()
            chain = [
                row for row in candidates
                if is_chain_member(idempotency_key, str(row["idempotency_key"]))
            ]
            assert chain  # the conflict proves the bare key's row exists
            existing = max(chain, key=lambda row: int(row["attempt"]))
            status = str(existing["status"])
            if status == _STATUS_IN_PROGRESS and self._is_stale(existing, now):
                attempt = int(existing["attempt"]) + 1
                previous = str(existing["run_id"])
                await db.execute(
                    "UPDATE run SET status = ? WHERE run_id = ?", (_STATUS_SUPERSEDED, previous)
                )
                await db.execute(
                    "INSERT INTO run (run_id, idempotency_key, integration, status, attempt,"
                    " superseded_run_id, created_at, heartbeat_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id, f"{idempotency_key}#{attempt}", integration,
                        _STATUS_IN_PROGRESS, attempt, previous, _ts(now), _ts(now),
                    ),
                )
                logger.warning(
                    "memory: run %s took over %s (stale heartbeat, attempt %d)",
                    run_id, previous, attempt,
                )
                return RunClaim(
                    acquired=True, run_id=run_id, existing_status=status,
                    existing_outcome=None, took_over_from=previous,
                )
            return RunClaim(
                acquired=False,
                run_id=str(existing["run_id"]),
                existing_status=status,
                existing_outcome=_outcome_from(existing),
            )

        claim: RunClaim = await self._write(op)
        return claim

    def _is_stale(self, row: Any, now: datetime) -> bool:
        last = row["heartbeat_at"] or row["created_at"]
        return now - _parse_ts(str(last)) > self._stale_after

    async def heartbeat(self, run_id: RunId) -> None:
        now = _ts(self._clock())

        async def op(db: Any) -> None:
            await db.execute(
                "UPDATE run SET heartbeat_at = ? WHERE run_id = ? AND status = ?",
                (now, run_id, _STATUS_IN_PROGRESS),
            )

        await self._write(op)

    async def get_run(self, run_id: RunId) -> RunOutcome | None:
        async def op(db: Any) -> RunOutcome | None:
            async with db.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)) as cursor:
                row = await cursor.fetchone()
            return _outcome_from(row) if row is not None else None

        result: RunOutcome | None = await self._read(op)
        return result

    async def list_runs(
        self, *, limit: int = 50, status: str | None = None, cursor: str | None = None
    ) -> tuple[list[RunOutcome], str | None]:
        """Newest first by `run_id` (ULID-shaped, so id order is creation order).

        Keyset pagination: `cursor` is the last `run_id` of the previous page. `status`
        filters on the row's status column, which is the outcome's status for every row
        but a superseded one (served as `failed`).
        """
        clauses = ["1 = 1"]
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if cursor is not None:
            clauses.append("run_id < ?")
            params.append(cursor)
        params.append(limit + 1)

        async def op(db: Any) -> list[Any]:
            async with db.execute(
                f"SELECT * FROM run WHERE {' AND '.join(clauses)} ORDER BY run_id DESC LIMIT ?",
                params,
            ) as select:
                return list(await select.fetchall())

        rows: list[Any] = await self._read(op)
        page = rows[:limit]
        outcomes = [o for o in (_outcome_from(row) for row in page) if o is not None]
        next_cursor = str(page[-1]["run_id"]) if len(rows) > limit and page else None
        return outcomes, next_cursor

    async def list_escalations(
        self, *, limit: int = 50
    ) -> list[tuple[RunId, EscalationRecord]]:
        async def op(db: Any) -> list[Any]:
            async with db.execute(
                "SELECT run_id, payload_json FROM escalation"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ) as select:
                return list(await select.fetchall())

        rows: list[Any] = await self._read(op)
        return [
            (str(row["run_id"]), EscalationRecord.model_validate_json(row["payload_json"]))
            for row in rows
        ]

    # -- approvals ----------------------------------------------------------------
    async def save_approval(self, record: ApprovalRecord) -> None:
        async def op(db: Any) -> None:
            await db.execute(
                "INSERT INTO approval (approval_id, run_id, state, plan_json, requested_at,"
                " expires_at, decided_at, decided_by, decision_note, context_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(approval_id) DO UPDATE SET state = excluded.state,"
                " plan_json = excluded.plan_json, decided_at = excluded.decided_at,"
                " decided_by = excluded.decided_by, decision_note = excluded.decision_note,"
                " context_json = excluded.context_json",
                (
                    record.approval_id, record.run_id, record.state,
                    self._dump_json(record.plan),
                    _ts(record.requested_at), _ts(record.expires_at),
                    _ts(record.decided_at) if record.decided_at else None,
                    record.decided_by, record.decision_note, self._dump_json(record.context),
                ),
            )

        await self._write(op)

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        async def op(db: Any) -> ApprovalRecord | None:
            async with db.execute(
                "SELECT * FROM approval WHERE approval_id = ?", (approval_id,)
            ) as cursor:
                row = await cursor.fetchone()
            return _approval_from(row) if row is not None else None

        result: ApprovalRecord | None = await self._read(op)
        return result

    async def decide_approval(
        self,
        approval_id: str,
        state: Literal["approved", "rejected", "expired"],
        *,
        actor: str | None = None,
        note: str | None = None,
    ) -> tuple[ApprovalRecord, bool]:
        """Move a pending approval to a terminal state; single-use by construction.

        The `WHERE state = 'pending'` is the whole mechanism: of two concurrent decisions
        exactly one updates a row, and the other reads the winner's state back. Raises
        `KeyError` for an unknown id -- callers look the record up first.
        """
        now = _ts(self._clock())

        async def op(db: Any) -> tuple[ApprovalRecord, bool]:
            cursor = await db.execute(
                "UPDATE approval SET state = ?, decided_at = ?, decided_by = ?,"
                " decision_note = ? WHERE approval_id = ? AND state = 'pending'",
                (state, now, actor, note, approval_id),
            )
            applied = cursor.rowcount == 1
            async with db.execute(
                "SELECT * FROM approval WHERE approval_id = ?", (approval_id,)
            ) as select:
                row = await select.fetchone()
            if row is None:
                raise KeyError(approval_id)
            return _approval_from(row), applied

        result: tuple[ApprovalRecord, bool] = await self._write(op)
        return result


# ---------------------------------------------------------------------------
# Row <-> model
# ---------------------------------------------------------------------------


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _loads_counts(raw: Any) -> dict[str, int]:
    parsed = json.loads(raw) if raw else {}
    return {str(k): int(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def _record_from(row: Any) -> SignatureRecord:
    return SignatureRecord(
        signature_id=row["signature_id"],
        scope=row["scope"],
        subject_key=row["subject_key"],
        fingerprint=row["fingerprint"],
        first_seen_at=_parse_ts(row["first_seen_at"]),
        last_seen_at=_parse_ts(row["last_seen_at"]),
        occurrences=int(row["occurrences"]),
        verdict_counts=_loads_counts(row["verdict_counts"]),
        last_verdict=row["last_verdict"],
        last_run_id=row["last_run_id"],
    )


def _observation_from(row: Any) -> Observation:
    return Observation(
        observation_id=row["observation_id"],
        signature_id=row["signature_id"],
        run_id=row["run_id"],
        occurred_at=_parse_ts(row["occurred_at"]),
        verdict=row["verdict"],
        confidence=float(row["confidence"]),
        action_taken=row["action_taken"],
        action_outcome=row["action_outcome"],
        commit_sha=row["commit_sha"],
    )


def _approval_from(row: Any) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row["approval_id"],
        run_id=row["run_id"],
        state=row["state"],
        plan=json.loads(row["plan_json"]),
        requested_at=_parse_ts(row["requested_at"]),
        expires_at=_parse_ts(row["expires_at"]),
        decided_at=_parse_ts(row["decided_at"]) if row["decided_at"] else None,
        decided_by=row["decided_by"],
        decision_note=row["decision_note"],
        context=json.loads(row["context_json"] or "{}"),
    )


def _outcome_from(row: Any) -> RunOutcome | None:
    """The stored outcome, or a placeholder for a row that has none yet.

    A claimed-but-unfinished row answers `in_progress` so a `GET` between accept and
    completion says "not yet" rather than "never existed". A superseded row (Appendix C
    takeover) answers `failed` with a `run_timeout` escalation naming its successor: A.1
    has no `superseded` status, and a heartbeat that went stale is the run timing out as
    far as the store can tell.
    """
    if row["outcome_json"]:
        return RunOutcome.model_validate_json(row["outcome_json"])
    run_id = str(row["run_id"])
    created_at = _parse_ts(row["created_at"])
    trace_url = f"/v1/runs/{run_id}/trace"
    if str(row["status"]) == _STATUS_SUPERSEDED:
        now = datetime.now(UTC)
        return RunOutcome(
            run_id=run_id,
            integration=str(row["integration"]),
            status="failed",
            created_at=created_at,
            completed_at=now,
            duration_ms=int((now - created_at).total_seconds() * 1000),
            stages=[],
            total_tokens=TokenUsage(),
            final={},
            escalation=EscalationRecord(
                # Deterministic: a reader polling this row must see one escalation, not a
                # new id on every read.
                escalation_id=(
                    "esc_" + hashlib.sha256(f"{run_id}|superseded".encode()).hexdigest()[:16]
                ),
                reason="run_timeout",
                message="the run's heartbeat went stale and a later claim took it over",
                payload={"superseded": True},
                channels=["db"],
                delivered_at=now,
            ),
            trace_url=trace_url,
        )
    return RunOutcome(
        run_id=run_id,
        integration=str(row["integration"]),
        status="in_progress",
        created_at=created_at,
        completed_at=None,
        duration_ms=None,
        stages=[],
        total_tokens=TokenUsage(),
        final={},
        trace_url=trace_url,
    )


__all__ = [
    "ACTION_WINDOW_HOURS",
    "FAULT_SQLITE_LOCKED",
    "PRIOR_DOMINANT_VERDICT_SHARE",
    "PRIOR_MIN_OCCURRENCES",
    "ApprovalRecord",
    "MemoryHit",
    "MemoryQuery",
    "MemoryStore",
    "MemoryStoreError",
    "Observation",
    "RunClaim",
    "SignatureKey",
    "SignatureRecord",
    "SqliteMemoryStore",
    "dominant_verdict",
    "has_outcome",
    "observation_id_for",
    "signature_id_for",
]
