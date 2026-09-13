"""`src/harness/storage.py`: one schema, one owner, applied at startup.

Appendix B.3's SQLite rows that live at the file level: migrations create a missing file;
a corrupt file is quarantined and recreated; a migration failure raises. Plus the two
things the runner promises the rest of the harness: idempotence, and the legacy Phase 1
`spans` table carried over rather than left beside `trace_span`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.harness import storage
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.harness.storage import (
    MIGRATIONS_DIR,
    applied_versions,
    apply_migrations,
    connect,
    latest_version,
    migration_files,
    schema_is_current,
)

EXPECTED_TABLES = {
    "schema_version", "failure_signature", "observation", "run", "trace_span",
    "approval", "escalation",
}


async def _tables(db_path: Path) -> set[str]:
    async with (
        connect(db_path) as db,
        db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor,
    ):
        return {str(row[0]) for row in await cursor.fetchall()}


async def test_migrations_create_the_whole_schema_on_a_missing_file(tmp_db_path: Path) -> None:
    assert not tmp_db_path.exists()
    applied = await apply_migrations(tmp_db_path)
    assert applied == [1]
    assert await _tables(tmp_db_path) >= EXPECTED_TABLES
    assert await applied_versions(tmp_db_path) == {1}
    assert await schema_is_current(tmp_db_path)


async def test_migrations_are_idempotent(tmp_db_path: Path) -> None:
    assert await apply_migrations(tmp_db_path) == [1]
    assert await apply_migrations(tmp_db_path) == []
    assert await applied_versions(tmp_db_path) == {1}


async def test_wal_and_pragmas(tmp_db_path: Path) -> None:
    """PLAN.md: WAL persisted in the file; busy_timeout and synchronous per connection."""
    await apply_migrations(tmp_db_path)
    async with connect(tmp_db_path) as db:
        async with db.execute("PRAGMA journal_mode") as cursor:
            assert (await cursor.fetchone())[0] == "wal"  # type: ignore[index]
        async with db.execute("PRAGMA busy_timeout") as cursor:
            assert (await cursor.fetchone())[0] == storage.BUSY_TIMEOUT_MS  # type: ignore[index]
        async with db.execute("PRAGMA synchronous") as cursor:
            assert (await cursor.fetchone())[0] == 1  # NORMAL  # type: ignore[index]
        async with db.execute("PRAGMA foreign_keys") as cursor:
            assert (await cursor.fetchone())[0] == 1  # type: ignore[index]


async def test_schema_is_current_is_false_before_migrations(tmp_db_path: Path) -> None:
    assert not await schema_is_current(tmp_db_path)
    tmp_db_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(tmp_db_path).close()  # an empty file, no schema_version
    assert not await schema_is_current(tmp_db_path)


async def test_legacy_spans_table_is_carried_over_and_dropped(tmp_db_path: Path) -> None:
    """A Phase 1/2 `data/harness.db` has a `spans` table; after migration there is exactly
    one span table and the old rows are readable through the recorder."""
    tmp_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(tmp_db_path) as legacy:
        legacy.executescript(
            """
            CREATE TABLE spans (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, span_id TEXT NOT NULL UNIQUE,
                parent_span_id TEXT, run_id TEXT NOT NULL, name TEXT NOT NULL,
                component TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
                ended_at TEXT, duration_ms INTEGER, attributes TEXT NOT NULL, error TEXT
            );
            INSERT INTO spans VALUES (1, 'sp_a', NULL, 'run_01J8ZZZZZZZZZZZZZZZZZZZZZZ', 'run',
              'orchestrator', 'ok', '2026-09-01T00:00:00.000000+00:00',
              '2026-09-01T00:00:01.000000+00:00', 1000, '{"k": 1}', NULL);
            INSERT INTO spans VALUES (2, 'sp_b', 'sp_a', 'run_01J8ZZZZZZZZZZZZZZZZZZZZZZ',
              'child', 'agent', 'error', '2026-09-01T00:00:00.500000+00:00',
              '2026-09-01T00:00:00.900000+00:00', 400, '{}', '{"type": "X", "message": "m"}');
            """
        )

    await apply_migrations(tmp_db_path)

    tables = await _tables(tmp_db_path)
    assert "trace_span" in tables and "spans" not in tables
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), []))
    trace = await recorder.read_trace("run_01J8ZZZZZZZZZZZZZZZZZZZZZZ")
    assert trace is not None
    assert [s.name for s in trace.spans] == ["run", "child"]
    assert trace.spans[0].attributes == {"k": 1}
    assert trace.spans[1].error == {"type": "X", "message": "m"}


async def test_corrupt_file_is_quarantined_and_recreated(tmp_db_path: Path) -> None:
    """Appendix B.3: `file is not a database` → rename to `.corrupt.<ts>`, recreate."""
    tmp_db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_db_path.write_bytes(b"this is not a sqlite file, it is forty bytes+")

    assert await apply_migrations(tmp_db_path) == [1]

    quarantined = list(tmp_db_path.parent.glob(f"{tmp_db_path.name}.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes().startswith(b"this is not")
    assert await _tables(tmp_db_path) >= EXPECTED_TABLES


async def test_a_failing_migration_raises(tmp_path: Path) -> None:
    """B.3: fail fast rather than serve a half-migrated schema; nothing is recorded."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_init.sql").write_text("CREATE TABLE ok (x INTEGER);", encoding="utf-8")
    (migrations / "002_bad.sql").write_text("CREATE TABLE ok (x INTEGER);", encoding="utf-8")
    db = tmp_path / "m.db"

    with pytest.raises(sqlite3.OperationalError):
        await apply_migrations(db, migrations)

    assert await applied_versions(db) == {1}
    assert not await schema_is_current(db, migrations)


def test_migration_files_are_ordered_and_named() -> None:
    files = migration_files()
    assert [v for v, _ in files] == sorted(v for v, _ in files)
    assert files[0][1].name == "001_init.sql"
    assert latest_version() == files[-1][0]


def test_malformed_migration_names_are_refused(tmp_path: Path) -> None:
    (tmp_path / "init.sql").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="NNN_name.sql"):
        migration_files(tmp_path)


def test_the_migrations_ship_with_the_image(repo_root: Path) -> None:
    """Same argument as the prompt templates: `.sql` files have no import machinery, only
    a path under `src/`, and the Dockerfile's `COPY src ./src` is what puts them there."""
    assert MIGRATIONS_DIR.is_relative_to(repo_root / "src")
    dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY src ./src" in dockerfile
    dockerignore = (repo_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert not any(
        pattern.strip().endswith(".sql") or pattern.strip() in ("src/", "src")
        for pattern in dockerignore
    )
