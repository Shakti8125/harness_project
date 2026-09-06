"""Claim verification against the artifacts a run actually collected.

Frozen transcription of PLAN.md Appendix A.6. The evaluator owns the bookkeeping --
tally verdicts, aggregate them into a report, derive a confidence delta -- and holds a
registry of :class:`ClaimChecker` objects. The checkers themselves, which are the only
part that knows how to look something up in a domain artifact, live in the
integration and are injected.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

# PLAN.md "Concrete numbers in one place": the report fails on any refuted claim, or
# when the verified share of all claims falls below this fraction.
MIN_VERIFIED_SHARE: Final[float] = 0.5


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
        raise NotImplementedError

    def evaluate(
        self, claims: Sequence[Claim], artifacts: Mapping[str, BaseModel]
    ) -> EvaluationReport:
        raise NotImplementedError
