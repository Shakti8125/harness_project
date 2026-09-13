"""PLAN.md Phase 4 Verify, step 1: the Evaluator over the integration's checkers.

The three tests PLAN names, then the verdict rule itself (`skipped` / `pass` / `warn` /
`fail` by refutation / `fail` by share), the registry, and the delta table. The bundles
are hand-built and small: what is under test is the arithmetic of the report and the
one checker PLAN's step names (`quote_exists`), not the fixtures -- those are
`test_claim_checkers.py` and the e2e suite.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from src.harness.confidence import DEFAULT_ADJUSTMENT_DELTAS
from src.harness.evaluator import (
    DELTA_FULLY_VERIFIED,
    DELTA_REFUTED,
    MIN_VERIFIED_SHARE,
    UNREGISTERED_KIND_DETAIL,
    Claim,
    ClaimVerdict,
    EvaluationReport,
    Evaluator,
)
from src.integrations.cicd.claim_checkers import (
    AVAILABILITY_KEY,
    BUNDLE_KEY,
    DIAGNOSIS_KEY,
    EvidenceAvailability,
    QuoteExistsChecker,
    build_claim_checkers,
    claims_from_citations,
)
from src.integrations.cicd.schemas import (
    Citation,
    Diagnosis,
    DiffSummary,
    FailureBundle,
    JobRef,
    LogExcerpt,
    PriorHistory,
    TruncationReport,
)

LOG = """\
tests/test_pricing.py::test_discount_applies FAILED                     [ 40%]
=================================== FAILURES ===================================
________________________ test_discount_applies _________________________
    def test_discount_applies() -> None:
