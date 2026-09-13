"""Wave-3 audit finding 10, container-layout half.

`load_prompt_template` resolves `prompts/{name}.md` via `Path(__file__).parent /
"prompts"` -- relative to the module's own file, not the process's current working
directory. That is what makes the templates resolvable at all once they are `*.md` data
files instead of importable Python: a data file has no import machinery to find it, only
a path, and the path must not silently assume "cwd == repo root", which is not a
guarantee any of dev / `docker run` / a Space / a test runner invoked from elsewhere
actually gives you.

Two checks, cheap and independent of each other:

1. **Resolution survives a changed working directory** -- the direct test of the
   `Path(__file__)`-relative claim, with the module's own template cache cleared so this
   test cannot pass on a previous test's cached read.
2. **The image really ships the file** -- `Dockerfile`'s `COPY src ./src` (verbatim) and
   `.dockerignore` carrying no pattern that matches these two `.md` files. This is a
   static check on the repo's own build inputs, not a Docker build; it exists so that a
   later edit to either file cannot silently stop shipping the templates without any test
   noticing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.integrations.cicd import rendering


@pytest.fixture(autouse=True)
def _clear_template_cache() -> None:
    rendering._template_cache.clear()
    yield
    rendering._template_cache.clear()


def test_template_resolution_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert Path.cwd() == tmp_path
    assert os.getcwd() != str(Path(__file__).resolve().parents[2])

    template = rendering.load_prompt_template("investigator")

    assert template.version
    assert template.body


def test_both_prompt_templates_load_from_a_relocated_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    # Both bumped in Phase 3 (the prior-history clause); the Diagnostician's again in
    # Phase 4 (what each claim kind's `quote` must hold, for the Evaluator's checkers).
    expected = {"investigator": "2", "diagnostician": "3"}
    for name, version in expected.items():
        template = rendering.load_prompt_template(name)
        assert template.version == version
        assert "$" in template.body  # a string.Template body, not a literal string


def test_dockerfile_copies_the_whole_src_tree_verbatim(repo_root: Path) -> None:
    dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY src ./src" in dockerfile, (
        "the build no longer ships src/ wholesale -- prompts/*.md would need an "
        "explicit COPY of its own, and nothing today provides one"
    )


def test_dockerignore_does_not_exclude_the_prompt_templates(repo_root: Path) -> None:
    dockerignore = (repo_root / ".dockerignore").read_text(encoding="utf-8")
    patterns = [
        line.strip()
        for line in dockerignore.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    # The only `*.md` pattern must be the literal repo-root README, not a glob that
    # would also catch `src/integrations/cicd/prompts/*.md`.
    md_patterns = [p for p in patterns if p.endswith(".md")]
    assert md_patterns == ["README.md"], (
        f"a new .dockerignore pattern {md_patterns!r} may now exclude the prompt "
        "templates from the built image -- verify src/integrations/cicd/prompts/*.md "
        "specifically before widening this list"
    )


def test_the_prompt_files_actually_exist_on_disk_where_the_dockerfile_would_find_them(
    repo_root: Path,
) -> None:
    prompts_dir = repo_root / "src" / "integrations" / "cicd" / "prompts"
    assert (prompts_dir / "investigator.md").is_file()
    assert (prompts_dir / "diagnostician.md").is_file()
    # And no importable Python stand-in was left behind alongside them.
    assert not (prompts_dir / "investigator.py").exists()
    assert not (prompts_dir / "diagnostician.py").exists()
