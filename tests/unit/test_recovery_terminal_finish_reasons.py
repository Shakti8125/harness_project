"""review.md finding 9: the "empty candidates" half of Appendix B.1's safety row.

Before the fix, `GeminiClient.generate` wrote a single sentinel (`"UNKNOWN"`) for both
"no candidate came back at all" and "a candidate came back but stated no reason", and
`_TERMINAL_FINISH_REASONS` did not contain it either way -- so a genuinely empty response
(deterministic, per B.1: "no candidates -> no retry") burned the full 3-attempt structured
budget instead of failing on the first call.

The fix splits the sentinel in two (`NO_CANDIDATE_FINISH_REASON` / `UNSTATED_FINISH_REASON`)
and makes only the first terminal. Both halves are pinned here: the terminal path (this is
the actual regression fix) and the non-terminal path (this is the guard against
over-broadening -- a candidate that merely omitted the field must not be treated as a
deterministic refusal).

No network, no provider quota: `call` returns a fabricated `RawLlmResponse` directly, the
same shape `GeminiClient.generate` would hand `retry_structured` (see `llm.py:636-651`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from src.harness.contracts import TokenUsage
from src.harness.llm import NO_CANDIDATE_FINISH_REASON, UNSTATED_FINISH_REASON, RawLlmResponse
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.harness.recovery import RetryPolicy, retry_structured


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str


@pytest.fixture
def recorder() -> TraceRecorder:
    # Unbound (no run_id): `span()` is a documented no-op that never touches a database,
    # so this needs no tmp_db_path and cannot write to ./data/harness.db.
    return TraceRecorder(db_path=Path("unused.db"), redactor=Redactor(SecretRegistry(), ()))


async def test_no_candidates_is_terminal_on_the_first_attempt(
    recorder: TraceRecorder,
) -> None:
    """The regression fix: an empty response ends the loop in one call, not three."""
    calls = 0

    async def call(prompt: str) -> RawLlmResponse:
        nonlocal calls
        calls += 1
        return RawLlmResponse(
            text="",
            tokens=TokenUsage(),
            finish_reason=NO_CANDIDATE_FINISH_REASON,
            model="stub-model",
            latency_ms=1,
        )

    output, attempts, error = await retry_structured(
        call, "prompt", _Out, RetryPolicy(), recorder
    )

    assert calls == 1
    assert output is None
    assert error is not None
    assert error.kind == "invalid_output"
    assert error.detail == {"finish_reason": "NO_CANDIDATES"}
    assert len(attempts) == 1
    assert attempts[0].outcome == "fatal"


async def test_unstated_finish_reason_is_not_terminal(recorder: TraceRecorder) -> None:
    """The guard against over-broadening: a candidate that came back but stated no
    finish reason is not a deterministic refusal, and must still spend the ordinary
    structured-retry budget rather than failing fast on the first attempt.
    """
    calls = 0

    async def call(prompt: str) -> RawLlmResponse:
        nonlocal calls
        calls += 1
        return RawLlmResponse(
            text="not valid json",
            tokens=TokenUsage(),
            finish_reason=UNSTATED_FINISH_REASON,
            model="stub-model",
            latency_ms=1,
        )

    output, attempts, error = await retry_structured(
        call, "prompt", _Out, RetryPolicy(), recorder
    )

    assert calls == 3  # STRUCTURED_MAX_ATTEMPTS, unchanged -- still retried in full
    assert output is None
    assert error is not None
    assert error.kind == "invalid_output"
    assert all(attempt.outcome == "validation_error" for attempt in attempts)
