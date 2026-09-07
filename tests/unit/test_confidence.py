"""`calibrate()`'s four-step contract, tested against the contract's own worked example.

The Phase 0 handoff called for exactly this: "two unit tests fall straight out of the
docstring; write both." They are `test_sums_first_then_clamps_once` (0.95 / +0.05 / -0.15
-> 0.85, and 0.84 is wrong) and `test_unregistered_signal_is_loud_not_silent`.
"""

from __future__ import annotations

import pytest

from src.harness.confidence import (
    CONFIDENCE_CEILING,
    CONFIDENCE_FLOOR,
    UNREGISTERED_SIGNAL_DELTA,
    UNREGISTERED_SIGNAL_REASON,
    ConfidenceModel,
    calibrate,
)


def test_sums_first_then_clamps_once() -> None:
    """Step 4's worked example. Clamping per step would give 0.84, and 0.84 is wrong."""
    score, adjustments = calibrate(
        0.95,
        {
            "evidence_fully_verified": "all citations verified",
            "evidence_refuted": "one citation refuted",
        },
        ConfidenceModel(),
    )

    assert score == pytest.approx(0.85)
    assert score != pytest.approx(0.84)
    assert [adjustment.delta for adjustment in adjustments] == [0.05, -0.15]


def test_unregistered_signal_is_loud_not_silent() -> None:
    """Step 3: an unregistered signal still emits a row, carrying the sentinel reason."""
    score, adjustments = calibrate(
        0.80,
        {"empty_diff_contradiction": "caller's reason, which must be replaced"},
        ConfidenceModel(),  # the default table does NOT carry the integration's row
    )

    assert len(adjustments) == 1
    assert adjustments[0].name == "empty_diff_contradiction"
    assert adjustments[0].delta == UNREGISTERED_SIGNAL_DELTA
    assert adjustments[0].reason == UNREGISTERED_SIGNAL_REASON
    assert score == pytest.approx(0.80), "an unregistered row must not invent a penalty"


def test_registering_the_row_makes_it_bite() -> None:
    model = ConfidenceModel(deltas={"empty_diff_contradiction": -0.10})
    score, adjustments = calibrate(0.80, {"empty_diff_contradiction": "empty diff"}, model)

    assert score == pytest.approx(0.70)
    assert adjustments[0].reason == "empty diff"


def test_every_signal_produces_exactly_one_adjustment() -> None:
    """Step 1: no filtering, no silent skip, and the order is the signals' order."""
    signals = {
        "no_citations": "a",
        "not_a_real_signal": "b",
        "cold_start": "c",
        "gateway_degraded": "d",
    }
    _, adjustments = calibrate(0.9, signals, ConfidenceModel())

    assert len(adjustments) == len(signals)
    assert [adjustment.name for adjustment in adjustments] == list(signals)


def test_clamped_to_the_ceiling_never_reaches_one() -> None:
    score, _ = calibrate(
        0.99, {"memory_agreement": "priors agree"}, ConfidenceModel()
    )
    assert score == pytest.approx(CONFIDENCE_CEILING)
    assert score < 1.0


def test_clamped_to_the_floor() -> None:
    score, _ = calibrate(
        0.05,
        {"evidence_refuted": "refuted", "no_citations": "none", "gateway_degraded": "x"},
        ConfidenceModel(),
    )
    assert score == pytest.approx(CONFIDENCE_FLOOR)


def test_no_signals_is_the_identity() -> None:
    score, adjustments = calibrate(0.77, {}, ConfidenceModel())
    assert score == pytest.approx(0.77)
    assert adjustments == []


def test_a_running_total_may_leave_the_bounds_and_come_back() -> None:
    """The exact case step 4 forbids clamping away: over the ceiling, then back under."""
    model = ConfidenceModel(deltas={"way_up": 0.50, "way_down": -0.40})
    score, _ = calibrate(0.90, {"way_up": "up", "way_down": "down"}, model)

    # Sum is 1.00, which clamps to the 0.99 ceiling. Clamping the intermediate 1.40 first
    # would give 0.99 - 0.40 = 0.59.
    assert score == pytest.approx(0.99)
