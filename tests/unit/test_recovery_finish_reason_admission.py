"""Phase 1 re-audit finding 2 and final-audit finding 1 (`src/harness/recovery.py`).
`_TERMINAL_FINISH_REASONS` holds three provider values (`SAFETY`, `RECITATION`,
`BLOCKLIST`) plus the empty-response sentinel, and the agent read the pinned SDK's full
18-member `FinishReason` enum to rule the rest *out* by name rather than leaving them as
an accident of omission.

`PROHIBITED_CONTENT` and `SPII` were briefly in that terminal set and are now deliberately
out, which is why they sit in `NON_TERMINAL_REASONS` below with the rest of the
exclusions. `finish_reason` is read off `candidates[0]` -- it says why *generation*
stopped, so a content refusal there is a verdict on the sample, not on the prompt; a
prompt-level block arrives as empty candidates instead and is covered by
`NO_CANDIDATE_FINISH_REASON`. This test file is the thing that would catch them drifting
back in.

Two halves, both load-bearing:

1. Every terminal reason ends the loop on the *first* call (deterministic refusal: a
   retry cannot change a verdict on the content already sent).
2. Every value the widening explicitly declined to admit is still retried in full
   (`STRUCTURED_MAX_ATTEMPTS` attempts) -- this is the guard against a future widening
   silently making a re-samplable condition terminal. `STRUCTURED_MAX_ATTEMPTS` is
   imported from `src.harness.recovery` rather than hardcoded, so this file tracks the
   real bound instead of asserting a number that could drift out from under it.

No network, no provider quota: `call` returns a fabricated `RawLlmResponse` directly, the
same fixture shape `test_recovery_terminal_finish_reasons.py` (finding 9) already uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from src.harness.contracts import TokenUsage
from src.harness.llm import NO_CANDIDATE_FINISH_REASON, UNSTATED_FINISH_REASON, RawLlmResponse
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.harness.recovery import STRUCTURED_MAX_ATTEMPTS, RetryPolicy, retry_structured


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str


@pytest.fixture
def recorder() -> TraceRecorder:
    # Unbound (no run_id): span() is a documented no-op here, so no tmp_db_path is
    # needed and nothing here can touch ./data/harness.db.
    return TraceRecorder(db_path=Path("unused.db"), redactor=Redactor(SecretRegistry(), ()))


# ---------------------------------------------------------------------------
# Terminal: single call, regardless of which of the four admitted reasons it is.
# ---------------------------------------------------------------------------

TERMINAL_REASONS = [
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    NO_CANDIDATE_FINISH_REASON,  # "NO_CANDIDATES" -- the empty-response sentinel
]


@pytest.mark.parametrize("finish_reason", TERMINAL_REASONS)
async def test_terminal_finish_reasons_end_the_loop_on_the_first_attempt(
    finish_reason: str, recorder: TraceRecorder
) -> None:
    calls = 0

    async def call(prompt: str) -> RawLlmResponse:
        nonlocal calls
        calls += 1
        return RawLlmResponse(
            text="",
            tokens=TokenUsage(),
            finish_reason=finish_reason,
            model="stub-model",
            latency_ms=1,
        )

    output, attempts, error = await retry_structured(
        call, "prompt", _Out, RetryPolicy(), recorder
    )

    assert calls == 1
    assert output is None
    assert len(attempts) == 1
    assert attempts[0].outcome == "fatal"
    assert error is not None
    assert error.kind == "invalid_output"
    assert error.detail == {"finish_reason": finish_reason}


# ---------------------------------------------------------------------------
# Non-terminal: the exclusions. Every one of these must still spend the full
# structured-retry budget rather than failing fast.
# ---------------------------------------------------------------------------

NON_TERMINAL_REASONS = [
    "PROHIBITED_CONTENT",  # candidate-level refusal: a re-sample can quote elsewhere
    "SPII",                # ditto -- not a verdict on the prompt (final-audit finding 1)
    "MALFORMED_FUNCTION_CALL",
    "IMAGE_SAFETY",
    "LANGUAGE",
    "OTHER",
    UNSTATED_FINISH_REASON,  # "UNKNOWN" -- a candidate that stated no reason at all
]


@pytest.mark.parametrize("finish_reason", NON_TERMINAL_REASONS)
async def test_excluded_finish_reasons_are_retried_to_the_structured_budget(
    finish_reason: str, recorder: TraceRecorder
) -> None:
    calls = 0

    async def call(prompt: str) -> RawLlmResponse:
        nonlocal calls
        calls += 1
        return RawLlmResponse(
            text="not valid json",  # guarantees a validation_error outcome, not "ok"
            tokens=TokenUsage(),
            finish_reason=finish_reason,
            model="stub-model",
            latency_ms=1,
        )

    output, attempts, error = await retry_structured(
        call, "prompt", _Out, RetryPolicy(), recorder
    )

    assert calls == STRUCTURED_MAX_ATTEMPTS
    assert output is None
    assert len(attempts) == STRUCTURED_MAX_ATTEMPTS
    assert all(attempt.outcome == "validation_error" for attempt in attempts)
    assert error is not None
    assert error.kind == "invalid_output"
    # The terminal branch's AgentError carries `{"finish_reason": ...}`; the exhausted
    # validation-retry branch carries `{"last_error": ...}` instead. Pinning the
    # *shape* here is what keeps a future widening honest: if `finish_reason` starts
    # showing up in `detail` for one of these values, that value quietly became
    # terminal without anyone updating this test's parametrize list.
    assert "last_error" in error.detail
    assert "finish_reason" not in error.detail
