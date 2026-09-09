"""`review-2.md` finding 1, the harness-core half of the urgent fix bundle: a cumulative
per-call delay budget in `retry_structured` (`RETRY_DELAY_BUDGET_S`), on top of the
per-sleep clamp already lowered to `MAX_RETRY_AFTER_S = 20.0` in `llm.py`.

Before this bundle, a provider that stated a long delay on every refusal was honoured
without limit: up to `TRANSIENT_MAX_ATTEMPTS - 1 = 3` sleeps per `retry_structured` call,
each up to the per-sleep ceiling. Combined with the `Investigator.run` `status="ok"`
hardcode this bundle also fixes (see
`tests/integration/test_investigator_upstream_failure_ends_run.py`), a fully
rate-limited run used to be able to hold a connection for minutes.

Two kinds of test here, deliberately both present:

- Fast, deterministic ones with `asyncio.sleep` patched to a recorder, pinning the exact
  arithmetic at the boundary (budget spent exactly at the limit vs. one sleep over -- the
  loop returns *before* sleeping in the latter case, which is what makes the budget a true
  ceiling rather than an overshoot-by-one).
- One genuine wall-clock test with `asyncio.sleep` left real, against the actual shipped
  constants (`RETRY_DELAY_BUDGET_S = 20.0`, `MAX_RETRY_AFTER_S = 20.0`), because a mocked
  sleep can prove the arithmetic but cannot prove the loop actually stops waiting in real
  time -- which is the entire point of a "run terminates inside the new budget" claim.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.harness import recovery as recovery_module
from src.harness.llm import MAX_RETRY_AFTER_S, LlmRateLimited
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.harness.recovery import RETRY_DELAY_BUDGET_S, RetryPolicy, retry_structured
from src.integrations.cicd.schemas import Diagnosis


@pytest.fixture
def recorder() -> TraceRecorder:
    return TraceRecorder(db_path=Path("unused.db"), redactor=Redactor(SecretRegistry(), ()))


async def _always_rate_limited(delay_s: float, prompt: str) -> None:
    raise LlmRateLimited("provider rate limited the request", retry_after_s=delay_s)


# --- fast, deterministic arithmetic on the boundary ------------------------------------


async def test_a_delay_that_exactly_fits_the_remaining_budget_is_still_slept(
    recorder: TraceRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`total_delay_s + delay > RETRY_DELAY_BUDGET_S` is a strict `>`: a delay that would
    land the running total exactly on the budget is honoured, not treated as over.
    """
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(recovery_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(recovery_module, "RETRY_DELAY_BUDGET_S", 10.0)

    async def call(prompt: str) -> None:
        await _always_rate_limited(10.0, prompt)

    output, attempts, error = await retry_structured(
        call, "prompt", Diagnosis, RetryPolicy(), recorder
    )

    assert output is None
    assert error is not None
    # One sleep taken (0.0 + 10.0 == 10.0, not > 10.0); the second attempt's delay would
    # make total 20.0 > 10.0, so the loop ends there instead of sleeping again.
    assert slept == [10.0]
    assert len(attempts) == 2


async def test_a_delay_that_would_land_one_over_the_budget_ends_the_loop_without_sleeping(
    recorder: TraceRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror case: a delay that would push the running total even fractionally past
    the budget is not honoured at all -- the loop ends on the spot, before the
    `asyncio.sleep` call, rather than sleeping and exceeding the ceiling by one delay.
    """
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(recovery_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(recovery_module, "RETRY_DELAY_BUDGET_S", 10.0)

    async def call(prompt: str) -> None:
        await _always_rate_limited(10.0001, prompt)

    output, attempts, error = await retry_structured(
        call, "prompt", Diagnosis, RetryPolicy(), recorder
    )

    assert output is None
    assert error is not None
    assert error.kind == "llm_rate_limited"
    # Zero sleeps: the very first delay already exceeds the budget, so the loop is
    # exhausted on attempt 1 without ever calling `asyncio.sleep`.
    assert slept == []
    assert len(attempts) == 1


async def test_the_budget_ends_the_loop_on_the_same_error_kind_the_attempt_budget_would(
    recorder: TraceRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spending the delay budget does not invent a new outcome: the terminal
    `AgentError.kind` is whatever the exception's own `agent_error_kind` is, exactly as
    if the attempt budget (not the delay budget) had ended the loop -- so the escalation
    path downstream (`Orchestrator._OUTCOME_FOR_ERROR_KIND`) is unchanged either way.
    """
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(recovery_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(recovery_module, "RETRY_DELAY_BUDGET_S", 1.0)

    async def call(prompt: str) -> None:
        await _always_rate_limited(5.0, prompt)

    output, attempts, error = await retry_structured(
        call, "prompt", Diagnosis, RetryPolicy(), recorder
    )

    assert output is None
    assert error is not None
    assert error.kind == "llm_rate_limited"
    assert len(attempts) == 1
    assert attempts[0].outcome == "transient"


# --- the real wall clock, against the actual shipped constants -------------------------


async def test_a_fully_rate_limited_call_terminates_inside_the_real_delay_budget() -> None:
    """No mocked sleep, no monkeypatched constants: the provider states exactly
    `MAX_RETRY_AFTER_S` (20.0s, the per-sleep ceiling a real clamp would already have
    produced) on every attempt, against the real `RETRY_DELAY_BUDGET_S` (20.0s).

    Worked out from the source: attempt 1's delay (20.0) exactly fits the empty budget
    (20.0 is not > 20.0), so it sleeps once, for real, ~20s. Attempt 2's delay would bring
    the running total to 40.0 > 20.0, so the loop ends there *without* sleeping again --
    the budget is a true ceiling, not an overshoot-by-one. Total real wall time is
    therefore bounded by one sleep (~20s), not two (~40s) or three (~60s), which is
    exactly the difference this fix makes to a request a human is waiting on.
    """
    assert MAX_RETRY_AFTER_S == 20.0, "the recipe below assumes today's shipped value"
    assert RETRY_DELAY_BUDGET_S == 20.0, "the recipe below assumes today's shipped value"

    recorder = TraceRecorder(db_path=Path("unused.db"), redactor=Redactor(SecretRegistry(), ()))

    async def call(prompt: str) -> None:
        await _always_rate_limited(MAX_RETRY_AFTER_S, prompt)

    started = time.monotonic()
    output, attempts, error = await retry_structured(
        call, "prompt", Diagnosis, RetryPolicy(), recorder
    )
    elapsed_s = time.monotonic() - started

    assert output is None
    assert error is not None
    assert error.kind == "llm_rate_limited"
    assert len(attempts) == 2, "one sleep, then the budget ends the loop on the next attempt"

    # One real sleep of ~20s happened (not zero -- the budget must not be so eager it
    # refuses even the first, exactly-fitting delay).
    assert elapsed_s >= MAX_RETRY_AFTER_S - 1.0, (
        f"elapsed {elapsed_s:.1f}s is too short for even one honoured {MAX_RETRY_AFTER_S}s "
        "sleep to have happened -- the budget is refusing a delay that exactly fits it"
    )
    # And only one -- a second ~20s sleep would put this well past 30s, and three (the
    # pre-fix worst case for one call) would put it past 55s.
    assert elapsed_s < 2 * MAX_RETRY_AFTER_S - 5.0, (
        f"elapsed {elapsed_s:.1f}s looks like more than one delay was honoured -- the "
        "per-run delay budget is not bounding the wall clock the way it should"
    )
