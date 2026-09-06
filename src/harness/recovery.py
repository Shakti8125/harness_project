"""Retry and backoff for structured generation.

Frozen transcription of PLAN.md Appendix A.8. The loop distinguishes a schema
violation (retry with a repair instruction) from a transient upstream failure (retry
with exponential backoff and full jitter) from an oversized context (retry with a
smaller budget). It returns the parsed model or an :class:`AgentError` -- it never
raises for an upstream failure.

Attempt counts and backoff bounds mirror PLAN.md "Concrete numbers in one place".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from src.harness.contracts import AgentError, TokenUsage, TOut
from src.harness.llm import DEFAULT_REQUEST_TIMEOUT_S, RawLlmResponse
from src.harness.observability import TraceRecorder

STRUCTURED_MAX_ATTEMPTS: Final[int] = 3     # schema-violation retries, attempts in total
TRANSIENT_MAX_ATTEMPTS: Final[int] = 4      # rate-limit / upstream retries, attempts in total
BACKOFF_BASE_S: Final[float] = 0.5
BACKOFF_MAX_S: Final[float] = 8.0


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = STRUCTURED_MAX_ATTEMPTS
    transient_max_attempts: int = TRANSIENT_MAX_ATTEMPTS
    backoff_base_s: float = BACKOFF_BASE_S
    backoff_max_s: float = BACKOFF_MAX_S
    jitter: Literal["full", "none"] = "full"
    timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    downshift_context_on_too_large: bool = True


class AttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: int
    outcome: Literal["ok", "validation_error", "transient", "too_large", "timeout", "fatal"]
    error_summary: str | None
    latency_ms: int
    tokens: TokenUsage


async def retry_structured(
    call: Callable[[str], Awaitable[RawLlmResponse]],
    prompt: str,
    schema: type[TOut],
    policy: RetryPolicy,
    recorder: TraceRecorder,
) -> tuple[TOut | None, list[AttemptRecord], AgentError | None]:
    """Call `call` until it yields text that validates as `schema`, or the policy is spent."""
    raise NotImplementedError
