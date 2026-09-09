"""Retry and backoff for structured generation.

Frozen transcription of PLAN.md Appendix A.8, plus the Phase 1 implementation of the
loop it describes. The loop distinguishes a schema violation (retry with a repair
instruction) from a transient upstream failure (retry with exponential backoff and full
jitter) from an oversized context (retry with a smaller budget). It returns the parsed
model or an :class:`AgentError` -- it never raises for an upstream failure.

Attempt counts and backoff bounds mirror PLAN.md "Concrete numbers in one place"; the
per-condition behaviour is Appendix B.1, row for row.

**Two adaptations of B.1 to A.8's frozen signature, recorded rather than discovered.**
``call`` takes a prompt and nothing else, so the only knob this loop can turn between
attempts is the prompt text:

* ``finish_reason == "MAX_TOKENS"`` is specified as "retry with ``max_output_tokens x
  1.5``". The output token budget belongs to the ``LlmRequest`` the caller closed over
  and is not reachable from here, so the same condition is instead treated as a
  validation failure and retried with an explicit instruction to answer more briefly.
  Same budget, same number of attempts, same terminal ``invalid_output``.
* "400 request too large -> re-assemble context at ``budget x 0.5``" would require the
  ``ContextManager`` and the original sections, neither of which this function has.
  It halves the *prompt* instead, keeping the head and the tail, which is where a
  re-assembly at half budget would also have landed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from src.harness.contracts import AgentError, TokenUsage, TOut
from src.harness.llm import (
    DEFAULT_REQUEST_TIMEOUT_S,
    LlmAuthError,
    LlmContextTooLarge,
    LlmRateLimited,
    LlmTimeout,
    LlmTransportError,
    RawLlmResponse,
)
from src.harness.observability import TraceRecorder

logger = logging.getLogger("harness.recovery")

STRUCTURED_MAX_ATTEMPTS: Final[int] = 3     # schema-violation retries, attempts in total
TRANSIENT_MAX_ATTEMPTS: Final[int] = 4      # rate-limit / upstream retries, attempts in total
BACKOFF_BASE_S: Final[float] = 0.5
BACKOFF_MAX_S: Final[float] = 8.0

#: Appendix B.1: a timeout gets "1 retry at same budget" -- two attempts in total.
TIMEOUT_MAX_ATTEMPTS: Final[int] = 2

#: Appendix B.1: an oversized request is retried once, at half the context.
TOO_LARGE_MAX_ATTEMPTS: Final[int] = 2

#: Ceiling, in seconds, on the *total* time one :func:`retry_structured` call may spend
#: asleep between attempts -- provider-stated delays and jittered backoff alike.
#:
#: Why a second bound exists at all: :data:`src.harness.llm.MAX_RETRY_AFTER_S` clamps one
#: sleep, and a clamp on one sleep says nothing about how many there are. With four
#: transient attempts, a provider that states a long delay on every refusal buys three
#: sleeps at the per-sleep ceiling from a single call, and a caller that drives two agents
#: in sequence pays that twice. Nothing above this function bounds it: the loop is the last
#: place in the stack that knows how long it has already waited.
#:
#: The number, and the worst case it buys. 20 s per call, so a caller running two agents in
#: sequence sleeps at most **40 s in total**, whatever the provider states and however many
#: times it states it -- the sleep is taken only if it fits in what is left, so the budget
#: is a true ceiling and not an overshoot-by-one-sleep. 40 s is inside the shortest
#: end-to-end request duration measured as tolerated in front of this service (57 s, one
#: observation; its actual ceiling is unverified and is not something to design against),
#: and it leaves the remaining margin to the round trips themselves rather than spending it
#: on waiting. It is also large enough that the ordinary path never notices: full-jitter
#: backoff over a spent transient budget draws from [0, 0.5], [0, 1] and [0, 2], at most
#: 3.5 s, so this bound bites only when a provider is stating long delays -- which is
#: exactly the case where retrying sooner is worth less than answering the caller.
#:
#: When it is spent the loop does not invent a new outcome: it stops honouring the stated
#: delay and ends on the same :class:`AgentError` the attempt budget would have produced,
#: so the escalation path is unchanged.
RETRY_DELAY_BUDGET_S: Final[float] = 20.0

#: Span attribute set when the prompt was halved after an oversized-request failure.
ATTR_CONTEXT_DOWNSHIFT: Final[str] = "context_downshift"

#: Provider finish reasons that are terminal: no retry can change them.
_TERMINAL_FINISH_REASONS: Final[frozenset[str]] = frozenset({"SAFETY", "RECITATION", "BLOCKLIST"})


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


def backoff_delay(attempt: int, policy: RetryPolicy) -> float:
    """Exponential backoff for the ``attempt``-th transient failure (1-based).

    Full jitter -- a uniform draw from ``[0, computed]``, not ``computed`` plus a wobble.
    The point is to break up a thundering herd of retries that all began together, and
    only the uniform form actually spreads them.
    """
    ceiling = min(policy.backoff_max_s, policy.backoff_base_s * (2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling) if policy.jitter == "full" else ceiling  # noqa: S311


def extract_json(text: str) -> str:
    """Strip a markdown code fence, if the model wrapped its JSON in one.

    Cheap and worth it: a fenced body is otherwise a guaranteed decode failure that burns
    a whole attempt on a formatting habit rather than on a content problem.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped[3:]
    if body.lower().startswith("json"):
        body = body[4:]
    closing = body.rfind("```")
    return (body[:closing] if closing != -1 else body).strip()


