"""The SQLite file: connections, pragmas, migrations.

DERIVED, NOT TRANSCRIBED. Appendix A does not specify this module; PLAN.md's layout puts
"migrations/" under `memory.py`. It is split out because two harness modules write the
same file -- `observability.py` (spans) and `memory.py` (everything else) -- and the
schema must have exactly one owner. Putting the runner in `memory.py` would make
`observability` import `memory`, which imports `orchestrator`, which imports
`observability`: a cycle. This module imports nothing else from the harness.

Three things live here and nowhere else:

1. **`connect()`** -- every connection to the file goes through it, so the per-connection
   pragmas PLAN.md Phase 3 fixes (`busy_timeout=5000`, `synchronous=NORMAL`, plus
   `foreign_keys=ON` for the `observation -> failure_signature` reference) are set once,
   in one place. `journal_mode=WAL` is persistent in the file and is set by the migration
   runner rather than per connection.
2. **`apply_migrations()`** -- `migrations/NNN_*.sql`, applied in filename order, each in
   one transaction, tracked in `schema_version`. Idempotent: an applied version is
   skipped. Every startup path calls it (the API lifespan, the Space launcher, the
   recorder, the store, the replay script), and a call that finds nothing to do costs one
   `SELECT`.
3. **The B.3 "file is not a database" recovery** -- a corrupt file is renamed to
   `<name>.corrupt.<ts>` and recreated. Memory is regenerable; a corrupt file taking the
   service down is not acceptable, a corrupt file silently reused is worse.

Appendix B.3's "migration failure at startup → process exits non-zero" is honoured by
*not* catching anything else here: a migration that fails raises, and the composition root
lets it propagate out of startup.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import aiosqlite

logger = logging.getLogger("harness.storage")

MIGRATIONS_DIR: Final[Path] = Path(__file__).with_name("migrations")

# PLAN.md Phase 3, "Decision (SQLite, single writer, WAL)".
BUSY_TIMEOUT_MS: Final[int] = 5_000

_MIGRATION_NAME: Final[re.Pattern[str]] = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

#: The message SQLite uses for a file that is not one. Matched case-insensitively on the
#: exception text because `sqlite3.DatabaseError` carries no structured code.
_NOT_A_DATABASE: Final[str] = "file is not a database"

#: The Phase 1 span table, superseded by `trace_span` in migration 001. Rows found in it
#: at migration time are carried over so a pre-existing local trace is not lost, and the
#: table is dropped so there is exactly one span table afterwards.
_LEGACY_SPANS_TABLE: Final[str] = "spans"


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    """Every `NNN_name.sql` under `directory`, sorted by version. Malformed names raise."""
    found: list[tuple[int, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise ValueError(f"migration {path.name!r} is not named NNN_name.sql")
        found.append((int(match.group(1)), path))
    versions = [version for version, _ in found]
    if len(set(versions)) != len(versions):
        raise ValueError(f"duplicate migration versions in {directory}: {versions}")
    return found


def latest_version(directory: Path = MIGRATIONS_DIR) -> int:
    """The highest migration version shipped with this build (0 when none)."""
    files = migration_files(directory)
    return files[-1][0] if files else 0


@asynccontextmanager
async def connect(db_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    """Open one connection with the per-connection pragmas applied, and close it after.

    Used as `async with connect(path) as db:` -- the same shape as `aiosqlite.connect`,
    with the pragmas already set. The parent directory is created so a first run on an
    empty volume does not fail on the open.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    try:
        await _pragma(db, f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        await _pragma(db, "PRAGMA synchronous=NORMAL")
        await _pragma(db, "PRAGMA foreign_keys=ON")
        yield db
    finally:
        await db.close()


async def _pragma(db: aiosqlite.Connection, statement: str) -> None:
    """Run a PRAGMA and close its cursor.

    A PRAGMA that sets a value also *returns* one, and a cursor left open on it counts as
    a statement in progress -- which makes the implicit COMMIT that `executescript` issues
    fail with "SQL statements in progress".
    """
    async with db.execute(statement):
        pass


async def applied_versions(db_path: Path) -> set[int]:
    """The migration versions recorded in `schema_version`; empty when the table is absent."""
    async with connect(db_path) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ) as cursor:
            if await cursor.fetchone() is None:
                return set()
        async with db.execute("SELECT version FROM schema_version") as cursor:
            rows = await cursor.fetchall()
    return {int(row[0]) for row in rows}


