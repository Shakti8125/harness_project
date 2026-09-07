"""Deterministic calibration of a self-reported confidence score.

DERIVED, NOT TRANSCRIBED. Appendix A does not specify this module. Its members are
named by the repository layout (``ConfidenceModel``, ``Adjustment``, ``calibrate()``)
and its behaviour is fixed by PLAN.md "How ``final_confidence`` is derived":

    final_confidence = clamp(self_confidence + sum(deltas), 0.0, 0.99)

The 0.99 ceiling is deliberate -- nothing is certain, and a hard 1.0 invites
``confidence == 1.0`` special-casing downstream. Every applied delta is recorded as an
:class:`Adjustment`, whose shape (``name``, ``delta``, ``reason``) is copied from
Appendix A.11 so the integration and the harness agree on one model.

The adjustment *deltas* are data, so they live in :class:`ConfidenceModel` and can be
overridden. The adjustment *conditions* are not modelled here at all: deciding which
signals fired requires reading domain artifacts, so the caller passes in the names of
the adjustments that apply. Six of the seven rows of PLAN.md's adjustment table are
domain-neutral and appear in :data:`DEFAULT_ADJUSTMENT_DELTAS`; the seventh, whose
condition names a specific artifact type of the first integration, is deliberately
left for that integration to add to ``ConfidenceModel.deltas`` (delta -0.10).

That split is only safe while omitting a row is *loud*. If the integration never
registers its row, the penalty must not quietly evaporate into an unchanged score:
:func:`calibrate` still emits an :class:`Adjustment` for the signal, carrying
:data:`UNREGISTERED_SIGNAL_DELTA` and :data:`UNREGISTERED_SIGNAL_REASON`, so the
wiring omission reaches the trace instead of the operator's blind spot. See step 3 of
:func:`calibrate`'s contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict

CONFIDENCE_FLOOR: Final[float] = 0.0
CONFIDENCE_CEILING: Final[float] = 0.99

#: PLAN.md adjustment table, domain-neutral rows. Name -> delta.
DEFAULT_ADJUSTMENT_DELTAS: Final[Mapping[str, float]] = MappingProxyType(
    {
        # >= PRIOR_MIN_OCCURRENCES priors for this signature and >= 60% share the verdict
        "memory_agreement": 0.10,
        # Evaluator: all citations verified
        "evidence_fully_verified": 0.05,
        # Evaluator: any citation refuted
        "evidence_refuted": -0.15,
        # the model cited nothing
        "no_citations": -0.10,
        # no baseline existed for comparison
        "cold_start": -0.05,
        # a required read tool returned an error
        "gateway_degraded": -0.10,
    }
)

#: Delta recorded for a signalled adjustment that has no row in ``ConfidenceModel.deltas``.
#: Zero, because inventing a penalty the table never authorised would be worse than the
#: omission; the visibility of the recorded row is what carries the failure.
UNREGISTERED_SIGNAL_DELTA: Final[float] = 0.0

#: Reason recorded alongside :data:`UNREGISTERED_SIGNAL_DELTA`. A fixed sentinel, so the
#: trace reader and the eval harness can detect the omission by string equality rather
#: than by matching prose written elsewhere.
UNREGISTERED_SIGNAL_REASON: Final[str] = "no delta registered for this signal"


class Adjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    delta: float
    reason: str


class ConfidenceModel(BaseModel):
    """The delta table and clamp bounds applied to a self-reported score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deltas: dict[str, float] = dict(DEFAULT_ADJUSTMENT_DELTAS)
    floor: float = CONFIDENCE_FLOOR
    ceiling: float = CONFIDENCE_CEILING


def calibrate(
    self_confidence: float,
    signals: Mapping[str, str],
    model: ConfidenceModel,
) -> tuple[float, list[Adjustment]]:
    """Sum the deltas of every signalled adjustment, then clamp the total exactly once.

    `signals` maps the name of each adjustment that fired to the human-readable reason
    it fired, which is carried through into the returned :class:`Adjustment` list and
    written to the trace. The model never sees the adjusted number.

    The contract, in the order it must be implemented:

    1. **Every name in ``signals`` produces exactly one :class:`Adjustment`**, in the
       iteration order of ``signals``. There is no filtering step and no silent skip:
       ``len(adjustments) == len(signals)`` holds for every input.
    2. A name **present** in ``model.deltas`` produces
       ``Adjustment(name=name, delta=model.deltas[name], reason=signals[name])``.
    3. A name **absent** from ``model.deltas`` is a wiring fault and must be loud. It
       produces ``Adjustment(name=name, delta=UNREGISTERED_SIGNAL_DELTA,
       reason=UNREGISTERED_SIGNAL_REASON)`` -- the sentinel reason replaces the caller's,
       so an unregistered row is visible in the trace and detectable by string equality.
       It is never dropped and never raised: a signal name is runtime data supplied by
       the caller, so an unregistered one is reported through the return value, not by
       aborting a live run.
    4. **Sum first, then clamp once.** Add the ``delta`` of every adjustment from steps 2
       and 3 to ``self_confidence`` to form a single running total, and apply
       ``min(max(total, model.floor), model.ceiling)`` to that total once, as the final
       operation. Intermediate totals are never clamped, never rounded and never
       inspected; a running total may legitimately exceed ``model.ceiling`` or fall below
       ``model.floor`` and come back inside the bounds before the end. This is PLAN.md's
       ``clamp(self_confidence + sum(deltas), floor, ceiling)`` and nothing else. Worked
       example that separates the two readings: ``self_confidence=0.95`` with deltas
       ``+0.05`` and ``-0.15`` sums to ``0.85`` and clamps to ``0.85``; clamping per step
       would yield ``0.84``, and ``0.84`` is wrong.

    Returns the clamped score first, then the adjustments that produced it.
    """
    adjustments = [
        Adjustment(name=name, delta=model.deltas[name], reason=reason)
        if name in model.deltas
        # Step 3: the sentinel reason replaces the caller's, so an unregistered row is
        # detectable by string equality rather than by matching prose written elsewhere.
        else Adjustment(
            name=name,
            delta=UNREGISTERED_SIGNAL_DELTA,
            reason=UNREGISTERED_SIGNAL_REASON,
        )
        for name, reason in signals.items()
    ]
    # Step 4: one running total, one clamp, applied last. Summing into a local and
    # clamping the sum is the whole difference between 0.85 and the wrong 0.84 --
    # writing this as a fold that clamps each intermediate would pass the naive test
    # and fail the worked example in the contract above.
    total = self_confidence + sum(adjustment.delta for adjustment in adjustments)
    return min(max(total, model.floor), model.ceiling), adjustments