def halve_prompt(prompt: str) -> str:
    """Keep the first and last quarter of a prompt, marking the gap.

    Used only for the oversized-request path. Head and tail are kept for the same reason
    the context manager keeps them: the instructions are at the front and the proximate
    evidence is at the back.
    """
    quarter = len(prompt) // 4
    if quarter == 0:
        return prompt
    dropped = len(prompt) - 2 * quarter
    marker = f"… [{dropped:,} characters elided to fit the model] …"
    return f"{prompt[:quarter]}\n{marker}\n{prompt[-quarter:]}"


def _repair_instruction(error_summary: str) -> str:
    return (
        "\n\n---\nYour previous response was rejected. Fix exactly this and return the "
        "corrected JSON object, with no prose and no code fence:\n"
        f"{error_summary}\n"
    )


async def retry_structured(
    call: Callable[[str], Awaitable[RawLlmResponse]],
    prompt: str,
    schema: type[TOut],
    policy: RetryPolicy,
    recorder: TraceRecorder,
) -> tuple[TOut | None, list[AttemptRecord], AgentError | None]:
    """Call `call` until it yields text that validates as `schema`, or the policy is spent.

    Each attempt is a child span, per Appendix B.1. Returns the parsed model on success;
    on exhaustion returns ``(None, attempts, AgentError(...))`` whose ``kind`` names the
    condition that ended the loop. Never raises for a provider failure.
    """
    attempts: list[AttemptRecord] = []
    current_prompt = prompt
    total_delay_s = 0.0
    validation_failures = 0
    transient_failures = 0
    timeout_failures = 0
    too_large_failures = 0
    last_error: AgentError | None = None

    # A ceiling on the whole loop, independent of the per-condition budgets. Without it,
    # a provider alternating between two failure classes could keep both budgets alive
    # forever; each class's counter only ever increments on its own kind of failure.
    hard_ceiling = policy.max_attempts + policy.transient_max_attempts + TIMEOUT_MAX_ATTEMPTS

    while len(attempts) < hard_ceiling:
        attempt_number = len(attempts) + 1
        started = time.monotonic()
        async with recorder.span(
            "llm.attempt", "llm", attempt=attempt_number, schema=schema.__name__
        ) as span:
            try:
                response = await call(current_prompt)
            except LlmTransportError as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                span.set_error({"type": type(exc).__name__, "message": str(exc)})

                if isinstance(exc, LlmAuthError):
                    # Appendix B.1: no retry. A credential does not become valid by being
                    # asked again, and each attempt is another audit-log entry.
                    attempts.append(
                        AttemptRecord(
                            attempt=attempt_number, outcome="fatal",
                            error_summary=str(exc), latency_ms=latency_ms,
                            tokens=TokenUsage(),
                        )
                    )
                    return None, attempts, AgentError(
                        kind="llm_auth", message=str(exc), attempts=len(attempts)
                    )

                if isinstance(exc, LlmContextTooLarge):
                    too_large_failures += 1
                    attempts.append(
                        AttemptRecord(
                            attempt=attempt_number, outcome="too_large",
                            error_summary=str(exc), latency_ms=latency_ms,
                            tokens=TokenUsage(),
                        )
                    )
                    if (
                        not policy.downshift_context_on_too_large
                        or too_large_failures >= TOO_LARGE_MAX_ATTEMPTS
                    ):
                        return None, attempts, AgentError(
                            kind="invalid_output", message=str(exc), attempts=len(attempts)
                        )
                    current_prompt = halve_prompt(current_prompt)
                    span.set_attribute(ATTR_CONTEXT_DOWNSHIFT, True)
                    continue

                if isinstance(exc, LlmTimeout):
                    timeout_failures += 1
                    outcome: Literal["timeout", "transient"] = "timeout"
                    exhausted = timeout_failures >= TIMEOUT_MAX_ATTEMPTS
                else:
                    transient_failures += 1
                    outcome = "transient"
                    exhausted = transient_failures >= policy.transient_max_attempts

                # The delay is chosen *before* the attempt is recorded, because whether
                # it still fits in the delay budget is part of deciding whether this
                # attempt was the last one.
                delay = exc.retry_after_s
                if delay is None or isinstance(exc, LlmTimeout):
                    delay = backoff_delay(transient_failures or 1, policy)
                if isinstance(exc, LlmRateLimited) and exc.retry_after_s is not None:
                    delay = exc.retry_after_s
                if not exhausted and total_delay_s + delay > RETRY_DELAY_BUDGET_S:
                    # Budget spent. Stop honouring stated delays and end the loop exactly
                    # where the attempt budget ends it -- same error kind, same escalation.
                    # Waiting longer here does not make the answer better; it only makes
                    # the caller wait for the same answer.
                    logger.info(
                        "retry delay budget spent after %.1fs of %.1fs (next delay would "
                        "be %.1fs); ending the loop instead of sleeping",
                        total_delay_s, RETRY_DELAY_BUDGET_S, delay,
                    )
                    exhausted = True

                attempts.append(
                    AttemptRecord(
                        attempt=attempt_number, outcome=outcome,
                        error_summary=str(exc), latency_ms=latency_ms,
                        tokens=TokenUsage(),
                    )
                )
                if exhausted:
                    return None, attempts, AgentError(
                        kind=exc.agent_error_kind,  # type: ignore[arg-type]
                        message=str(exc),
                        attempts=len(attempts),
                    )
                total_delay_s += delay
                await asyncio.sleep(delay)
                continue

            latency_ms = response.latency_ms or int((time.monotonic() - started) * 1000)
            span.set_tokens(response.tokens)
            span.set_attribute("finish_reason", response.finish_reason)

            if response.finish_reason in _TERMINAL_FINISH_REASONS:
                # Deterministic refusal: the same prompt produces the same block.
                attempts.append(
                    AttemptRecord(
                        attempt=attempt_number, outcome="fatal",
                        error_summary=f"finish_reason={response.finish_reason}",
                        latency_ms=latency_ms, tokens=response.tokens,
                    )
                )
                return None, attempts, AgentError(
                    kind="invalid_output",
                    message="provider returned no usable candidate",
                    attempts=len(attempts),
                    detail={"finish_reason": response.finish_reason},
                )

            if response.finish_reason == "MAX_TOKENS":
                error_summary = (
                    "your previous response was cut off before it was complete; answer "
                    "the same question substantially more briefly"
                )
            else:
                try:
                    parsed = schema.model_validate_json(extract_json(response.text))
                except ValidationError as exc:
                    error_summary = json.dumps(exc.errors(include_url=False))[:2000]
                except ValueError as exc:
                    # `model_validate_json` raises this for a body that is not JSON at all.
                    error_summary = f"response was not valid JSON: {exc}"
                else:
                    attempts.append(
                        AttemptRecord(
                            attempt=attempt_number, outcome="ok", error_summary=None,
                            latency_ms=latency_ms, tokens=response.tokens,
                        )
                    )
                    return parsed, attempts, None

            validation_failures += 1
            span.set_error({"type": "invalid_output", "message": error_summary[:500]})
            attempts.append(
                AttemptRecord(
                    attempt=attempt_number, outcome="validation_error",
                    error_summary=error_summary, latency_ms=latency_ms,
                    tokens=response.tokens,
                )
            )
            last_error = AgentError(
                kind="invalid_output",
                message="model output did not satisfy the contract",
                attempts=len(attempts),
                detail={"last_error": error_summary},
            )
            if validation_failures >= policy.max_attempts:
                return None, attempts, last_error
            # Feed the failure back in, appended to the ORIGINAL prompt rather than to
            # the previous repaired one: stacking repair blocks across attempts drifts
            # the instruction further from the task with every round.
            current_prompt = prompt + _repair_instruction(error_summary)

    logger.warning("retry_structured: hard attempt ceiling reached")
    return None, attempts, last_error or AgentError(
        kind="internal", message="retry budget exhausted", attempts=len(attempts)
    )