async def schema_is_current(db_path: Path, directory: Path = MIGRATIONS_DIR) -> bool:
    """True when every shipped migration is recorded as applied. Never raises."""
    try:
        applied = await applied_versions(db_path)
    except Exception:  # noqa: BLE001 - a readiness probe reports, it does not raise
        logger.exception("storage: could not read schema_version")
        return False
    return all(version in applied for version, _ in migration_files(directory))


def _quarantine_corrupt_file(db_path: Path) -> Path:
    """Appendix B.3: rename a file SQLite refuses to open and let the next open recreate it."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = db_path.with_name(f"{db_path.name}.corrupt.{stamp}")
    logger.critical(
        "storage: %s is not a database; moving it to %s and recreating", db_path, target
    )
    db_path.rename(target)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            sidecar.rename(target.with_name(target.name + suffix))
    return target


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def _carry_over_legacy_spans(db: aiosqlite.Connection) -> None:
    """Copy the Phase 1 `spans` rows into `trace_span` and drop the old table.

    Run right after migration 001's `CREATE TABLE`s. Ordered by the old table's `seq` so the
    read path's `ORDER BY rowid` reproduces the original insertion order.
    """
    if not await _table_exists(db, _LEGACY_SPANS_TABLE):
        return
    await db.execute(
        "INSERT OR IGNORE INTO trace_span (span_id, parent_span_id, run_id, name, component,"
        " status, started_at, ended_at, duration_ms, attributes_json, error_json)"
        " SELECT span_id, parent_span_id, run_id, name, component, status, started_at,"
        " ended_at, duration_ms, attributes, error FROM spans ORDER BY seq"
    )
    await db.execute(f"DROP TABLE {_LEGACY_SPANS_TABLE}")
    logger.info("storage: carried the legacy spans table over to trace_span")


async def _apply(db_path: Path, directory: Path) -> list[int]:
    applied = await applied_versions(db_path)
    newly: list[int] = []
    async with connect(db_path) as db:
        # Persistent in the file; set once here rather than on every connection.
        await _pragma(db, "PRAGMA journal_mode=WAL")
        # The runner owns its own bookkeeping table: a migration file must not have to
        # remember to create it, and the first file's failure must still be recordable.
        await db.execute(
            "CREATE TABLE IF NOT EXISTS schema_version"
            " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await db.commit()
        for version, path in migration_files(directory):
            if version in applied:
                continue
            script = path.read_text(encoding="utf-8")
            # `executescript` commits whatever is pending and then runs the text as-is,
            # so the transaction has to be *in* the text for the DDL to be atomic. The
            # version row is written afterwards, in its own commit: a failure between the
            # two leaves idempotent `IF NOT EXISTS` DDL applied and no version recorded,
            # which the next startup simply repeats.
            await db.executescript(f"BEGIN;\n{script}\nCOMMIT;")
            if version == 1:
                await _carry_over_legacy_spans(db)
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
            await db.commit()
            newly.append(version)
            logger.info("storage: applied migration %03d (%s)", version, path.name)
    return newly


async def apply_migrations(db_path: Path, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Bring `db_path` up to the latest shipped schema. Returns the versions applied now.

    A corrupt file is quarantined once and the migrations are applied to a fresh one; any
    other failure propagates (B.3: fail fast rather than serve a half-migrated schema).
    """
    try:
        return await _apply(db_path, directory)
    except sqlite3.DatabaseError as exc:
        if _NOT_A_DATABASE not in str(exc).lower() or not db_path.exists():
            raise
        _quarantine_corrupt_file(db_path)
        return await _apply(db_path, directory)
