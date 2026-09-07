"""Where a run's outcome lives between the request that started it and the one that reads it.

**In-process, and deliberately so — this is replaced in the memory phase.** PLAN.md puts
durable run storage behind `MemoryStore.save_run`, which does not exist yet; building a
second, parallel persistence layer now would mean designing the schema twice and then
choosing which one to throw away. A dict is honest about being temporary in a way a
half-built `runs` table would not be.

What that costs, stated rather than discovered later: outcomes do not survive a restart,
and they are not shared between processes. Both are acceptable while the container runs a
single uvicorn worker and the demo path (`POST /v1/replay/{scenario}`) is synchronous —
it returns the outcome in the same response that produced it and never needs this at all.
The asynchronous `POST /v1/runs` path is what reads it back.

The span trace is *not* stored here: it is already durable in SQLite, written by the
`TraceRecorder` as the run executes, and `GET /v1/runs/{id}/trace` reads it from there.
So a restart loses the outcome summary but keeps the evidence of what happened.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from src.harness.contracts import RunId, RunOutcome, TokenUsage


class RunRegistry:
    """A small async-safe map of run id to outcome."""

    def __init__(self) -> None:
        self._runs: dict[str, RunOutcome] = {}
        self._lock = asyncio.Lock()

    async def mark_in_progress(self, run_id: RunId, integration: str, trace_url: str) -> None:
        """Record a placeholder so a `GET` between accept and completion answers 200.

        Without it, the window between `202 Accepted` and the run finishing answers 404,
        which reads to a caller as "that run never existed" rather than "not yet".
        """
        async with self._lock:
            self._runs[run_id] = RunOutcome(
                run_id=run_id,
                integration=integration,
                status="in_progress",
                created_at=datetime.now(UTC),
                completed_at=None,
                duration_ms=None,
                stages=[],
                total_tokens=TokenUsage(),
                final={},
                trace_url=trace_url,
            )

    async def save(self, outcome: RunOutcome) -> None:
        async with self._lock:
            self._runs[outcome.run_id] = outcome

    async def get(self, run_id: str) -> RunOutcome | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def list(self, *, limit: int = 50, status: str | None = None) -> list[RunOutcome]:
        async with self._lock:
            runs = list(self._runs.values())
        if status is not None:
            runs = [run for run in runs if run.status == status]
        # Run ids are ULID-shaped, so reverse lexicographic order is newest-first without
        # needing to look at the timestamps.
        runs.sort(key=lambda run: run.run_id, reverse=True)
        return runs[:limit]
