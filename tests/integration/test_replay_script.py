"""`scripts/replay.py` under a memory outage (Phase 3 audit finding 9).

The API's run routes degrade when the store cannot be reached -- run unclaimed, return
the outcome, log the loss. The CLI path did the same work through unguarded `claim_run`
and `save_run` calls, with `save_run` *before* the print: a locked store at the end of a
replay spent three model calls and printed a traceback instead of the outcome. The
script is loaded from its file (it is not a package) with the composition root patched to
a context whose store fails every call after the B.3 ladder.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from src.harness.memory import SqliteMemoryStore
from tests.integration.test_memory_e2e import make_context


def _load_replay_script(repo_root: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("replay_script", repo_root / "scripts/replay.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_a_replay_still_prints_its_outcome_when_the_store_is_down(
    tmp_db_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    replay = _load_replay_script(repo_root)
    faulty = SqliteMemoryStore(
        tmp_db_path, fault_inject="sqlite_locked", retry_backoff_ms=(1, 2, 3)
    )
    context = make_context(tmp_db_path, memory=faulty)
    monkeypatch.setattr(replay, "get_app_context", lambda: context)
    monkeypatch.setattr(sys, "argv", ["replay.py", "flaky_test"])

    code = await replay.main()

    out, err = capsys.readouterr()
    assert code == 0, err
    assert "run_id:      run_" in out
    assert "status:      escalated" in out, "the fail-closed cap: 999 retries, retry denied"
    assert "policy_denied" in out
    assert "memory store unavailable: running unclaimed" in err
    assert "memory store unavailable: outcome run_" in err and "not recorded" in err
    assert "Traceback" not in err
    # The outcome was printed before the failed save, and the trace is still readable.
    assert out.index("status:") < out.index("gateway spans:")
