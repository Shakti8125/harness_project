"""Wave-3 re-audit finding 3 (LOW), second bullet:

    "Guard that costs one test: a grep gate alongside test_no_env_access.py asserting
    `Settings(` appears nowhere outside src/settings.py."

`get_settings()` (`src/settings.py`) is the only leak-safe way to construct
`Settings`. A `ValidationError` raised by calling `Settings()` directly carries the
offending *value* verbatim in `err["input"]` / the dict pydantic prints in
`str(ValidationError)` (pydantic elides only the *middle* of a long input dict, which
does not help a short settings dict with three or four keys). `get_settings()` is the
only call site in the repository that catches that error and re-raises `SettingsError`
with every value stripped (`_redact_validation_error`, `src/settings.py:129-149`).
Nothing in the type system stops a future module -- `src/api/deps.py` (the Phase 1
composition root, PLAN.md:306) or a future `scripts/replay.py` -- from calling
`Settings()` directly, or from writing `except ValidationError as e: logger.error(e)`
around it, either of which routes a live secret straight to a log line or an uncaught
traceback before the `Redactor` (Phase 5) exists to scrub anything.

**What this gate checks, precisely.** Every `ast.Call` node, in every `*.py` file in
the repository outside `tests/`, `.venv`/caches/dot-dirs, and `src/settings.py` itself
(the one permitted construction site), whose callee is:

  - a bare name `Settings` -- `Settings(...)`, reached via
    `from src.settings import Settings`; or
  - an attribute access whose final segment is exactly `Settings` --
    `settings.Settings(...)`, `src.settings.Settings(...)`.

This is an AST match on the exact callee *identifier*, not a text/substring grep,
specifically because a naive substring search for the literal text `Settings(` also
matches inside other, unrelated identifiers that happen to end in the same six
letters immediately before an opening paren -- concretely, `BaseSettings(` (the
pydantic-settings superclass call inside `class Settings(BaseSettings):` itself)
contains `Settings(` as a contiguous substring, and a case-insensitive variant of the
same naive check also matches `get_settings()`. `SettingsConfigDict(...)` is a
different identifier again. All three are legitimate, non-leaking constructs used
throughout this file and `src/settings.py`, and a gate that flagged any of them would
be a spurious failure worse than no gate at all (the brief for this test, verbatim).
AST equality on the callee identifier is exact, so none of the three can ever match:
their `Name.id` / `Attribute.attr` is `BaseSettings`, `get_settings`, and
`SettingsConfigDict` respectively -- never the four-character-shorter string
`Settings`. `SettingsError` (the redaction-carrying exception type defined alongside
`Settings` in the same module) is excluded the same way: its identifier is
`SettingsError`, not `Settings`.

**On `except ValidationError` outside `src/settings.py` (the finding's second
bullet, "the second half of the same bypass"): deliberately NOT gated here, and here
is why.** The bypass the finding describes is two steps: (1) construct `Settings()`
directly, somewhere that isn't `get_settings()`, and (2) catch the resulting
`ValidationError` and print it (or let it propagate to an uncaught traceback) before
any redaction happens. Step (2) is only dangerous in the presence of step (1) --
a `ValidationError` raised by *validating literally anything else* (a webhook
payload, an LLM structured-output response, any other pydantic model in this
project) carries none of `Settings`'s secrets and is ordinary, expected control flow
for a FastAPI/pydantic codebase. This gate already forbids step (1) outright, for
every file this scan covers: with no direct `Settings()` call reachable outside
`src/settings.py`, there is no way for a `ValidationError` carrying a `Settings`
field's value to exist outside `src/settings.py` in the first place, so an
independent "no `except ValidationError` outside settings.py" rule would add no
coverage against *this* finding while pre-emptively banning ordinary validation of
unrelated models that Phase 1+ will legitimately need (e.g. validating an inbound
GitHub webhook body). Banning that ahead of time, on the strength of a finding that
is actually fully closed by the narrower `Settings(` gate above, is exactly the
"spurious failure on a legitimate construct" this test is warned against. Left
uncovered, and recorded as a deliberate scoping decision rather than a gap: if a
future agent ever reintroduces a *second* direct `Settings()` call site, this gate
fails on that call already, before any `except ValidationError` around it would even
matter.

**Known residual (documented, not required by the finding, same shape as
`test_layering.py`'s dynamic-import residual):** subclassing --
`class MySettings(Settings): ...` followed by `MySettings()` -- constructs a
`Settings`-shaped object without the literal callee identifier `Settings` ever
appearing at the call site, so it is invisible to both this AST check and to any
plain-text grep. Nothing in this project subclasses `Settings` today (verified by
`test_settings_is_never_subclassed_outside_its_own_module` below), and doing so
would already be a strange, unmotivated design choice for a `BaseSettings` leaf
class. Out of scope by construction for a static per-call-site AST scan.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = REPO_ROOT / "src"
TESTS_DIR = REPO_ROOT / "tests"
SETTINGS_MODULE = SRC_DIR / "settings.py"

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
    """Every `.py` file in the repository outside `tests/`, `.venv`/caches, and
    `src/settings.py` itself -- same scope as `test_no_env_access.py`'s collector,
    for the same reason: the rule this closes ("only one module may construct
    `Settings`") is a repository-wide rule, not a `src/`-only one, so a future
    `scripts/replay.py` (Phase 1) is covered automatically, with no test change,
    the moment it exists.
    """
    assert SRC_DIR.is_dir(), f"expected {SRC_DIR} to exist"
    assert SETTINGS_MODULE.is_file(), f"expected {SETTINGS_MODULE} to exist"
    return sorted(p for p in REPO_ROOT.rglob("*.py") if not _is_excluded(p))


def _is_settings_identifier(node: ast.expr) -> bool:
    """True iff `node` is a callee that resolves, by exact identifier, to `Settings`
    itself -- never to `BaseSettings`, `SettingsConfigDict`, `get_settings`,
    `SettingsError`, or any other name that merely shares letters with it.
    """
    if isinstance(node, ast.Name):
        return node.id == "Settings"
    if isinstance(node, ast.Attribute):
        return node.attr == "Settings"
    return False


def _settings_constructor_calls(tree: ast.Module) -> list[ast.Call]:
    """Every `ast.Call` whose callee is exactly the identifier `Settings`."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_settings_identifier(node.func)
    ]


