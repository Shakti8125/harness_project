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
can report it. The placeholder written by `mark_in_progress` would stay `in_progress`
forever, and `GET /v1/runs/{run_id}` could not tell a dead run from a slow one.

Nothing here touches the network or the model.
"""

from __future__ import annotations

import asyncio
import gc
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.api import main as api_main
from src.api.run_registry import RunRegistry
from src.harness.contracts import RunOutcome, TokenUsage
from src.harness.orchestrator import new_run_id


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> RunRegistry:
    """A fresh registry, swapped in for the module-level one `_supervised` writes to."""
    fresh = RunRegistry()
    monkeypatch.setattr(api_main, "registry", fresh)
    return fresh


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


async def test_a_failing_background_run_is_marked_failed(registry: RunRegistry) -> None:
    """The defect in one assertion: after the `202`, a raising run must not leave the
    row at `in_progress` forever.
    """
    run_id = new_run_id()
    await registry.mark_in_progress(run_id, "cicd", f"/v1/runs/{run_id}/trace")
    assert (await registry.get(run_id)).status == "in_progress"  # type: ignore[union-attr]

    async def boom() -> RunOutcome:
        raise RuntimeError("collector exploded after the 202")

    api_main._spawn_run(boom(), run_id)
    # Drain the loop until the supervised task has run its course.
    for _ in range(50):
        await asyncio.sleep(0)
        if not api_main._background_runs:
            break

    stored = await registry.get(run_id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.escalation is not None
    assert "collector exploded" in str(stored.escalation.payload["detail"])


async def test_the_failed_row_keeps_the_trace_link_and_created_at(
    registry: RunRegistry,
) -> None:
    """A failed run usually has real spans — the `TraceRecorder` writes them to SQLite as
    the run executes — and that link is the only evidence of *where* it died. Losing it
    while recording the failure would trade one blind spot for another.
    """
    run_id = new_run_id()
    await registry.mark_in_progress(run_id, "cicd", f"/v1/runs/{run_id}/trace")
    before = await registry.get(run_id)
    assert before is not None

    await registry.mark_failed(run_id, "something went wrong")

    after = await registry.get(run_id)
    assert after is not None
    assert after.status == "failed"
    assert after.trace_url == before.trace_url
    assert after.created_at == before.created_at
    assert after.integration == "cicd"
    assert after.completed_at is not None
    assert after.duration_ms is not None


async def test_mark_failed_on_an_unknown_run_does_not_raise(
    registry: RunRegistry,
) -> None:
    """Defensive: the supervisor calls this from a done-callback path where there is no
    caller left to catch anything. It must degrade rather than explode.
    """
    orphan = new_run_id()
    await registry.mark_failed(orphan, "orphan")
    stored = await registry.get(orphan)
    assert stored is not None
    assert stored.status == "failed"


async def test_an_inflight_run_is_strongly_referenced(registry: RunRegistry) -> None:
    """The liveness half — the one RUF006 was actually about.

    A task with no strong reference is collectable while still pending. Forcing a `gc`
    pass mid-flight and then asserting the run still completes is the closest a test can
    get to the failure without depending on when CPython chooses to collect.
    """
    run_id = new_run_id()
    await registry.mark_in_progress(run_id, "cicd", f"/v1/runs/{run_id}/trace")
    gate = asyncio.Event()

    async def slow() -> RunOutcome:
        await gate.wait()
        return _outcome(run_id)

    api_main._spawn_run(slow(), run_id)

    assert len(api_main._background_runs) == 1, "the task must be held while in flight"
    gc.collect()  # would reap a weakly-referenced task
    assert len(api_main._background_runs) == 1

    gate.set()
    for _ in range(50):
        await asyncio.sleep(0)
        if not api_main._background_runs:
            break

    # Completed normally, and the set does not leak.
    assert api_main._background_runs == set()


async def test_cancellation_is_not_recorded_as_a_failure(registry: RunRegistry) -> None:
    """`CancelledError` means the server is shutting down, not that the run was wrong.
    It is also not an `Exception` subclass, so the bare `except Exception` would let it
    through regardless — this pins that the row is left alone rather than being stamped
    `failed` on every restart.
    """
    run_id = new_run_id()
    await registry.mark_in_progress(run_id, "cicd", f"/v1/runs/{run_id}/trace")

    async def forever() -> RunOutcome:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")  # pragma: no cover

    api_main._spawn_run(forever(), run_id)
    await asyncio.sleep(0)  # let `_supervised` enter and await the inner coroutine
    task = next(iter(api_main._background_runs))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await registry.get(run_id)
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