>       assert discount(100, 10) == 90
E       assert 91 == 90
E        +  where 91 = discount(100, 10)
tests/test_pricing.py:12: AssertionError
=========================== short test summary info ============================
FAILED tests/test_pricing.py::test_discount_applies - assert 91 == 90
"""


def job() -> JobRef:
    return JobRef(
        repo="octo-org/harness-demo-repo", workflow_name="CI", workflow_id=9001,
        run_id=501234567, run_attempt=1, job_id=601234567, job_name="test (3.12)",
        head_sha="e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df", branch="main", event="push",
        started_at=datetime.now(UTC), completed_at=None, conclusion="failure",
    )


def bundle(*, log: str | None = LOG) -> FailureBundle:
    logs = []
    if log is not None:
        lines = log.count("\n")
        logs.append(
            LogExcerpt(
                job_id=601234567, total_lines=lines, included_lines=lines, excerpt=log,
                anchor_line_numbers=[],
                truncation=TruncationReport(
                    original_chars=len(log), kept_chars=len(log), original_lines=lines,
                    kept_lines=lines, anchors_found=0, anchors_kept=0, anchors_dropped=0,
                    elisions=[],
                ),
            )
        )
    return FailureBundle(
        job=job(),
        logs=logs,
        diff=DiffSummary(
            baseline_kind="branch_green", base_sha="a" * 40,
            head_sha="e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df",
        ),
        prior_history=PriorHistory(signature_id=None, unavailable=True),
        collected_at=datetime.now(UTC),
    )


def diagnosis(citations: list[Citation]) -> Diagnosis:
    return Diagnosis(
        reasoning="r", category="real_regression", summary="s", self_confidence=0.9,
        citations=citations, suggested_action="open_fix_pr",
    )


def artifacts(
    b: FailureBundle, citations: list[Citation], *, logs: bool = True, diff: bool = True
) -> dict[str, BaseModel]:
    return {
        BUNDLE_KEY: b,
        DIAGNOSIS_KEY: diagnosis(citations),
        AVAILABILITY_KEY: EvidenceAvailability(logs=logs, diff=diff),
    }


def quote(text: str, locator: str = "log:job/601234567") -> Citation:
    return Citation(claim_kind="quote_exists", locator=locator, quote=text)


# ---------------------------------------------------------------------------
# The three tests PLAN.md names
# ---------------------------------------------------------------------------


def test_fabricated_quote_refuted() -> None:
    """A citation quoting a line the log does not contain -> `fail`, delta -0.15."""
    evaluator = Evaluator(build_claim_checkers())
    citations = [quote("AssertionError: expected 42")]
    report = evaluator.evaluate(claims_from_citations(citations), artifacts(bundle(), citations))

    assert report.verdict == "fail"
    assert report.refuted == 1 and report.verified == 0 and report.unverifiable == 0
    assert report.confidence_delta == pytest.approx(-0.15)
    assert report.verdicts[0].result == "refuted"
    assert "not found" in report.verdicts[0].detail
    assert "1 of 1 claim(s) refuted" in report.reason


def test_paraphrased_quote_verified_fuzzy() -> None:
    """The same line with its whitespace collapsed is `verified`; a near-miss on one
    character reaches the fuzzy path and carries its ratio in `detail`."""
    checker = QuoteExistsChecker()
    collapsed = quote("E    assert   91 ==   90")
    verdict = checker.check(claims_from_citations([collapsed])[0], artifacts(bundle(), [collapsed]))
    assert verdict.result == "verified"
    assert verdict.detail == "exact"          # whitespace collapse makes it a substring
    assert verdict.matched_locator == "log:job/601234567"

    # One character off a 40-character line: a paraphrase in the sense PLAN.md means
    # (truncated/retyped), above the 0.92 line, and reported as fuzzy.
    near = quote("tests/test_pricing.py:12: AssertionErrorr")
    verdict = checker.check(claims_from_citations([near])[0], artifacts(bundle(), [near]))
    assert verdict.result == "verified"
    assert verdict.detail.startswith("fuzzy 0.9")
    ratio = float(verdict.detail.split()[1])
    assert 0.92 <= ratio < 1.0


def test_missing_artifact_is_unverifiable_not_refuted() -> None:
    """No log was collected: the quote cannot be checked, and absence of evidence is
    not evidence of hallucination -- the claim is `unverifiable`, nothing is `refuted`,
    and the -0.15 penalty never fires."""
    evaluator = Evaluator(build_claim_checkers())
    log_quote = quote("assert 91 == 90")
    diff_claim = Citation(
        claim_kind="commit_in_range", locator="diff:e2cdf1b4",
        quote="e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df",
    )
    citations = [log_quote, diff_claim]
    report = evaluator.evaluate(
        claims_from_citations(citations), artifacts(bundle(log=None), citations, logs=False)
    )

    assert report.verdicts[0].result == "unverifiable"
    assert "no log excerpt" in report.verdicts[0].detail
    assert report.verdicts[1].result == "verified"
    assert report.refuted == 0 and report.unverifiable == 1 and report.verified == 1
    # One of two verified is exactly PLAN.md's 0.5 line, not below it: `warn`, so the
    # run proceeds with every effect downgraded, and the delta is 0.
    assert report.verdict == "warn"
    assert report.confidence_delta == 0.0

    # The lone unverifiable claim is still not refuted -- but with nothing verified the
    # share rule (`verified/total < 0.5`, PLAN.md "Concrete numbers", total = every claim)
    # fails the report on its own. Recorded as the literal reading in dispatch decision 4:
    # the reason names the share, `refuted` stays 0, and the -0.15 row does not fire.
    alone = evaluator.evaluate(
        claims_from_citations([log_quote]), artifacts(bundle(log=None), [log_quote], logs=False)
    )
    assert alone.verdicts[0].result == "unverifiable"
    assert alone.verdict == "fail" and alone.refuted == 0
    assert alone.confidence_delta == 0.0
    assert "only 0 of 1" in alone.reason


# ---------------------------------------------------------------------------
# The verdict rule
# ---------------------------------------------------------------------------


class FixedChecker:
    """A checker whose answer is decided by the claim's own payload -- for the arithmetic."""

    kind = "fixed"

    def check(self, claim: Claim, artifacts: Mapping[str, BaseModel]) -> ClaimVerdict:
        result = str(claim.payload["result"])
        return ClaimVerdict(
            claim_id=claim.claim_id, kind=self.kind, result=result,  # type: ignore[arg-type]
            detail="fixed",
        )


def fixed(*results: str) -> list[Claim]:
    return [
        Claim(claim_id=f"cl_{i:02d}", kind="fixed", payload={"result": r})
        for i, r in enumerate(results, start=1)
    ]


