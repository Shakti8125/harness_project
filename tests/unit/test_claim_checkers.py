# ruff: noqa: E501  -- one check per line reads as a table; wrapping each call hides the case
"""The five claim checkers, kind by kind, plus the availability model they read.

Every checker answers one of three ways, and which of `refuted` / `unverifiable` applies
is decided from `EvidenceAvailability` -- that is the property most of these pin. The
bundles are hand-built so each case is one line of setup; the fixtures' real logs and
diffs are exercised through the e2e suite.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from src.harness.evaluator import Claim
from src.integrations.cicd.claim_checkers import (
    AVAILABILITY_KEY,
    BUNDLE_KEY,
    DIAGNOSIS_KEY,
    CommitInRangeChecker,
    DependencyBumpChecker,
    EvidenceAvailability,
    FileInDiffChecker,
    QuoteExistsChecker,
    TestInLogChecker,
    availability_of,
    collapse_ws,
    find_quote,
    parse_locator,
)
from src.integrations.cicd.schemas import (
    DependencyChange,
    Diagnosis,
    DiffSummary,
    FailureBundle,
    FileChange,
    JobRef,
    LogExcerpt,
    PriorHistory,
    TruncationReport,
)

HEAD = "e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df"
BASE = "8d4d0a89231f66f3b3910ad16e033041c898512d"
MID = "1111111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

LOG = """\
2026-09-05T14:03:59.1234567Z tests/test_pricing.py::test_discount_applies \x1b[31mFAILED\x1b[0m   [ 40%]
2026-09-05T14:03:59.1234567Z =================================== FAILURES ===================================
2026-09-05T14:03:59.1234567Z ________________________ test_discount_applies _________________________
2026-09-05T14:03:59.1234567Z     def test_discount_applies() -> None:
2026-09-05T14:03:59.1234567Z >       assert discount(100, 10) == 90
2026-09-05T14:03:59.1234567Z E       assert 91 == 90
2026-09-05T14:03:59.1234567Z E        +  where 91 = discount(100, 10)
2026-09-05T14:03:59.1234567Z tests/test_pricing.py:12: AssertionError
2026-09-05T14:03:59.1234567Z some unrelated line about test_checkout_total
2026-09-05T14:03:59.1234567Z FAILED tests/test_pricing.py::test_discount_applies - assert 91 == 90
"""

PATCH = """\
@@ -1,4 +1,4 @@
 def discount(price: int, percent: int) -> int:
-    return price - (price * percent) // 100
+    return price - (price * percent) // 100 + 1
"""


def job() -> JobRef:
    return JobRef(
        repo="octo-org/harness-demo-repo", workflow_name="CI", workflow_id=9001,
        run_id=501234567, run_attempt=1, job_id=601234567, job_name="test (3.12)",
        head_sha=HEAD, branch="main", event="push",
        started_at=datetime.now(UTC), completed_at=None, conclusion="failure",
    )


def excerpt(text: str, job_id: int = 601234567) -> LogExcerpt:
    lines = text.count("\n")
    return LogExcerpt(
        job_id=job_id, total_lines=lines, included_lines=lines, excerpt=text,
        anchor_line_numbers=[],
        truncation=TruncationReport(
            original_chars=len(text), kept_chars=len(text), original_lines=lines,
            kept_lines=lines, anchors_found=0, anchors_kept=0, anchors_dropped=0, elisions=[],
        ),
    )


def bundle(
    *,
    logs: list[LogExcerpt] | None = None,
    files: list[FileChange] | None = None,
    baseline_kind: str = "branch_green",
    truncated: bool = False,
    commit_shas: list[str] | None = None,
    dependency_changes: list[DependencyChange] | None = None,
) -> FailureBundle:
    files = files if files is not None else [
        FileChange(path="src/pricing/discount.py", status="modified", additions=1, deletions=1, patch=PATCH),
        FileChange(path="README.md", status="modified", additions=2, deletions=0, patch=None),
    ]
    return FailureBundle(
        job=job(),
        logs=logs if logs is not None else [excerpt(LOG)],
        diff=DiffSummary(
            baseline_kind=baseline_kind,  # type: ignore[arg-type]
            base_sha=None if baseline_kind == "none" else BASE,
            head_sha=HEAD,
            files=files,
            truncated=truncated,
            total_files=len(files) + (300 if truncated else 0),
            commit_shas=commit_shas if commit_shas is not None else [MID, HEAD],
        ),
        dependency_changes=dependency_changes or [],
        prior_history=PriorHistory(signature_id=None, unavailable=True),
        cold_start=baseline_kind == "none",
        collected_at=datetime.now(UTC),
    )


def artifacts(
    b: FailureBundle, *, logs: bool | None = None, diff: bool | None = None,
    suspected_package: str | None = None, availability: bool = True,
) -> dict[str, BaseModel]:
    out: dict[str, BaseModel] = {
        BUNDLE_KEY: b,
        DIAGNOSIS_KEY: Diagnosis(
            reasoning="r", category="real_regression", summary="s", self_confidence=0.9,
            citations=[], suggested_action="open_fix_pr", suspected_package=suspected_package,
        ),
    }
    if availability:
        derived = availability_of(b, ())
        out[AVAILABILITY_KEY] = EvidenceAvailability(
            logs=derived.logs if logs is None else logs,
            diff=derived.diff if diff is None else diff,
        )
    return out


def claim(kind: str, quote: str, locator: str = "") -> Claim:
    return Claim(claim_id="cl_01", kind=kind, payload={"locator": locator, "quote": quote, "note": ""})


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------


def test_availability_reads_the_bundle_and_the_degraded_list() -> None:
    assert availability_of(bundle(), ()) == EvidenceAvailability(logs=True, diff=True)
    assert availability_of(bundle(), ("logs",)).logs is False
    assert availability_of(bundle(), ("diff",)).diff is False
    assert availability_of(bundle(logs=[]), ()).logs is False
    # A cold start has no diff at all; a fetched empty diff is still a diff.
    assert availability_of(bundle(baseline_kind="none", files=[]), ()).diff is False
    assert availability_of(bundle(files=[]), ()).diff is True


def test_helpers() -> None:
    assert collapse_ws("  a \n\t b  ") == "a b"
    assert parse_locator("log:job/601234567") == ("log", "job/601234567")
    assert parse_locator("diff:src/x.py") == ("diff", "src/x.py")
    assert parse_locator(" LOG:job/1 ") == ("log", "job/1")
    assert parse_locator("src/x.py") == ("", "src/x.py")
    assert parse_locator("") == ("", "")


# ---------------------------------------------------------------------------
# quote_exists
# ---------------------------------------------------------------------------


def test_quote_exact_in_the_named_log() -> None:
    v = QuoteExistsChecker().check(claim("quote_exists", "assert 91 == 90", "log:job/601234567"), artifacts(bundle()))
    assert (v.result, v.detail, v.matched_locator) == ("verified", "exact", "log:job/601234567")


def test_quote_ignores_ansi_and_timestamps_on_both_sides() -> None:
    quoted = "2026-09-05T14:03:59.1234567Z tests/test_pricing.py::test_discount_applies \x1b[31mFAILED\x1b[0m"
    v = QuoteExistsChecker().check(claim("quote_exists", quoted, "log:job/601234567"), artifacts(bundle()))
    assert v.result == "verified" and v.detail == "exact"


def test_quote_in_a_diff_patch_by_path() -> None:
    v = QuoteExistsChecker().check(
        claim("quote_exists", "return price - (price * percent) // 100 + 1", "diff:src/pricing/discount.py"),
        artifacts(bundle()),
    )
    assert v.result == "verified" and v.matched_locator == "diff:src/pricing/discount.py"


def test_quote_found_under_a_wrong_locator_is_verified_and_says_where() -> None:
    """The quote is the evidence, the locator its address: a wrong job id widens the
    search and `matched_locator` reports the real source."""
    v = QuoteExistsChecker().check(claim("quote_exists", "assert 91 == 90", "log:job/999"), artifacts(bundle()))
    assert v.result == "verified" and v.matched_locator == "log:job/601234567"
    v = QuoteExistsChecker().check(claim("quote_exists", "assert 91 == 90", "nonsense"), artifacts(bundle()))
    assert v.result == "verified"


def test_quote_multiline_is_matched_as_consecutive_lines() -> None:
    quoted = "E       assert 91 == 90\nE        +  where 91 = discount(100, 10)"
    v = QuoteExistsChecker().check(claim("quote_exists", quoted, "log:job/601234567"), artifacts(bundle()))
    assert v.result == "verified" and v.detail == "exact"
    # ...and a two-line quote whose second line is retyped is fuzzy over the window.
    quoted = "E       assert 91 == 90\nE        +  where 91 = discount(100, 11)"
    v = QuoteExistsChecker().check(claim("quote_exists", quoted, "log:job/601234567"), artifacts(bundle()))
    assert v.result == "verified" and v.detail.startswith("fuzzy")


def test_quote_below_the_fuzzy_line_is_refuted() -> None:
    v = QuoteExistsChecker().check(claim("quote_exists", "assert 42 == 90 somewhere else", "log:job/601234567"), artifacts(bundle()))
    assert v.result == "refuted"
    assert "not found in log:job/601234567" in v.detail


def test_empty_quote_is_refuted() -> None:
    v = QuoteExistsChecker().check(claim("quote_exists", "   ", "log:job/601234567"), artifacts(bundle()))
    assert v.result == "refuted" and v.detail == "empty quote"


def test_quote_against_a_missing_log_is_unverifiable() -> None:
    b = bundle(logs=[])
    v = QuoteExistsChecker().check(claim("quote_exists", "assert 91 == 90", "log:job/601234567"), artifacts(b))
    assert v.result == "unverifiable" and "no log excerpt" in v.detail
    # A log that *exists* but the fetch was reported degraded reads the same way.
    v = QuoteExistsChecker().check(claim("quote_exists", "nowhere", "log:job/601234567"), artifacts(bundle(), logs=False))
    assert v.result == "unverifiable"


def test_quote_against_a_missing_diff_is_unverifiable() -> None:
    v = QuoteExistsChecker().check(
        claim("quote_exists", "nowhere", "diff:src/pricing/discount.py"), artifacts(bundle(), diff=False)
    )
    assert v.result == "unverifiable" and "no diff" in v.detail
    cold = bundle(baseline_kind="none", files=[])
    v = QuoteExistsChecker().check(claim("quote_exists", "nowhere", "diff:src/x.py"), artifacts(cold))
    assert v.result == "unverifiable"


def test_find_quote_threshold_is_0_92() -> None:
    text = "the quick brown fox jumps over the lazy dog\n"
    assert find_quote("the quick brown fox jumps over the lazy dog", text) == ("exact", 1.0)
    how, ratio = find_quote("the quick brown fox jumps over the lazy dot", text) or ("", 0.0)
    assert how == "fuzzy" and ratio >= 0.92
    assert find_quote("the quick brown fox jumps over a sleepy cat", text) is None
    assert find_quote("", text) is None


def test_no_bundle_is_unverifiable() -> None:
    for checker in (QuoteExistsChecker(), FileInDiffChecker(), DependencyBumpChecker(), TestInLogChecker(), CommitInRangeChecker()):
        v = checker.check(claim(checker.kind, "x", "log:job/1"), {})
        assert v.result == "unverifiable", checker.kind


# ---------------------------------------------------------------------------
# file_in_diff
# ---------------------------------------------------------------------------


def test_file_in_diff_exact_by_locator_or_quote() -> None:
    v = FileInDiffChecker().check(claim("file_in_diff", "src/pricing/discount.py", "diff:src/pricing/discount.py"), artifacts(bundle()))
    assert (v.result, v.detail) == ("verified", "exact")
    v = FileInDiffChecker().check(claim("file_in_diff", "README.md", f"diff:{HEAD}"), artifacts(bundle()))
    assert (v.result, v.detail) == ("verified", "exact")   # a sha locator is not a path
    v = FileInDiffChecker().check(claim("file_in_diff", "`./src/pricing/discount.py`", ""), artifacts(bundle()))
    assert (v.result, v.detail) == ("verified", "exact")


def test_file_in_diff_suffix_match_on_a_path_boundary() -> None:
    v = FileInDiffChecker().check(claim("file_in_diff", "pricing/discount.py", ""), artifacts(bundle()))
    assert (v.result, v.detail, v.matched_locator) == ("verified", "suffix", "diff:src/pricing/discount.py")
    v = FileInDiffChecker().check(claim("file_in_diff", "discount.py", ""), artifacts(bundle()))
    assert v.result == "verified" and v.detail == "suffix"
    # `count.py` is a suffix of the string but not of a path segment.
    v = FileInDiffChecker().check(claim("file_in_diff", "count.py", ""), artifacts(bundle()))
    assert v.result == "refuted"


def test_file_in_diff_refuted_names_the_count() -> None:
    v = FileInDiffChecker().check(claim("file_in_diff", "src/nope.py", "diff:src/nope.py"), artifacts(bundle()))
    assert v.result == "refuted" and v.detail == "path not in diff (2 files)"


def test_file_in_diff_truncated_compare_is_unverifiable() -> None:
    v = FileInDiffChecker().check(claim("file_in_diff", "src/nope.py", ""), artifacts(bundle(truncated=True)))
    assert v.result == "unverifiable" and "truncated" in v.detail


def test_file_in_diff_without_a_diff_is_unverifiable() -> None:
    v = FileInDiffChecker().check(claim("file_in_diff", "src/nope.py", ""), artifacts(bundle(), diff=False))
    assert v.result == "unverifiable"
    v = FileInDiffChecker().check(claim("file_in_diff", "src/nope.py", ""), artifacts(bundle(baseline_kind="none", files=[])))
    assert v.result == "unverifiable"
    # An empty diff that WAS fetched refutes.
    v = FileInDiffChecker().check(claim("file_in_diff", "src/nope.py", ""), artifacts(bundle(files=[])))
    assert v.result == "refuted" and "(0 files)" in v.detail


def test_file_in_diff_with_no_path_is_refuted() -> None:
    v = FileInDiffChecker().check(claim("file_in_diff", "", ""), artifacts(bundle()))
    assert v.result == "refuted" and v.detail == "no path named"


# ---------------------------------------------------------------------------
# dependency_bump
# ---------------------------------------------------------------------------


def pydantic_bump() -> DependencyChange:
    return DependencyChange(
        ecosystem="pip", manifest_path="requirements.txt", package="pydantic",
        from_version="1.10.13", to_version="2.9.2", source="manifest_diff",
    )


@pytest.mark.parametrize(
    "quote",
    [
        "pip: pydantic 1.10.13 -> 2.9.2 (requirements.txt, manifest_diff)",
        "pydantic 1.10.13 -> 2.9.2",
        "pydantic==2.9.2",
        "+pydantic==2.9.2",
        "pydantic bumped from 1.10.13 to 2.9.2",
        "pydantic",
        "Pydantic 2.9.2",
        # Audit finding 3: punctuation right after a version must not become part of it.
        "pydantic 1.10.13->2.9.2",
        "pydantic bumped from 1.10.13 to 2.9.2.",
        "pydantic (1.10.13 -> 2.9.2).",
        "pydantic==2.9.2,",
    ],
)
def test_dependency_bump_verified_shapes(quote: str) -> None:
    b = bundle(dependency_changes=[pydantic_bump()])
    v = DependencyBumpChecker().check(claim("dependency_bump", quote, "diff:requirements.txt"), artifacts(b))
    assert v.result == "verified", (quote, v.detail)
    assert v.matched_locator == "diff:requirements.txt"
    assert "pydantic 1.10.13 -> 2.9.2" in v.detail


def test_dependency_bump_wrong_version_is_refuted() -> None:
    b = bundle(dependency_changes=[pydantic_bump()])
    v = DependencyBumpChecker().check(claim("dependency_bump", "pydantic 1.10.13 -> 3.0.0", "diff:requirements.txt"), artifacts(b))
    assert v.result == "refuted" and "not 1.10.13, 3.0.0" in v.detail


def test_dependency_bump_unknown_package_is_refuted() -> None:
    b = bundle(dependency_changes=[pydantic_bump()])
    v = DependencyBumpChecker().check(claim("dependency_bump", "fastapi 0.100.0 -> 0.115.0", "diff:requirements.txt"), artifacts(b))
    assert v.result == "refuted" and "no dependency change for fastapi" in v.detail
    v = DependencyBumpChecker().check(claim("dependency_bump", "pydantic", "diff:requirements.txt"), artifacts(bundle()))
    assert v.result == "refuted" and "(0 change(s)" in v.detail


def test_dependency_bump_falls_back_to_the_suspected_package() -> None:
    b = bundle(dependency_changes=[pydantic_bump()])
    v = DependencyBumpChecker().check(
        claim("dependency_bump", "the version moved 1.10.13 -> 2.9.2", "diff:requirements.txt"),
        artifacts(b, suspected_package="pydantic"),
    )
    assert v.result == "verified"


def test_dependency_bump_without_a_diff_is_unverifiable() -> None:
    v = DependencyBumpChecker().check(claim("dependency_bump", "pydantic 1.10.13 -> 2.9.2", ""), artifacts(bundle(), diff=False))
    assert v.result == "unverifiable"


def test_dependency_bump_new_dependency_tolerates_no_from_version() -> None:
    added = DependencyChange(
        ecosystem="pip", manifest_path="requirements.txt", package="pydantic-settings",
        from_version=None, to_version="2.5.2", source="manifest_diff",
    )
    b = bundle(dependency_changes=[added])
    v = DependencyBumpChecker().check(claim("dependency_bump", "pydantic-settings 2.5.2", ""), artifacts(b))
    assert v.result == "verified"
    v = DependencyBumpChecker().check(claim("dependency_bump", "pydantic_settings==2.5.2", ""), artifacts(b))
    assert v.result == "verified"   # `_` and `-` are the same package name


# ---------------------------------------------------------------------------
# test_in_log
# ---------------------------------------------------------------------------


def test_test_in_log_on_an_anchor_line() -> None:
    v = TestInLogChecker().check(claim("test_in_log", "tests/test_pricing.py::test_discount_applies", "log:job/601234567"), artifacts(bundle()))
    assert (v.result, v.detail, v.matched_locator) == ("verified", "on an anchor line", "log:job/601234567")
    v = TestInLogChecker().check(claim("test_in_log", "test_discount_applies", ""), artifacts(bundle()))
    assert v.result == "verified"


def test_test_in_log_on_a_non_anchor_line_is_refuted() -> None:
    """`test_checkout_total` appears in the log, but only on a line no anchor pattern
    matches -- the claim is about the failure, not about the log mentioning a name."""
    v = TestInLogChecker().check(claim("test_in_log", "test_checkout_total", "log:job/601234567"), artifacts(bundle()))
    assert v.result == "refuted" and "no anchor line" in v.detail


def test_test_in_log_missing_log_and_empty_id() -> None:
    v = TestInLogChecker().check(claim("test_in_log", "test_discount_applies", ""), artifacts(bundle(logs=[])))
    assert v.result == "unverifiable"
    v = TestInLogChecker().check(claim("test_in_log", "", ""), artifacts(bundle()))
    assert v.result == "refuted" and v.detail == "empty test id"


# ---------------------------------------------------------------------------
# commit_in_range
# ---------------------------------------------------------------------------


def test_commit_in_range_head_and_intermediate_by_prefix() -> None:
    v = CommitInRangeChecker().check(claim("commit_in_range", HEAD, f"diff:{HEAD}"), artifacts(bundle()))
    assert (v.result, v.detail, v.matched_locator) == ("verified", "head commit", f"diff:{HEAD}")
    v = CommitInRangeChecker().check(claim("commit_in_range", f"commit {MID[:7]} touched pricing", ""), artifacts(bundle()))
    assert (v.result, v.detail) == ("verified", "in range")
    # The locator alone can carry the sha.
    v = CommitInRangeChecker().check(claim("commit_in_range", "the head commit", f"diff:{HEAD[:12]}"), artifacts(bundle()))
    assert v.result == "verified"


def test_commit_in_range_base_is_not_in_range() -> None:
    v = CommitInRangeChecker().check(claim("commit_in_range", BASE, ""), artifacts(bundle()))
    assert v.result == "refuted" and "not among the 2 commit(s)" in v.detail


def test_commit_in_range_no_sha_is_refuted() -> None:
    v = CommitInRangeChecker().check(claim("commit_in_range", "the latest commit", ""), artifacts(bundle()))
    assert v.result == "refuted" and v.detail == "quote names no commit sha"


def test_commit_in_range_unknown_range_is_unverifiable() -> None:
    cold = bundle(baseline_kind="none", files=[], commit_shas=[])
    v = CommitInRangeChecker().check(claim("commit_in_range", HEAD, ""), artifacts(cold))
    assert v.result == "verified" and v.detail == "head commit"   # the head is always known
    v = CommitInRangeChecker().check(claim("commit_in_range", MID, ""), artifacts(cold))
    assert v.result == "unverifiable"
    v = CommitInRangeChecker().check(claim("commit_in_range", MID, ""), artifacts(bundle(commit_shas=[]), diff=False))
    assert v.result == "unverifiable"
    # A bundle stored before this phase: the range list is empty though the diff was fetched.
    v = CommitInRangeChecker().check(claim("commit_in_range", MID, ""), artifacts(bundle(commit_shas=[])))
    assert v.result == "unverifiable" and v.detail == "commit range unknown"


def test_diff_summary_renders_the_commit_range_the_prompt_promises() -> None:
    """Audit finding 4: the v3 prompt tells the model `commit_in_range` quotes "the full
    commit sha as listed under 'Diff against the baseline'", so that section must list
    every sha in the range -- otherwise an intermediate commit can never be cited and the
    only other sha on show, the base, is refuted."""
    from src.integrations.cicd.rendering import render_diff_summary

    text = render_diff_summary(bundle().diff)
    assert "2 commit(s) in range, oldest first:" in text
    listing = text.split("oldest first:", 1)[1]
    assert MID in listing and HEAD in listing
    assert listing.index(MID) < listing.index(HEAD), "oldest first"
    # A cold start has no range and says so, as before.
    assert "No green baseline" in render_diff_summary(bundle(baseline_kind="none", files=[]).diff)
    # A pre-Phase-4 bundle (no list) renders the head alone and says the range is unknown.
    assert "range unknown" in render_diff_summary(bundle(commit_shas=[]).diff)


def test_checkers_derive_availability_when_the_artifact_is_absent() -> None:
    """Without an `availability` artifact the checkers read the bundle alone."""
    v = QuoteExistsChecker().check(claim("quote_exists", "nowhere", "log:job/601234567"), artifacts(bundle(logs=[]), availability=False))
    assert v.result == "unverifiable"
    v = FileInDiffChecker().check(claim("file_in_diff", "x.py", ""), artifacts(bundle(baseline_kind="none", files=[]), availability=False))
    assert v.result == "unverifiable"
