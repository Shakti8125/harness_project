"""Wave-3 audit finding 2, the formerly-dead branch in `retry_structured` itself.

`classify_provider_error` extracting a delay is necessary but not sufficient: this pins
that `retry_structured` actually *sleeps* that many seconds rather than falling back to a
jittered draw. Before the fix, `LlmRateLimited.retry_after_s` was hardcoded `None`, so
every one of these sleeps came from `backoff_delay` -- a uniform draw from `[0, ceiling]`
that totals under 4 seconds across the whole 4-attempt transient budget. After the fix,
a provider that states a delay is honoured on every attempt, up to the per-run delay
budget (`RETRY_DELAY_BUDGET_S`, `review-2.md` finding 1 -- see
`tests/unit/test_retry_delay_budget.py` for that ceiling itself). The delay chosen below
(6.0s) is deliberately small enough that three honoured sleeps (18.0s) stay inside that
20.0s budget, so this file keeps testing exactly what it says it tests -- "honoured
verbatim every time" -- without the budget fix (a later, equally deliberate change)
truncating the sequence after one sleep and turning this into a test of the budget
instead.

No network, no provider quota: `call` raises the classified exception directly (this is
exactly the exception `GeminiClient.generate` would raise, per `llm.py:569`,
`raise classify_provider_error(exc) from exc`), and `asyncio.sleep` is patched with a
recorder instead of actually sleeping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.harness import recovery as recovery_module
from src.harness.llm import LlmRateLimited
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.harness.recovery import RetryPolicy, retry_structured
from src.integrations.cicd.schemas import Diagnosis


@pytest.fixture
def recorder() -> TraceRecorder:
    # Unbound (no run_id): `span()` is a documented no-op that never touches a database,
    # so this needs no tmp_db_path and cannot write to ./data/harness.db.
    return TraceRecorder(db_path=Path("unused.db"), redactor=Redactor(SecretRegistry(), ()))


async def test_a_429_with_a_stated_retry_delay_is_slept_verbatim_every_time(
    recorder: TraceRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(recovery_module.asyncio, "sleep", fake_sleep)

    async def call(prompt: str) -> None:
        raise LlmRateLimited("provider rate limited the request", retry_after_s=6.0)

    policy = RetryPolicy()  # transient_max_attempts defaults to 4
    output, attempts, error = await retry_structured(
        call, "prompt", Diagnosis, policy, recorder
    )

    assert output is None
    assert error is not None
    assert error.kind == "llm_rate_limited"
    assert len(attempts) == 4
    # Three sleeps between four attempts, each honouring the provider's stated delay
    # rather than a jittered draw from [0, 0.5], [0, 1], [0, 2] (which would sum well
    # under 4 seconds and could never equal 6.0 three times running). 18.0s total stays
    # inside the 20.0s per-run delay budget, so the attempt budget -- not the delay
    # budget -- is what ends this loop; see the module docstring.
    assert slept == [6.0, 6.0, 6.0]


async def test_without_a_stated_delay_it_falls_back_to_jittered_backoff(
    recorder: TraceRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control case: `retry_after_s=None` must still take the old path."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(recovery_module.asyncio, "sleep", fake_sleep)
    # Full jitter is randomised; pin it so this test is deterministic without weakening
    # what it checks (the delay must NOT be 41.0 -- it must come from backoff_delay).
    monkeypatch.setattr(recovery_module.random, "uniform", lambda _lo, hi: hi)

    async def call(prompt: str) -> None:
        raise LlmRateLimited("provider rate limited the request", retry_after_s=None)

    policy = RetryPolicy()
    await retry_structured(call, "prompt", Diagnosis, policy, recorder)

    assert slept == [
        recovery_module.BACKOFF_BASE_S,
        recovery_module.BACKOFF_BASE_S * 2,
        recovery_module.BACKOFF_BASE_S * 4,
    ]