def _settings_subclasses(tree: ast.Module) -> list[ast.ClassDef]:
    """Every `class Foo(Settings): ...` -- the documented residual (see module
    docstring): a subclass constructor call wouldn't carry the `Settings` identifier
    at its own call site, so it can't be caught by `_settings_constructor_calls`.
    Used only to prove the residual is currently empty, not to gate on it.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(_is_settings_identifier(base) for base in node.bases)
    ]


FILES = _source_files()


def test_settings_module_exists_and_is_excluded() -> None:
    assert SETTINGS_MODULE.is_file(), (
        f"expected {SETTINGS_MODULE} to exist as the sole permitted Settings() call site"
    )


def test_source_files_were_actually_collected() -> None:
    """Guard against the collector silently passing over zero files."""
    assert len(FILES) > 0, "no source files found outside tests/ — scan is vacuous"


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_direct_settings_construction_outside_settings_module(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = _settings_constructor_calls(tree)
    assert not calls, (
        f"{path.relative_to(REPO_ROOT)} calls Settings(...) directly at line(s) "
        f"{[c.lineno for c in calls]} — only src/settings.py:get_settings() may "
        "construct Settings, because it is the only call site that redacts a "
        "ValidationError before it can reach stdout/stderr (Wave-3 re-audit finding 3)."
    )


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_settings_is_never_subclassed_outside_its_own_module(path: Path) -> None:
    """Documents that the subclassing residual (module docstring) is empty today --
    not a gate against a hole this test cannot close, just proof there is currently
    no live instance of it to worry about."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    subclasses = _settings_subclasses(tree)
    assert not subclasses, (
        f"{path.relative_to(REPO_ROOT)} subclasses Settings at line(s) "
        f"{[c.lineno for c in subclasses]} — this project has none today, and a "
        "subclass would be able to construct a Settings-shaped object without ever "
        "writing the literal callee `Settings(...)`, escaping this AST gate. "
        "If this is intentional, this test (and the module docstring's residual "
        "note) needs updating by test-verifier, not just a green run."
    )