@pytest.mark.parametrize(
    ("results", "verdict", "delta"),
    [
        ((), "skipped", 0.0),
        (("verified",), "pass", DELTA_FULLY_VERIFIED),
        (("verified", "verified", "verified"), "pass", DELTA_FULLY_VERIFIED),
        (("verified", "unverifiable"), "warn", 0.0),
        (("verified", "verified", "unverifiable"), "warn", 0.0),
        (("verified", "refuted"), "fail", DELTA_REFUTED),
        (("unverifiable", "refuted"), "fail", DELTA_REFUTED),
        # Fail by share, no refutation: verified/total < 0.5, delta stays 0 (PLAN.md's
        # -0.15 row is "any citation refuted", and none was).
        (("verified", "unverifiable", "unverifiable"), "fail", 0.0),
        (("unverifiable",), "fail", 0.0),
    ],
)
def test_verdict_rule(results: tuple[str, ...], verdict: str, delta: float) -> None:
    report = Evaluator([FixedChecker()]).evaluate(fixed(*results), {})
    assert report.verdict == verdict
    assert report.confidence_delta == pytest.approx(delta)
    assert (report.verified, report.refuted, report.unverifiable) == (
        results.count("verified"), results.count("refuted"), results.count("unverifiable")
    )
    assert len(report.verdicts) == len(results)


def test_fail_by_share_reason_names_the_share() -> None:
    report = Evaluator([FixedChecker()]).evaluate(
        fixed("verified", "unverifiable", "unverifiable"), {}
    )
    assert report.verdict == "fail"
    assert "only 1 of 3" in report.reason
    assert f"{MIN_VERIFIED_SHARE:.0%}" in report.reason


def test_exactly_half_verified_is_not_a_fail() -> None:
    """`< 0.5` fails; `== 0.5` does not (the boundary is PLAN.md's, kept exact)."""
    report = Evaluator([FixedChecker()]).evaluate(fixed("verified", "unverifiable"), {})
    assert report.verdict == "warn"


# ---------------------------------------------------------------------------
# The registry and the deltas
# ---------------------------------------------------------------------------


def test_unregistered_kind_is_unverifiable_never_refuted() -> None:
    report = Evaluator([FixedChecker()]).evaluate(
        [Claim(claim_id="cl_01", kind="nobody_knows", payload={})], {}
    )
    assert report.verdicts[0].result == "unverifiable"
    assert report.verdicts[0].detail == UNREGISTERED_KIND_DETAIL
    assert report.verdict == "fail"   # 0 of 1 verified -- the share rule, not a refutation
    assert report.confidence_delta == 0.0


def test_two_checkers_for_one_kind_is_a_wiring_fault() -> None:
    with pytest.raises(ValueError, match="two claim checkers"):
        Evaluator([FixedChecker(), FixedChecker()])


def test_the_shipped_checkers_cover_every_claim_kind() -> None:
    from typing import get_args

    kinds = set(get_args(Citation.model_fields["claim_kind"].annotation))
    assert Evaluator(build_claim_checkers()).kinds == kinds


def test_deltas_are_the_confidence_tables() -> None:
    """One table owns the numbers; the report's delta cannot drift from what calibrate
    will add."""
    assert DEFAULT_ADJUSTMENT_DELTAS["evidence_fully_verified"] == DELTA_FULLY_VERIFIED
    assert DEFAULT_ADJUSTMENT_DELTAS["evidence_refuted"] == DELTA_REFUTED


def test_claims_from_citations_keeps_order_and_payload() -> None:
    citations = [
        Citation(claim_kind="file_in_diff", locator="diff:a.py", quote="a.py", note="n"),
        Citation(claim_kind="test_in_log", locator="log:job/1", quote="t::x"),
    ]
    claims = claims_from_citations(citations)
    assert [c.claim_id for c in claims] == ["cl_01", "cl_02"]
    assert [c.kind for c in claims] == ["file_in_diff", "test_in_log"]
    assert claims[0].payload == {"locator": "diff:a.py", "quote": "a.py", "note": "n"}


def test_report_shape_is_a6() -> None:
    fields = set(EvaluationReport.model_fields)
    assert fields == {
        "verdicts", "verified", "refuted", "unverifiable", "verdict", "confidence_delta", "reason",
    }
    assert set(ClaimVerdict.model_fields) == {
        "claim_id", "kind", "result", "detail", "matched_locator",
    }
