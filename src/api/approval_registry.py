"""Where a pending approval lives between the run that raised it and the `POST` that decides it.

**In-process, and deliberately so — replaced in the memory phase**, for exactly the
reasons `run_registry.py` gives for run outcomes: PLAN.md's durable `approval` table
arrives with `SqliteMemoryStore`, and building a second persistence layer now would mean
choosing which one to throw away later. What this costs: approvals do not survive a
restart, and are not shared between processes. Acceptable while the container runs one
worker; a restart turns a pending approval into a `404`, which is honest -- the plan it
carried is still in the run's trace.

Each entry keeps two things the deciding request needs and the `ApprovalRequest` itself
does not carry: **how to rebuild the gateway** the plan must execute through (the run's
mode, scenario and repository), and the run id, so the stored `RunOutcome` can be updated
to reflect the decision.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from src.harness.contracts import RunId
from src.integrations.cicd.schemas import ApprovalRequest


@dataclass(frozen=True)
class RunContext:
    """What it takes to build the same gateway the run used."""

    mode: Literal["live", "replay"]
    repo: str
    scenario_dir: Path | None


@dataclass
class ApprovalEntry:
    request: ApprovalRequest
    context: RunContext
    decided_at: datetime | None = None
    decided_by: str | None = None
    note: str | None = None


class ApprovalRegistry:
    """A small async-safe map of approval id to its entry."""

    def __init__(self) -> None:
        self._entries: dict[str, ApprovalEntry] = {}
        self._lock = asyncio.Lock()

    async def save(self, request: ApprovalRequest, context: RunContext) -> None:
        async with self._lock:
            self._entries[request.approval_id] = ApprovalEntry(request=request, context=context)

    async def get(self, approval_id: str) -> ApprovalEntry | None:
        async with self._lock:
            return self._entries.get(approval_id)

    async def transition(
        self,
        approval_id: str,
        state: Literal["approved", "rejected", "expired"],
        *,
        actor: str | None = None,
        note: str | None = None,
    ) -> tuple[ApprovalEntry, bool]:
        """Move a pending approval to a terminal state.

        Returns the entry and whether *this* call moved it. Single-use by construction
        (Appendix C): only a `pending` entry transitions, and the check and the write
        happen under one lock, so of two concurrent decisions exactly one applies and the
        other sees the winner's state and can answer `409` / `410`. Raises `KeyError` for
        an unknown id -- callers look the entry up first.
        """
        async with self._lock:
            entry = self._entries[approval_id]
            if entry.request.state != "pending":
                return entry, False
            entry.request = entry.request.model_copy(update={"state": state})
            entry.decided_at = datetime.now(UTC)
            entry.decided_by = actor
            entry.note = note
            return entry, True

    async def for_run(self, run_id: RunId) -> list[ApprovalEntry]:
        async with self._lock:
            return [e for e in self._entries.values() if e.request.run_id == run_id]
