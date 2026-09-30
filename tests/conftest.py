"""Shared fixtures for the whole test suite.

Test convention (see phase briefs): "A fresh temp SQLite per test via fixture. Never touch
./data/harness.db." Two fixtures enforce that:

- `tmp_db_path` hands back a path inside pytest's own `tmp_path` — never `./data/harness.db`
  — for any test that wants to open a SQLite connection directly.
- `isolated_settings` (autouse) points `Settings.database_path` at that same temp path for
  the duration of the test, via the `HARNESS_DATABASE_PATH` env var, and clears
  `get_settings`'s `lru_cache` before and after so no test can observe another test's
  settings snapshot and no test can accidentally write to the real data directory.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

_RETRY_DEFAULTS = {
    "HARNESS_LLM_TRANSIENT_MAX_ATTEMPTS": "4",
    "HARNESS_LLM_BACKOFF_BASE_S": "0.5",
    "HARNESS_LLM_BACKOFF_MAX_S": "8.0",
    "HARNESS_LLM_BACKOFF_JITTER": "full",
}


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """A per-test, never-shared SQLite file path. Nothing here touches ./data/harness.db."""
    return tmp_path / "test_harness.db"


@pytest.fixture(autouse=True)
def isolated_settings(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Redirect Settings.database_path to a per-test temp file and guarantee a clean
    `get_settings()` cache on both sides of the test, so tests never share, and never
    write to, ./data/harness.db.
    """
    monkeypatch.setenv("HARNESS_DATABASE_PATH", str(tmp_db_path))
    # The local `.env` may carry the free-tier retry profile (step-5 plan, Stage 1a).
    # Pin Appendix B.1's defaults so no attempt count in the suite depends on it;
    # `tests/unit/test_retry_settings.py` sets the profile where it means to.
    for name, value in _RETRY_DEFAULTS.items():
        monkeypatch.setenv(name, value)

    try:
        from src.settings import get_settings

        get_settings.cache_clear()
    except ImportError:
        # src/settings.py may be mid-construction early in Phase 0; degrade gracefully
        # rather than breaking every other test's collection.
        yield
        return

    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _real_data_db_mtime(repo_root: Path) -> float | None:
    db = repo_root / "data" / "harness.db"
    return db.stat().st_mtime if db.exists() else None


@pytest.fixture(autouse=True)
def _guard_real_db_untouched(repo_root: Path) -> Iterator[None]:
    """Belt-and-suspenders: fail loudly if a test manages to modify the real
    ./data/harness.db despite `isolated_settings` redirecting Settings elsewhere."""
    before = _real_data_db_mtime(repo_root)
    yield
    after = _real_data_db_mtime(repo_root)
    assert before == after, (
        "./data/harness.db was modified during a test — every test must use "
        "tmp_db_path / isolated_settings instead of the real database file."
    )
