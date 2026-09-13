"""Core envelopes exchanged between harness components.

Frozen transcription of PLAN.md Appendix A.1. Every model here is generic over the
domain: payload types (what an integration actually produces) never appear, they are
carried either as ``dict[str, JsonValue]`` or through the ``TOut`` type parameter of
:class:`AgentResult`. That parameterisation is the seam in the type system.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

RunId = Annotated[str, StringConstraints(pattern=r"^run_[0-9A-HJKMNP-TV-Z]{26}$")]  # ULID


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    integration: str                       # key of the integration that owns `subject`
    subject: dict[str, JsonValue]          # opaque to the harness; the integration parses it
    idempotency_key: str = Field(min_length=8, max_length=128)
    mode: Literal["live", "replay"] = "live"
    replay_fixture: str | None = None
    requested_by: str = "system"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str                       # "ev_" + 12 hex
    source: Literal["log", "diff", "config", "memory", "tool"]
    locator: str                           # opaque pointer into `source`; format owned by producer
    excerpt: str = Field(max_length=2000)
    sha256: str                            # of excerpt, normalized; used by the Evaluator


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: int = 0
    completion: int = 0
    thinking: int = 0
    total: int = 0
    estimated_cost_usd: float = 0.0


class AgentError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["invalid_output", "llm_timeout", "llm_rate_limited", "llm_auth",
                  "llm_upstream", "tool_error", "internal"]
    message: str                           # redacted before storage
    attempts: int
    detail: dict[str, JsonValue] = {}


TOut = TypeVar("TOut", bound=BaseModel)


# noqa on UP046: Appendix A.1 declares `TOut` as a module-level TypeVar and spells this
# `class AgentResult(BaseModel, Generic[TOut])`. PEP 695 type-parameter syntax would drop
# the named `TOut`, which `recovery.retry_structured` imports. Contract wins over style.
class AgentResult(BaseModel, Generic[TOut]):  # noqa: UP046
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: str
    status: Literal["ok", "invalid_output", "tool_error", "timeout", "escalate"]
    output: TOut | None
    confidence: float | None = Field(None, ge=0.0, le=1.0)   # post-calibration
    evidence: list[Evidence] = []
    attempts: int = 1
    latency_ms: int
    tokens: TokenUsage
    prompt_sha256: str | None = None
    model: str | None = None
    error: AgentError | None = None


class StageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str                             # integration-defined stage key
    agent: str | None
    status: str
    started_at: datetime
    duration_ms: int
    attempts: int
    tokens: TokenUsage
    summary: str                           # one-line, for the trace view


class EscalationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    escalation_id: str
    reason: Literal["low_confidence", "evidence_refuted", "invalid_output", "llm_timeout",
                    "llm_upstream", "config_error", "policy_denied", "tool_failure",
                    "cold_start_restricted", "rate_limited", "unknown_category",
                    "run_timeout"]
    message: str
    payload: dict[str, JsonValue]
    channels: list[Literal["log", "db", "webhook"]]
    delivered_at: datetime | None
    # Phase 4 amendment, additive: Appendix B.4 says a failed webhook delivery is
    # "recorded in `escalation.delivery_error`" and the `escalation` table has had the
    # column since Phase 3; the contract lacked the field. `None` when delivery succeeded
    # or no outbound channel is configured. Never carries the webhook URL.
    delivery_error: str | None = None


class RunOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: RunId
    integration: str
    status: Literal["completed", "escalated", "awaiting_approval", "failed",
                    "deduplicated", "in_progress"]
    original_run_id: RunId | None = None       # set only when status == "deduplicated"
    created_at: datetime
    completed_at: datetime | None
    duration_ms: int | None
    stages: list[StageRecord]
    degraded_components: list[str] = []
    total_tokens: TokenUsage
    final: dict[str, JsonValue]                # integration payload; opaque to the harness
    escalation: EscalationRecord | None = None
    trace_url: str
