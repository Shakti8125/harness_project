"""Cross-run memory.

Frozen transcription of PLAN.md Appendix A.5. Memory is keyed on
``scope`` / ``subject_key`` / ``fingerprint`` -- three opaque strings the integration
computes -- so the store never learns what kind of thing it is remembering. Verdicts
are plain strings for the same reason: their vocabulary belongs to the integration.

Numeric constants mirror PLAN.md "Concrete numbers in one place".
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from src.harness.contracts import RunId, RunOutcome

# SQLite contention handling: busy_timeout, then app-level retries at these delays.
SQLITE_BUSY_TIMEOUT_MS: Final[int] = 5_000
WRITE_RETRY_BACKOFF_MS: Final[tuple[int, ...]] = (100, 200, 400)

# Prior-strength threshold: a signature counts as having an established prior once it
# has been seen at least this many times and one verdict holds at least this share.
PRIOR_MIN_OCCURRENCES: Final[int] = 3
PRIOR_DOMINANT_VERDICT_SHARE: Final[float] = 0.6

# Window over which `MemoryHit.actions_in_window` counts side-effecting actions.
ACTION_WINDOW_HOURS: Final[int] = 24


class SignatureKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str                             # caller-defined namespace for the subject
    subject_key: str                       # integration-defined
    fingerprint: str


class SignatureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signature_id: str
    scope: str
    subject_key: str
    fingerprint: str
    first_seen_at: datetime
    last_seen_at: datetime
    occurrences: int
    verdict_counts: dict[str, int]
    last_verdict: str | None
    last_run_id: RunId | None


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str
    signature_id: str
    run_id: RunId
    occurred_at: datetime
    verdict: str
    confidence: float
    action_taken: str | None
    action_outcome: Literal["passed_on_retry", "failed_again", "pending"] | None
    commit_sha: str | None


class MemoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: SignatureKey
    lookback_days: int = 30
    limit: int = 20


class MemoryHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: SignatureRecord | None                # None on a first-ever sighting
    recent: list[Observation]
    actions_in_window: dict[str, int]             # action name -> count over the window
    unavailable: bool = False                     # True when the store is degraded


class MemoryStore(Protocol):
    """Durable store for signatures, observations, run outcomes and run claims."""

    async def lookup(self, q: MemoryQuery) -> MemoryHit: ...

    async def upsert_signature(self, key: SignatureKey, verdict: str, run_id: RunId) -> str: ...

    async def record_observation(self, obs: Observation) -> None: ...

    async def update_observation_outcome(self, observation_id: str, outcome: str) -> None: ...

    async def save_run(self, outcome: RunOutcome) -> None: ...

    async def claim_run(self, idempotency_key: str, integration: str) -> RunClaim: ...

    async def heartbeat(self, run_id: RunId) -> None: ...


class RunClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    acquired: bool
    run_id: RunId
    existing_status: str | None
    existing_outcome: RunOutcome | None
    took_over_from: RunId | None = None
