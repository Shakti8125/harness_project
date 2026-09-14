# ruff: noqa: E501
"""`scripts/seed_demo_repo.sh` is bash and runs against a real GitHub account, so its
safety claims are pinned by reading it rather than running it.

Phase 5 audit finding 3: `--force` force-pushed `main` of an existing repository (its
history gone) and then aborted on the non-fast-forward demo-branch push, while the header
said `--force` "never deletes anything". The invariants now: `main` is never force-pushed;
a re-seed commits on top of the existing `main`; only the two `demo/*` branches -- the
script's own -- are replaced; registering the webhook is its own mode that touches no git
ref at all.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_demo_repo.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_main_is_never_force_pushed() -> None:
    text = _text()
    assert not re.search(r"push\s+(-q\s+)?(--force|-f)\b[^\n]*\bmain\b", text), "main must only ever fast-forward"
    assert "git push -q origin main" in text
    assert "reset -q --soft FETCH_HEAD" in text, "a re-seed builds on the fetched main, so history is kept"


def test_only_the_demo_branches_may_be_replaced() -> None:
    forced = re.findall(r"git push[^\n]*--force[^\n]*", _text())
    assert forced, "the demo branches are the script's own and are replaced on --force"
    for line in forced:
        assert "demo/" in line or '"$branch"' in line, line
        assert "main" not in line


def test_the_header_states_the_force_semantics_it_implements() -> None:
    header = "\n".join(_text().splitlines()[:30])
    assert "never deletes anything" not in header
    assert "fast-forward" in header and "demo/" in header
    assert "--webhook-only" in header


def test_the_webhook_only_mode_exists_and_the_next_steps_use_it() -> None:
    text = _text()
    assert "--webhook-only" in text
    assert re.search(r"\$0 \$REPO --webhook-only --webhook", text), "the printed next step must not re-seed"
    assert "--webhook https://<space>/webhooks/github --force" not in text


def test_the_secret_is_never_echoed() -> None:
    text = _text()
    assert "set -x" not in text
    for line in text.splitlines():
        if "HARNESS_GITHUB_WEBHOOK_SECRET" in line:
            assert not line.lstrip().startswith("echo"), line


def _bash() -> str | None:
    """Git's bash on Windows first: the `bash` on PATH there may be WSL's launcher with
    no distribution behind it."""
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(candidate).is_file():
            return candidate
    return shutil.which("bash")


def test_the_script_parses() -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("no bash available")
    completed = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True)
    if "WSL" in completed.stderr:
        pytest.skip("only WSL's bash launcher is available")
    assert completed.returncode == 0, completed.stderr