def test_settings_module_itself_is_the_one_construction_site() -> None:
    """Sanity-check the detector against real code, both directions: it finds the
    one legitimate call in `get_settings()`, proving the scan is not vacuous, and
    `src/settings.py` is excluded from the parametrized scan above (so this call
    does not fail its own gate)."""
    tree = ast.parse(SETTINGS_MODULE.read_text(encoding="utf-8"), filename=str(SETTINGS_MODULE))
    calls = _settings_constructor_calls(tree)
    assert len(calls) == 1, (
        f"expected exactly one Settings() call in src/settings.py (get_settings's "
        f"body), found {len(calls)} — this detector should find the real call site "
        "it's designed to permit, or the detector itself is broken"
    )
    assert SETTINGS_MODULE not in FILES, (
        "src/settings.py must be excluded from the parametrized scan, or its own "
        "legitimate Settings() call would fail its own gate"
    )


def test_tests_dir_itself_is_excluded_from_the_scan() -> None:
    """`tests/conftest.py` and any future test asserting on `Settings(...)` directly
    (constructing a deliberately-invalid one to test `get_settings`'s redaction, for
    instance) must never be swept into this gate — this is an application-code rule,
    not a test-code rule."""
    assert TESTS_DIR.is_dir()
    for path in FILES:
        assert TESTS_DIR not in path.parents, (
            f"{path} is under tests/ and must be excluded from the Settings() construction scan"
        )


# --- Non-vacuity: prove the detector actually distinguishes the dangerous call from
# every legitimate lookalike, using synthetic source strings rather than trusting
# that today's real files merely happen to be clean. ---


def _calls_in_source(source: str) -> list[ast.Call]:
    return _settings_constructor_calls(ast.parse(source))


def test_detector_flags_bare_name_call() -> None:
    calls = _calls_in_source(
        "from src.settings import Settings\n"
        "s = Settings()\n"
    )
    assert len(calls) == 1


def test_detector_flags_module_qualified_call() -> None:
    calls = _calls_in_source(
        "import src.settings\n"
        "s = src.settings.Settings()\n"
    )
    assert len(calls) == 1


def test_detector_flags_call_inside_try_except_validation_error() -> None:
    """The exact bypass shape named in the finding: constructing Settings() directly
    and catching ValidationError around it, outside get_settings()."""
    calls = _calls_in_source(
        "from pydantic import ValidationError\n"
        "from src.settings import Settings\n"
        "try:\n"
        "    s = Settings()\n"
        "except ValidationError as e:\n"
        "    logger.error(e)\n"
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "source",
    [
        # SettingsConfigDict(...) — a different identifier entirely; must not match.
        (
            "from pydantic_settings import SettingsConfigDict\n"
            "model_config = SettingsConfigDict(env_prefix='HARNESS_')\n"
        ),
        # BaseSettings(...) — the pydantic-settings superclass; must not match, even
        # though the substring "Settings(" is literally contained within it.
        (
            "from pydantic_settings import BaseSettings\n"
            "class Settings(BaseSettings):\n"
            "    pass\n"
            "b = BaseSettings()\n"
        ),
        # get_settings() — the one leak-safe accessor; must not match.
        (
            "from src.settings import get_settings\n"
            "s = get_settings()\n"
        ),
        # SettingsError(...) — the redaction exception type; must not match.
        (
            "from src.settings import SettingsError\n"
            "raise SettingsError('values withheld')\n"
        ),
        # A totally unrelated *args-settings kwarg name; must not match.
        (
            "def configure(settings=None):\n"
            "    return settings\n"
        ),
    ],
    ids=[
        "SettingsConfigDict",
        "BaseSettings_and_class_def",
        "get_settings",
        "SettingsError",
        "unrelated_settings_kwarg",
    ],
)
def test_detector_does_not_flag_legitimate_lookalikes(source: str) -> None:
    assert _calls_in_source(source) == []


def test_detector_subclass_residual_is_real_and_documented() -> None:
    """Proves the documented residual actually exists as claimed: a subclass
    constructor call is genuinely invisible to `_settings_constructor_calls`, which
    is why `_settings_subclasses` exists as a separate, narrower check."""
    source = (
        "from src.settings import Settings\n"
        "class MySettings(Settings):\n"
        "    pass\n"
        "m = MySettings()\n"
    )
    tree = ast.parse(source)
    assert _settings_constructor_calls(tree) == [], (
        "if this ever starts matching, the module docstring's residual note is "
        "stale and should be corrected, not silently left in place"
    )
    assert len(_settings_subclasses(tree)) == 1
