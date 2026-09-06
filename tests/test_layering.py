"""PLAN.md line 66 — "the single most important test in the repo".

`src/harness/**` is domain-agnostic. This test walks the AST of every module under
`src/harness/`, collects all `import` / `from ... import ...` targets, and asserts that
none of them resolve into `src.integrations` and none reference a denylisted domain word
as a whole path segment. It also greps the raw source text of every module (which catches
comments and docstrings that the import-target check cannot see) for the same denylist,
matched on word boundaries so that `ci` does not spuriously match `decision`, `explicit`,
`specific`, or `citation`.

The enforced denylist is exactly PLAN.md line 66's list — no more, no less:
    github, workflow_run, pytest, pull_request, ci

Do not widen this list unilaterally. If a wider list is desired, it is a contract change
that belongs in PLAN.md and should be routed back to harness-core (see
docs/progress/phase-0/harness-core.md — four Appendix-A-verbatim identifiers, "diff",
"commit_sha", "actions_in_window", "MAX_SIDE_EFFECTING_ACTIONS_PER_RUN", would collide with
a wider list and are contractually required).

Wave-3 audit finding 13: a dynamic import — `importlib.import_module("src.integrations...")`
or `__import__("src.integrations...")` — evades `ast.Import`/`ast.ImportFrom` collection
entirely, and the string literal denylist above does not (and per the ruling on finding 13,
should not) contain "integrations", since that word is not one of PLAN.md line 66's five.
This is closed cheaply below by a dedicated check that walks every `ast.Call` node looking
for a call to `importlib.import_module(...)` / a bare `import_module(...)` / `__import__(...)`
whose first positional argument is a string literal (or an f-string with a literal-only
first segment is not attempted — only a plain `ast.Constant` first argument is checked; a
computed module path built at runtime is out of scope for a static AST test by construction,
and is recorded as a residual, not-required-by-PLAN.md limitation). This does NOT widen the
string-literal denylist itself — it is a structurally different check, keyed on "does this
string, wherever it appears as the target of a dynamic-import call, resolve into
src.integrations", not on the denylisted domain words.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS_DIR = REPO_ROOT / "src" / "harness"

# Exactly PLAN.md line 66. Do not add to this list without a PLAN.md change.
DENYLIST = ["github", "workflow_run", "pytest", "pull_request", "ci"]

_DENYLIST_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in DENYLIST) + r")\b",
    re.IGNORECASE,
)


def _harness_modules() -> list[Path]:
    assert HARNESS_DIR.is_dir(), f"expected {HARNESS_DIR} to exist"
    return sorted(p for p in HARNESS_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _import_targets(tree: ast.Module) -> list[str]:
    """Every module path named by an `import` or `from ... import` statement."""
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # relative imports (node.level > 0) resolve within src.harness itself;
            # only absolute module targets can possibly reach src.integrations.
            targets.append(node.module)
    return targets


def _string_literals(tree: ast.Module) -> list[str]:
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
    return literals


def _is_dynamic_import_call(func: ast.expr) -> bool:
    """True for the callee of `importlib.import_module(...)`, a bare
    `import_module(...)` (i.e. `from importlib import import_module`), or `__import__(...)`.
    Deliberately name-based, not import-alias-resolved: this is a static AST scan, and the
    failure scenario in finding 13 doesn't require defeating an alias too, just a plain call.
    """
    if isinstance(func, ast.Attribute):
        return func.attr == "import_module"
    if isinstance(func, ast.Name):
        return func.id in {"import_module", "__import__"}
    return False


def _dynamic_import_targets(tree: ast.Module) -> list[str]:
    """Every string-literal first argument passed to a dynamic-import call.

    Only a plain `ast.Constant` first argument is recognised; a module path built at
    runtime from concatenation/f-strings/variables cannot be resolved statically and is
    out of scope by construction (see module docstring, finding 13).
    """
    targets: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_dynamic_import_call(node.func)):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            targets.append(first.value)
    return targets


MODULES = _harness_modules()


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_import_of_integrations(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for target in _import_targets(tree):
        assert not target.startswith("src.integrations"), (
            f"{path.relative_to(REPO_ROOT)} imports {target!r}, which resolves into "
            "src.integrations — the harness layer must not know about any integration."
        )
        assert not target.startswith("integrations"), (
            f"{path.relative_to(REPO_ROOT)} imports {target!r}, which resolves into "
            "the integrations package — the harness layer must not know about any integration."
        )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_import_of_denylisted_domain_words(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for target in _import_targets(tree):
        segments = target.split(".")
        for word in DENYLIST:
            assert word.lower() not in (s.lower() for s in segments), (
                f"{path.relative_to(REPO_ROOT)} imports {target!r}, which contains the "
                f"denylisted domain word {word!r} as a path segment."
            )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_dynamic_import_of_integrations(path: Path) -> None:
    """Wave-3 audit finding 13: closes the `importlib.import_module("src.integrations...")`
    / `__import__(...)` hole that plain ast.Import/ImportFrom collection cannot see."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for target in _dynamic_import_targets(tree):
        assert not target.startswith("src.integrations"), (
            f"{path.relative_to(REPO_ROOT)} dynamically imports {target!r} via "
            "importlib.import_module()/__import__() — this resolves into src.integrations "
            "just as surely as a static import would, and the harness layer must not know "
            "about any integration."
        )
        assert not target.startswith("integrations"), (
            f"{path.relative_to(REPO_ROOT)} dynamically imports {target!r} via "
            "importlib.import_module()/__import__() — this resolves into the integrations "
            "package just as surely as a static import would."
        )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_denylisted_string_literals(path: Path) -> None:
    """PLAN.md line 66: 'also greps src/harness/** for those words as bare string literals.'"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for literal in _string_literals(tree):
        match = _DENYLIST_PATTERN.search(literal)
        assert match is None, (
            f"{path.relative_to(REPO_ROOT)} contains the denylisted domain word "
            f"{match.group(0)!r} in the string literal {literal!r}."
        )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_denylisted_words_in_raw_source(path: Path) -> None:
    """Catches comments and docstrings that the literal-only AST check above cannot see."""
    text = path.read_text(encoding="utf-8")
    match = _DENYLIST_PATTERN.search(text)
    assert match is None, (
        f"{path.relative_to(REPO_ROOT)} contains the denylisted domain word "
        f"{match.group(0)!r} somewhere in its raw source text."
    )


def test_harness_modules_were_actually_collected() -> None:
    """Guard against the parametrized tests above silently passing over zero modules."""
    assert len(MODULES) > 0, "no modules found under src/harness — layering test is vacuous"
