"""Claim verification against the artifacts a run actually collected.

Frozen transcription of PLAN.md Appendix A.6, plus the Phase 4 implementation. The
evaluator owns the bookkeeping -- tally verdicts, aggregate them into a report, derive a
confidence delta -- and holds a registry of :class:`ClaimChecker` objects. The checkers
themselves, which are the only part that knows how to look something up in a domain
artifact, live in the integration and are injected.

Every check is deterministic -- PLAN.md: "no second LLM call, because using a model to
check a model just moves the credulity". The report is the same shape whatever the
checkers do, which is what lets the remediation gate and the confidence table read it
without knowing which claim kinds this integration has.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from src.harness.confidence import DEFAULT_ADJUSTMENT_DELTAS

logger = logging.getLogger("harness.evaluator")

# PLAN.md "Concrete numbers in one place": the report fails on any refuted claim, or
# when the verified share of all claims falls below this fraction.
MIN_VERIFIED_SHARE: Final[float] = 0.5

#: The `Adjustment.name`s of PLAN.md's two Evaluator rows. The integration's evaluate
#: agent signals one of these when it re-calibrates, and `confidence.py` prices it.
SIGNAL_FULLY_VERIFIED: Final[str] = "evidence_fully_verified"
SIGNAL_REFUTED: Final[str] = "evidence_refuted"

#: The same two rows as `confidence_delta` values, read off the one table that owns the
#: numbers so the report and the calibration cannot disagree about what a verdict cost.
DELTA_FULLY_VERIFIED: Final[float] = DEFAULT_ADJUSTMENT_DELTAS[SIGNAL_FULLY_VERIFIED]
DELTA_REFUTED: Final[float] = DEFAULT_ADJUSTMENT_DELTAS[SIGNAL_REFUTED]
DELTA_NONE: Final[float] = 0.0

#: `ClaimVerdict.detail` for a claim whose kind no registered checker handles. A wiring
#: fault, reported through the verdict rather than raised: the claim kinds are data the
#: model chose, and a run must not die on an unregistered one.
UNREGISTERED_KIND_DETAIL: Final[str] = "no checker registered for this claim kind"


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str
    kind: str                              # registered checker key
    payload: dict[str, JsonValue]


class ClaimVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str
    kind: str
    result: Literal["verified", "refuted", "unverifiable"]
    detail: str                            # e.g. "exact" | "fuzzy 0.94" | "no such locator"
    matched_locator: str | None = None


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdicts: list[ClaimVerdict]
    verified: int
    refuted: int
    unverifiable: int
    verdict: Literal["pass", "warn", "fail", "skipped"]
    confidence_delta: float
    reason: str


class ClaimChecker(Protocol):
    """Verifies one kind of claim against the run's artifacts. Supplied by the integration."""

    kind: str

    def check(self, claim: Claim, artifacts: Mapping[str, BaseModel]) -> ClaimVerdict: ...


class Evaluator:
    """Dispatches claims to registered checkers and aggregates their verdicts."""

    def __init__(self, checkers: Sequence[ClaimChecker]) -> None:
        """Index the checkers by kind. Two checkers for one kind is a wiring fault."""
        registry: dict[str, ClaimChecker] = {}
        for checker in checkers:
            if checker.kind in registry:
                raise ValueError(f"two claim checkers registered for kind {checker.kind!r}")
            registry[checker.kind] = checker
        self._checkers = registry

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(self._checkers)

    def check(self, claim: Claim, artifacts: Mapping[str, BaseModel]) -> ClaimVerdict:
        """One claim's verdict. An unregistered kind is `unverifiable`, never refuted."""
        checker = self._checkers.get(claim.kind)
        if checker is None:
            logger.warning("evaluator: no checker for claim kind %r", claim.kind)
            return ClaimVerdict(
                claim_id=claim.claim_id, kind=claim.kind, result="unverifiable",
                detail=UNREGISTERED_KIND_DETAIL,
            )
        return checker.check(claim, artifacts)

    def evaluate(
        self, claims: Sequence[Claim], artifacts: Mapping[str, BaseModel]
    ) -> EvaluationReport:
        """Check every claim, then apply PLAN.md's verdict rule over the tallies.

        `fail` on any `refuted`, or when fewer than half of all claims verified;
        `warn` on any `unverifiable`; `pass` otherwise; `skipped` when there was nothing
        to check. The delta is PLAN.md's two Evaluator rows: -0.15 for a refuted claim,
        +0.05 when everything verified, 0 in between -- including a `fail` reached on the
        share alone, since no citation was shown to be fabricated.
        """
        verdicts = [self.check(claim, artifacts) for claim in claims]
        verified = sum(1 for v in verdicts if v.result == "verified")
        refuted = sum(1 for v in verdicts if v.result == "refuted")
        unverifiable = sum(1 for v in verdicts if v.result == "unverifiable")
        total = len(verdicts)

        verdict: Literal["pass", "warn", "fail", "skipped"]
        delta = DELTA_NONE
        if total == 0:
            verdict, reason = "skipped", "no claims to evaluate"
        elif refuted:
            verdict, delta = "fail", DELTA_REFUTED
            reason = f"{refuted} of {total} claim(s) refuted"
        elif verified / total < MIN_VERIFIED_SHARE:
            verdict = "fail"
            reason = (
                f"only {verified} of {total} claim(s) verified "
                f"({verified / total:.0%} < {MIN_VERIFIED_SHARE:.0%})"
            )
        elif unverifiable:
            verdict = "warn"
            reason = f"{unverifiable} of {total} claim(s) unverifiable"
        else:
            verdict, delta = "pass", DELTA_FULLY_VERIFIED
            reason = f"all {total} claim(s) verified"

        return EvaluationReport(
            verdicts=verdicts,
            verified=verified,
            refuted=refuted,
            unverifiable=unverifiable,
            verdict=verdict,
            confidence_delta=delta,
            reason=reason,
        )
