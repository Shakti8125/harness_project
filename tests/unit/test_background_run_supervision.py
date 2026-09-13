"""Background runs are kept alive, and their failures are recorded.

`POST /v1/runs` and the async form of `POST /v1/replay/{scenario}` both answer `202` and
finish the work behind the response. Two separate defects lived in that handoff, and both
call sites carried the same `# noqa: RUF006` comment:

    asyncio.create_task(  # noqa: RUF006 - fire and forget; the registry is the handle

*Liveness.* RUF006 exists because `asyncio` holds only a **weak** reference to a task, so
one whose result nobody keeps may be garbage-collected mid-run. The comment asserted the
registry was the handle — but the registry holds a database row, not the task object, so
nothing kept the coroutine alive. The silenced lint was pointing at a real defect.

*Visibility.* A background run raises after its `202` is already sent, so no HTTP handler
can report it. The row written by `claim_run` would stay `in_progress` (until its
heartbeat went stale), and `GET /v1/runs/{run_id}` could not tell a dead run from a slow
one.

Phase 3 moved the run record from an in-process dict to the `MemoryStore`; the properties
pinned here are unchanged, and the tests now read them back through the store on a
per-test temp file.

Nothing here touches the network or the model.
"""

from __future__ import annotations

import asyncio
import gc
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.api import main as api_main
from src.api.deps import SECRET_PATTERNS, AppContext, build_secret_registry
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import RunOutcome, TokenUsage
from src.harness.memory import SqliteMemoryStore
from src.harness.observability import Redactor, TraceRecorder
from src.harness.orchestrator import new_run_id
from src.settings import get_settings


@pytest.fixture
async def registry(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> SqliteMemoryStore:
    """A fresh store on the per-test file, wired as the context `_supervised` writes to."""
    settings = get_settings()
    store = SqliteMemoryStore(tmp_db_path)
    await store.initialize()
    context = AppContext(
        settings=settings,
        recorder=TraceRecorder(
            db_path=tmp_db_path,
            redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
        ),
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=1000)),
        llm=object(),  # type: ignore[arg-type]  # never called here
        run_semaphore=asyncio.Semaphore(1),
        memory=store,
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    return store


async def _claim(store: SqliteMemoryStore, run_id: str) -> None:
    """Write the `in_progress` row the route would have claimed, under `run_id`."""
    store._run_id_factory = lambda: run_id  # the route lets the store mint; pin it here
    claim = await store.claim_run(f"cicd:test-{run_id}", "cicd")
    assert claim.acquired and claim.run_id == run_id


def _outcome(run_id: str) -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        integration="cicd",
        status="completed",
        created_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        duration_ms=1,
        stages=[],
        total_tokens=TokenUsage(),
        final={},
        trace_url=f"/v1/runs/{run_id}/trace",
    )


async def test_a_failing_background_run_is_marked_failed(registry: SqliteMemoryStore) -> None:
    """The defect in one assertion: after the `202`, a raising run must not leave the
    row at `in_progress` forever.
    """
    run_id = new_run_id()
    await _claim(registry, run_id)
    assert (await registry.get_run(run_id)).status == "in_progress"  # type: ignore[union-attr]

    async def boom() -> RunOutcome:
        raise RuntimeError("collector exploded after the 202")

    api_main._spawn_run(boom(), run_id)
    # Drain the loop until the supervised task has run its course. Real I/O now: the
    # failure record is a SQLite write, so this waits on the clock, not just the loop.
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not api_main._background_runs:
            break

    stored = await registry.get_run(run_id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.escalation is not None
    assert "collector exploded" in str(stored.escalation.payload["detail"])


async def test_the_failed_row_keeps_the_trace_link_and_created_at(
    registry: SqliteMemoryStore,
) -> None:
    """A failed run usually has real spans — the `TraceRecorder` writes them to SQLite as
    the run executes — and that link is the only evidence of *where* it died. Losing it
    while recording the failure would trade one blind spot for another.
    """
    run_id = new_run_id()
    await _claim(registry, run_id)
    before = await registry.get_run(run_id)
    assert before is not None

    await api_main.mark_failed(registry, run_id, "something went wrong")

    after = await registry.get_run(run_id)
    assert after is not None
    assert after.status == "failed"
    assert after.trace_url == before.trace_url
    assert after.created_at == before.created_at
    assert after.integration == "cicd"
    assert after.completed_at is not None
    assert after.duration_ms is not None


async def test_mark_failed_on_an_unknown_run_does_not_raise(
    registry: SqliteMemoryStore,
) -> None:
    """Defensive: the supervisor calls this from a done-callback path where there is no
    caller left to catch anything. It must degrade rather than explode.
    """
    orphan = new_run_id()
    await api_main.mark_failed(registry, orphan, "orphan")
    stored = await registry.get_run(orphan)
    assert stored is not None
    assert stored.status == "failed"


async def test_an_inflight_run_is_strongly_referenced(registry: SqliteMemoryStore) -> None:
    """The liveness half — the one RUF006 was actually about.

    A task with no strong reference is collectable while still pending. Forcing a `gc`
    pass mid-flight and then asserting the run still completes is the closest a test can
    get to the failure without depending on when CPython chooses to collect.
    """
    run_id = new_run_id()
    await _claim(registry, run_id)
    gate = asyncio.Event()

    async def slow() -> RunOutcome:
        await gate.wait()
        return _outcome(run_id)

    api_main._spawn_run(slow(), run_id)

    assert len(api_main._background_runs) == 1, "the task must be held while in flight"
    gc.collect()  # would reap a weakly-referenced task
    assert len(api_main._background_runs) == 1

    gate.set()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not api_main._background_runs:
            break

    # Completed normally, and the set does not leak.
    assert api_main._background_runs == set()


async def test_cancellation_is_not_recorded_as_a_failure(registry: SqliteMemoryStore) -> None:
    """`CancelledError` means the server is shutting down, not that the run was wrong.
    It is also not an `Exception` subclass, so the bare `except Exception` would let it
    through regardless — this pins that the row is left alone rather than being stamped
    `failed` on every restart.
    """
    run_id = new_run_id()
    await _claim(registry, run_id)

    async def forever() -> RunOutcome:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")  # pragma: no cover

    api_main._spawn_run(forever(), run_id)
    await asyncio.sleep(0)  # let `_supervised` enter and await the inner coroutine
    task = next(iter(api_main._background_runs))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await registry.get_run(run_id)
    assert stored is not None
    assert stored.status == "in_progress", "a shutdown must not be recorded as a failure"


def test_neither_call_site_still_silences_ruf006() -> None:
    """The `noqa` was the marker for this whole class of defect. If it comes back, so
    has the bug — a linter suppression is a much easier thing to reintroduce than the
    reasoning behind it.
    """
    source = Path("src/api/main.py").read_text(encoding="utf-8")
    assert "noqa: RUF006" not in source
    assert source.count("_spawn_run(") == 3  # one definition, two call sites
