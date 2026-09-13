"""Claim checkers for the Evaluator: the five `Citation.claim_kind`s, checked against the
`FailureBundle` the Investigator actually produced.

Every check is deterministic (PLAN.md Phase 4: "no second LLM call, because using a model
to check a model just moves the credulity"), and every checker answers one of three ways:
`verified` when the artifact supports the claim, `refuted` when the artifact was there
and does not, and `unverifiable` when the artifact is missing -- "absence of evidence is
not evidence of hallucination". Which of the last two applies is decided from
:class:`EvidenceAvailability`, computed by the evaluate agent from the bundle and the
run's degraded components: a compare that could not be fetched and an empty commit both
leave `DiffSummary.files == []`, and only the degraded list tells them apart.

The checkers read three artifacts by name -- the bundle, the diagnosis, and the
availability -- and nothing else. They are the only code that knows what a log excerpt or
a diff patch looks like; `harness/evaluator.py` only tallies.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from src.harness.evaluator import Claim, ClaimChecker, ClaimVerdict
from src.integrations.cicd.fingerprint import anchor_lines, clean_line
from src.integrations.cicd.schemas import Citation, Diagnosis, FailureBundle

#: Artifact keys the checkers read. `bundle` and `diagnosis` are the stage artifacts
#: under the names `wiring.ARTIFACT_KEYS` files them; `availability` is supplied by the
#: evaluate agent alongside them.
BUNDLE_KEY: Final[str] = "bundle"
DIAGNOSIS_KEY: Final[str] = "diagnosis"
AVAILABILITY_KEY: Final[str] = "availability"

#: The Investigator's degraded-component names for a fetch that failed. Spelled here
#: rather than imported from the agent so this module does not depend on it.
DEGRADED_LOGS: Final[str] = "logs"
DEGRADED_DIFF: Final[str] = "diff"

#: PLAN.md Phase 4: on a substring miss, the best-matching line must reach this ratio.
#: "Tight enough that a fabricated quote does not pass"; the eval set measures whether
#: that holds.
FUZZY_THRESHOLD: Final[float] = 0.92

_WS: Final[re.Pattern[str]] = re.compile(r"\s+")
_SHA: Final[re.Pattern[str]] = re.compile(r"\b[0-9a-f]{7,40}\b")
_VERSION: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])v?(\d+(?:\.\d+)+[0-9A-Za-z.+-]*)")
_WORD: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?")
#: Words that appear in a rendered dependency line and are never the package.
_NOT_A_PACKAGE: Final[frozenset[str]] = frozenset(
    {"pip", "npm", "go", "maven", "cargo", "other", "manifest_diff", "lockfile_diff",
     "from", "to", "bump", "bumped", "upgrade", "upgraded", "version", "package"}
)


class EvidenceAvailability(BaseModel):
    """Which of the run's artifacts were actually collected, so a checker can tell
    "the diff does not contain this" from "there is no diff to look in"."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    logs: bool
    diff: bool


def availability_of(bundle: FailureBundle, degraded: Sequence[str]) -> EvidenceAvailability:
    """Derive the availability from the bundle and the run's degraded components.

    Logs are available when at least one excerpt was collected and the fetch did not
    fail. The diff is available when the compare was fetched: not on a cold start
    (`baseline_kind == "none"` -- there is no range to diff) and not after a failed
    compare (`"diff"` degraded). An empty diff from a fetched compare *is* available.
    """
    return EvidenceAvailability(
        logs=bool(bundle.logs) and DEGRADED_LOGS not in degraded,
        diff=bundle.diff.baseline_kind != "none" and DEGRADED_DIFF not in degraded,
    )


def claims_from_citations(citations: Sequence[Citation]) -> list[Claim]:
    """One `Claim` per `Citation`, in order, ids `cl_01`, `cl_02`, ..."""
    return [
        Claim(
            claim_id=f"cl_{index:02d}",
            kind=citation.claim_kind,
            payload={
                "locator": citation.locator,
                "quote": citation.quote,
                "note": citation.note,
            },
        )
        for index, citation in enumerate(citations, start=1)
    ]


# ---------------------------------------------------------------------------
# Shared readers
# ---------------------------------------------------------------------------


