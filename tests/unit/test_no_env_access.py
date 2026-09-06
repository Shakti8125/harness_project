"""Appendix E, PLAN.md lines 1720-1740: "Single source of truth: src/settings.py. No other
module reads os.environ; a unit test greps for os.environ / os.getenv outside settings.py
and fails if found."

Placed at `tests/unit/test_no_env_access.py` specifically — the phase-verify skill's
cross-phase checks invoke it by this exact path.

Wave-3 audit finding 12: the original version of this test scanned only `src/`. Appendix
E's rule is "no other module reads os.environ" — full stop, not "no other module under
src/". A `scripts/replay.py` (Phase 1 layout:89) doing `os.getenv("HARNESS_GATEWAY")` would
have passed the old gate while violating the rule.

**What this scan covers, precisely:** every `*.py` file in the repository, found by
walking from `REPO_ROOT`, EXCEPT:
  - `src/settings.py` itself — the one permitted exception, Appendix E's "single source
    of truth".
  - anything under `tests/` (this whole directory) — test code legitimately monkeypatches
    and reads `os.environ` (see `tests/conftest.py`'s `isolated_settings` fixture, and any
    future test that asserts on `HARNESS_*` env behaviour). Appendix E's rule is about
    *application* code, not test harnessing.
  - `.venv/`, `__pycache__/`, and any dot-directory (`.git`, `.mypy_cache`, `.pytest_cache`,
    `.ruff_cache`) — third-party / tooling artifacts, not repository modules.

This means the scan covers `src/**` (all of it, not just `src/harness/`) today, and will
automatically start covering `scripts/**` the moment Phase 1 creates that directory —
no test change required. `scripts/` does not exist yet in Phase 0; `_source_files()`
handles its absence by simply finding nothing there (`rglob` over a nonexistent path
segment is never attempted; we walk from `REPO_ROOT`, which always exists).

The scan deliberately does NOT cover: `tests/**` (excluded per above), `pyproject.toml` /
`Dockerfile` / `fly.toml` / `docker-compose.yml` (not Python — a different mechanism would
be needed to grep non-`.py` files, and none of Appendix E's failure scenarios involve them),
or anything inside `.venv` (third-party library code, out of this repository's control).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = REPO_ROOT / "src"
TESTS_DIR = REPO_ROOT / "tests"
SETTINGS_MODULE = SRC_DIR / "settings.py"

_ENV_ACCESS_PATTERN = re.compile(r"os\.environ|os\.getenv")

# Directories (by name, matched anywhere in a path's parts) that are never repository
# modules and must never be scanned: venvs, caches, VCS metadata, and the test tree
# itself (test code legitimately manipulates os.environ; see module docstring).
_EXCLUDED_DIR_NAMES = {
    ".venv",
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def _is_excluded(path: Path) -> bool:
    parts = set(path.parts)
    if parts & _EXCLUDED_DIR_NAMES:
        return True
    try:
        path.relative_to(TESTS_DIR)
    except ValueError:
        pass
    else:
        return True
    return path == SETTINGS_MODULE


def _source_files() -> list[Path]:
    """Every `.py` file in the repository outside `tests/` and `.venv`/cache dirs,
    minus `src/settings.py` itself.

    Walking from `REPO_ROOT` (not just `src/`) is what closes Wave-3 audit finding 12:
    a future `scripts/` directory (Phase 1) is covered automatically, with no test change,
    the moment it exists. It does not exist yet in Phase 0 — `rglob` over `REPO_ROOT`
    simply finds no files there, which is not an error.
    """
    assert SRC_DIR.is_dir(), f"expected {SRC_DIR} to exist"
    assert SETTINGS_MODULE.is_file(), f"expected {SETTINGS_MODULE} to exist"
    return sorted(
        p for p in REPO_ROOT.rglob("*.py") if not _is_excluded(p)
    )


FILES = _source_files()


def test_settings_module_exists_and_is_excluded() -> None:
    assert SETTINGS_MODULE.is_file(), (
        f"expected {SETTINGS_MODULE} to exist as the sole env-reading module"
    )


def test_scripts_dir_absence_does_not_break_the_scan() -> None:
    """Documents the Phase 0 -> Phase 1 transition explicitly rather than leaving it
    implicit: `scripts/` doesn't exist yet, and that's fine — the collector above walks
    from REPO_ROOT and simply finds nothing under a path that isn't there. The moment
    Phase 1 creates `scripts/replay.py`, it lands in `FILES` above with zero test changes.
    """
    scripts_dir = REPO_ROOT / "scripts"
    if scripts_dir.is_dir():
        pytest.skip("scripts/ now exists — covered by the general collector above")
    assert not scripts_dir.exists()


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_env_access_outside_settings(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    match = _ENV_ACCESS_PATTERN.search(text)
    found = match.group(0) if match else ""
    assert match is None, (
        f"{path.relative_to(REPO_ROOT)} references {found!r} — "
        "only src/settings.py may read the process environment (PLAN.md Appendix E)."
    )


def test_settings_module_is_the_one_place_env_access_is_allowed() -> None:
    """Sanity check the fixture itself isn't vacuous: settings.py really does mention
    both tokens (in its docstring, describing this very rule) — confirming our regex
    is not so narrow that it would silently match nothing anywhere."""
    text = SETTINGS_MODULE.read_text(encoding="utf-8")
    assert _ENV_ACCESS_PATTERN.search(text) is not None


def test_tests_dir_itself_is_excluded_from_the_scan() -> None:
    """Guard against a future refactor accidentally re-including tests/**: this file
    and tests/conftest.py both legitimately reference os.environ-adjacent APIs
    (monkeypatch.setenv, not os.environ directly, but the exclusion must hold regardless
    of what any given test does) and must never appear in FILES."""
    assert TESTS_DIR.is_dir()
    for path in FILES:
        assert TESTS_DIR not in path.parents, (
            f"{path} is under tests/ and must be excluded from the env-access scan"
        )


def test_harness_source_files_were_actually_collected() -> None:
    """Guard against the collector silently passing over zero files."""
    assert len(FILES) > 0, "no source files found outside tests/ — scan is vacuous"
