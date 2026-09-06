"""Staged run driver.

Frozen transcription of PLAN.md Appendix A.2. The orchestrator knows about stages,
gates and run state; it does not know what a stage produces. Stage payload types
arrive as ``StageSpec.output_model`` and short-circuit logic arrives as
``StageSpec.gate`` -- both supplied by the integration, so every short-circuit
condition that is domain-specific lives outside this module.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from pydantic import BaseModel, ConfigDict

from src.harness.contracts import RunId, RunOutcome, RunRequest, StageRecord

# PLAN.md "Concrete numbers in one place": run heartbeat interval / staleness = 15 s / 120 s.
HEARTBEAT_INTERVAL_S: Final[float] = 15.0
HEARTBEAT_STALE_AFTER_S: Final[float] = 120.0


class StageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    name: str
    agent_key: str
    output_model: type[BaseModel]          # not serialized
    required: bool = True
    gate: Callable[[RunState], GateDecision] | None = None


class GateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proceed: bool
    reason: str
    escalate_as: str | None = None


class RunState(BaseModel):                 # mutable, extra="allow" -- the only non-frozen model
    model_config = ConfigDict(extra="allow", frozen=False, arbitrary_types_allowed=True)

    run_id: RunId
    request: RunRequest
    artifacts: dict[str, BaseModel]        # stage outputs, keyed by an integration-chosen name
    degraded: list[str]
    stages: list[StageRecord]


class Orchestrator:
    """Runs the configured stages for one request and returns its outcome."""

    async def run(self, request: RunRequest) -> RunOutcome:
        raise NotImplementedError


StageSpec.model_rebuild()