def _bundle(artifacts: Mapping[str, BaseModel]) -> FailureBundle | None:
    bundle = artifacts.get(BUNDLE_KEY)
    return bundle if isinstance(bundle, FailureBundle) else None


def _diagnosis(artifacts: Mapping[str, BaseModel]) -> Diagnosis | None:
    diagnosis = artifacts.get(DIAGNOSIS_KEY)
    return diagnosis if isinstance(diagnosis, Diagnosis) else None


def _availability(
    artifacts: Mapping[str, BaseModel], bundle: FailureBundle
) -> EvidenceAvailability:
    availability = artifacts.get(AVAILABILITY_KEY)
    if isinstance(availability, EvidenceAvailability):
        return availability
    # No availability artifact: the best reading the bundle alone supports.
    return availability_of(bundle, degraded=())


def _payload_str(claim: Claim, key: str) -> str:
    value: JsonValue = claim.payload.get(key, "")
    return value if isinstance(value, str) else str(value)


def collapse_ws(text: str) -> str:
    """Whitespace-collapsed, for the substring and fuzzy comparisons."""
    return _WS.sub(" ", text).strip()


def parse_locator(locator: str) -> tuple[str, str]:
    """`"log:job/601234567"` -> `("log", "job/601234567")`; `"diff:src/x.py"` ->
    `("diff", "src/x.py")`; anything without a known prefix -> `("", locator)`."""
    kind, sep, rest = locator.strip().partition(":")
    kind = kind.strip().lower()
    if sep and kind in ("log", "diff"):
        return kind, rest.strip()
    return "", locator.strip()


def _verdict(
    claim: Claim,
    result: Literal["verified", "refuted", "unverifiable"],
    detail: str,
    matched_locator: str | None = None,
) -> ClaimVerdict:
    return ClaimVerdict(
        claim_id=claim.claim_id, kind=claim.kind, result=result,
        detail=detail, matched_locator=matched_locator,
    )


def _no_bundle(claim: Claim) -> ClaimVerdict:
    return _verdict(claim, "unverifiable", "no failure bundle to check against")


def _normalise_path(path: str) -> str:
    path = path.strip().strip("`'\"")
    for prefix in ("./", "a/", "b/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    return path


# ---------------------------------------------------------------------------
# quote_exists
# ---------------------------------------------------------------------------


def find_quote(quote: str, text: str) -> tuple[str, float] | None:
    """Locate `quote` in `text`: `("exact", 1.0)`, `("fuzzy", ratio)`, or `None`.

    Both sides are cleaned of ANSI codes and timestamp prefixes (idempotent on the
    already-normalised excerpt) and whitespace-collapsed before the substring test. On a
    miss, a quote of k lines is compared to every window of k consecutive lines and the
    best window must reach `FUZZY_THRESHOLD` -- `difflib.get_close_matches` applies the
    same `SequenceMatcher.ratio()` behind two cheap prefilters, which is what keeps this
    affordable over a 120 000-character excerpt.
    """
    quote_lines = [collapse_ws(clean_line(line)) for line in quote.splitlines()]
    quote_lines = [line for line in quote_lines if line]
    if not quote_lines:
        return None
    needle = " ".join(quote_lines)
    lines = [collapse_ws(clean_line(line)) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if needle in " ".join(lines):
        return "exact", 1.0
    k = len(quote_lines)
    windows = (
        lines if k == 1
        else [" ".join(lines[i:i + k]) for i in range(max(0, len(lines) - k + 1))]
    )
    best = difflib.get_close_matches(needle, windows, n=1, cutoff=FUZZY_THRESHOLD)
    if not best:
        return None
    return "fuzzy", difflib.SequenceMatcher(None, needle, best[0]).ratio()


def _sources(
    bundle: FailureBundle, kind: str, ref: str
) -> list[tuple[str, str]]:
    """The `(locator, text)` sources a quote may be found in, the named one first.

    The quote is the evidence and the locator its address: a locator naming a job or
    file the bundle does not carry does not refute the quote, it widens the search to
    every source of that kind, and `matched_locator` reports where it was found.
    """
    logs = [(f"log:job/{excerpt.job_id}", excerpt.excerpt) for excerpt in bundle.logs]
    diffs = [
        (f"diff:{change.path}", change.patch)
        for change in bundle.diff.files
        if change.patch
    ]
    if kind == "log":
        candidates = logs
    elif kind == "diff":
        candidates = diffs
    else:
        candidates = logs + diffs
    named = [source for source in candidates if source[0] == f"{kind}:{ref}"]
    rest = [source for source in candidates if source not in named]
    return named + rest


class QuoteExistsChecker:
    kind = "quote_exists"

    def check(self, claim: Claim, artifacts: Mapping[str, BaseModel]) -> ClaimVerdict:
        bundle = _bundle(artifacts)
        if bundle is None:
            return _no_bundle(claim)
        availability = _availability(artifacts, bundle)
        locator_kind, ref = parse_locator(_payload_str(claim, "locator"))
        quote = _payload_str(claim, "quote")
        if not collapse_ws(quote):
            return _verdict(claim, "refuted", "empty quote")

        sources = _sources(bundle, locator_kind, ref)
        for locator, text in sources:
            found = find_quote(quote, text)
            if found is None:
                continue
            how, ratio = found
            detail = "exact" if how == "exact" else f"fuzzy {ratio:.2f}"
            return _verdict(claim, "verified", detail, matched_locator=locator)

        # Not found. Refuted only if the artifact the locator names was actually there.
        if locator_kind == "log" and not availability.logs:
            return _verdict(claim, "unverifiable", "no log excerpt was collected")
        if locator_kind == "diff" and not availability.diff:
            return _verdict(claim, "unverifiable", "no diff was collected")
        if not sources:
            return _verdict(claim, "unverifiable", "no evidence of that kind was collected")
        searched = ", ".join(locator for locator, _ in sources[:3])
        more = f" (+{len(sources) - 3} more)" if len(sources) > 3 else ""
        return _verdict(claim, "refuted", f"quote not found in {searched}{more}")


# ---------------------------------------------------------------------------
# file_in_diff
# ---------------------------------------------------------------------------


class FileInDiffChecker:
    kind = "file_in_diff"

    def check(self, claim: Claim, artifacts: Mapping[str, BaseModel]) -> ClaimVerdict:
        bundle = _bundle(artifacts)
        if bundle is None:
            return _no_bundle(claim)
        availability = _availability(artifacts, bundle)
        locator_kind, ref = parse_locator(_payload_str(claim, "locator"))
        # The path is the locator's when the locator names a file; a `diff:<sha>` locator
        # (the Diagnostician's evidence address for the whole patch) is not a path.
        path = ref if locator_kind == "diff" and ref and not _SHA.fullmatch(ref.lower()) else ""
        path = _normalise_path(path or _payload_str(claim, "quote"))
        if not path:
            return _verdict(claim, "refuted", "no path named")
        if not availability.diff:
            return _verdict(claim, "unverifiable", "no diff was collected")

        paths = [_normalise_path(change.path) for change in bundle.diff.files]
        if path in paths:
            return _verdict(claim, "verified", "exact", matched_locator=f"diff:{path}")
        suffix = next((p for p in paths if p.endswith("/" + path)), None)
        if suffix is not None:
            return _verdict(claim, "verified", "suffix", matched_locator=f"diff:{suffix}")
        if bundle.diff.truncated:
            return _verdict(
                claim, "unverifiable",
                f"path not in the {len(paths)} of {bundle.diff.total_files} files the "
                "compare returned (truncated)",
            )
        return _verdict(claim, "refuted", f"path not in diff ({len(paths)} files)")


# ---------------------------------------------------------------------------
# dependency_bump
# ---------------------------------------------------------------------------


def _package_candidates(quote: str) -> list[str]:
    """Words in the quote that could be a package name, in order of appearance."""
    stripped = _VERSION.sub(" ", quote)
    return [
        word for word in _WORD.findall(stripped)
        if word.lower() not in _NOT_A_PACKAGE and "/" not in word and not word.endswith(".txt")
    ]


def _same_package(a: str, b: str) -> bool:
    return a.lower().replace("_", "-") == b.lower().replace("_", "-")


def _versions_in(quote: str) -> list[str]:
    return [match.group(1) for match in _VERSION.finditer(quote)]


class DependencyBumpChecker:
    kind = "dependency_bump"

    def check(self, claim: Claim, artifacts: Mapping[str, BaseModel]) -> ClaimVerdict:
        bundle = _bundle(artifacts)
        if bundle is None:
            return _no_bundle(claim)
        availability = _availability(artifacts, bundle)
        if not availability.diff:
            return _verdict(claim, "unverifiable", "no diff was collected")

        quote = _payload_str(claim, "quote")
        changes = bundle.dependency_changes
        named = [
            change for change in changes
            if any(_same_package(word, change.package) for word in _package_candidates(quote))
        ]
        if not named:
            diagnosis = _diagnosis(artifacts)
            suspected = diagnosis.suspected_package if diagnosis is not None else None
            if suspected:
                named = [c for c in changes if _same_package(suspected, c.package)]
        if not named:
            candidates = _package_candidates(quote)
            package = candidates[0] if candidates else "(none named)"
            return _verdict(
                claim, "refuted",
                f"no dependency change for {package} ({len(changes)} change(s) in the diff)",
            )

        versions = _versions_in(quote)
        for change in named:
            known = {v.lstrip("v=") for v in (change.from_version, change.to_version) if v}
            if all(version.lstrip("v=") in known for version in versions):
                detail = (
                    f"{change.package} {change.from_version} -> {change.to_version} "
                    f"({change.manifest_path})"
                )
                return _verdict(
                    claim, "verified", detail, matched_locator=f"diff:{change.manifest_path}"
                )
        change = named[0]
        return _verdict(
            claim, "refuted",
            f"{change.package} changed {change.from_version} -> {change.to_version}, "
            f"not {', '.join(versions)}",
        )


# ---------------------------------------------------------------------------
# test_in_log
# ---------------------------------------------------------------------------


class TestInLogChecker:
    kind = "test_in_log"

    def check(self, claim: Claim, artifacts: Mapping[str, BaseModel]) -> ClaimVerdict:
        bundle = _bundle(artifacts)
        if bundle is None:
            return _no_bundle(claim)
        availability = _availability(artifacts, bundle)
        test_id = collapse_ws(_payload_str(claim, "quote"))
        if not test_id:
            return _verdict(claim, "refuted", "empty test id")
        if not availability.logs:
            return _verdict(claim, "unverifiable", "no log excerpt was collected")

        for excerpt in bundle.logs:
            for line in anchor_lines(excerpt.excerpt):
                if test_id in collapse_ws(line):
                    return _verdict(
                        claim, "verified", "on an anchor line",
                        matched_locator=f"log:job/{excerpt.job_id}",
                    )
        return _verdict(claim, "refuted", "test id on no anchor line of the log")


# ---------------------------------------------------------------------------
# commit_in_range
# ---------------------------------------------------------------------------


class CommitInRangeChecker:
    kind = "commit_in_range"

    def check(self, claim: Claim, artifacts: Mapping[str, BaseModel]) -> ClaimVerdict:
        bundle = _bundle(artifacts)
        if bundle is None:
            return _no_bundle(claim)
        availability = _availability(artifacts, bundle)
        quote = _payload_str(claim, "quote")
        _, ref = parse_locator(_payload_str(claim, "locator"))
        candidates = _SHA.findall(quote.lower()) or _SHA.findall(ref.lower())
        if not candidates:
            return _verdict(claim, "refuted", "quote names no commit sha")

        diff = bundle.diff
        known = [sha.lower() for sha in diff.commit_shas] + [diff.head_sha.lower()]
        for candidate in candidates:
            match = next((sha for sha in known if sha.startswith(candidate)), None)
            if match is not None:
                where = "head commit" if match == diff.head_sha.lower() else "in range"
                return _verdict(claim, "verified", where, matched_locator=f"diff:{match}")
        if not availability.diff:
            return _verdict(claim, "unverifiable", "no diff was collected")
        if not diff.commit_shas:
            return _verdict(claim, "unverifiable", "commit range unknown")
        return _verdict(
            claim, "refuted",
            f"{candidates[0]} is not among the {len(diff.commit_shas)} commit(s) in range",
        )


def build_claim_checkers() -> list[ClaimChecker]:
    """The five checkers PLAN.md names, one per `Citation.claim_kind`."""
    return [
        QuoteExistsChecker(),
        FileInDiffChecker(),
        DependencyBumpChecker(),
        TestInLogChecker(),
        CommitInRangeChecker(),
    ]
