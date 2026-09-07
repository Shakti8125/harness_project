"""The Context Manager's seven-step algorithm, and the guarantee it exists for.

PLAN.md Phase 1 Verify, step 1. The load-bearing case is
`test_error_lines_never_trimmed`: a 50,000-line synthetic log with an `AssertionError` at
line 12,345, asserting that line survives budgeting *verbatim*. Everything else in this
module exists to stop that test from passing for the wrong reason — a budget so generous
that nothing was trimmed at all would satisfy it while proving nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.harness.context_manager import (
    ContextBudget,
    ContextManager,
    ContextRequest,
    Section,
    strip_ansi,
    strip_timestamp_prefix,
)

ANCHOR_PATTERNS = [
    r"^E\s",
    r"^FAILED\s",
    r"^ERROR\b",
    r"Traceback \(most recent call last\)",
    r"##\[error\]",
    r"AssertionError",
    r"Error:\s",
    r"npm ERR!",
    r"exit code \d+",
    r"\bTimeout\b",
    r"Connection refused",
    r"ModuleNotFoundError",
    r"ImportError",
]

ASSERTION_LINE = "E       AssertionError: assert 91 == 90"


def synthetic_log(total_lines: int = 50_000, anchor_at: int = 12_345) -> str:
    """A log of `total_lines` with the assertion on line `anchor_at` (1-based)."""
    lines = [
        f"2026-09-05T14:03:{index % 60:02d}.0000000Z run step {index} of {total_lines}"
        for index in range(1, total_lines + 1)
    ]
    lines[anchor_at - 1] = ASSERTION_LINE
    return "\n".join(lines)


def assemble(content: str, budget: ContextBudget | None = None):
    manager = ContextManager()
    return manager.assemble(
        ContextRequest(
            sections=[Section(key="log", content=content, priority=10)],
            budget=budget or ContextBudget(),
            anchor_patterns=ANCHOR_PATTERNS,
        )
    )


def test_error_lines_never_trimmed() -> None:
    """PLAN.md Phase 1 Verify step 1: the assertion at line 12,345 survives verbatim."""
    bundle = assemble(synthetic_log())

    assert ASSERTION_LINE in bundle.text
    assert ASSERTION_LINE in bundle.per_section["log"]

    report = bundle.truncation["log"]
    # The guarantee is only meaningful if the budget actually bit. Without this the test
    # would pass on a log that fit whole.
    assert report.original_lines == 50_000
    assert report.kept_lines < report.original_lines
    assert report.elisions, "expected the budget to elide something"
    assert report.anchors_found == 1
    assert report.anchors_kept == 1
    assert report.anchors_dropped == 0


def test_the_anchor_survives_with_its_surrounding_window() -> None:
    """Anchors are kept +/- 20 lines, so the failure arrives with its context."""
    bundle = assemble(synthetic_log())
    text = bundle.per_section["log"]

    # The 20 lines either side of line 12,345 are lines 12,325..12,365.
    assert "run step 12325 of 50000" in text
    assert "run step 12365 of 50000" in text
    # ...and the line just outside the window is not pulled in by the window itself.
    assert "run step 12324 of 50000" not in text


def test_head_and_tail_are_kept() -> None:
    """Step 5: the first 200 and last 400 lines fill the remaining budget."""
    bundle = assemble(synthetic_log())
    text = bundle.per_section["log"]

    assert "run step 1 of 50000" in text
    assert "run step 200 of 50000" in text
    assert "run step 50000 of 50000" in text
    assert "run step 49601 of 50000" in text
    # Line 201 is outside the head, the tail and every anchor window.
    assert "run step 201 of 50000" not in text


def test_elisions_are_marked_and_reported() -> None:
    """Step 6: every gap is both visible in the text and recorded in the report."""
    bundle = assemble(synthetic_log())
    report = bundle.truncation["log"]

    assert "lines elided" in bundle.per_section["log"]
    for start, end in report.elisions:
        assert start <= end
        assert f"[{end - start + 1:,} lines elided]" in bundle.per_section["log"]

    # Kept lines plus elided lines account for the whole input, with nothing double
    # counted and nothing lost.
    elided = sum(end - start + 1 for start, end in report.elisions)
    assert report.kept_lines + elided == report.original_lines


def test_anchors_dropped_is_reported_when_inviolable_content_exceeds_the_budget() -> None:
    """Step 4: keep the LAST windows, and say how many anchors did not fit."""
    # 400 anchors, each dragging a 41-line window, against a budget that fits a few.
    lines = [f"filler line {index}" for index in range(4_000)]
    for index in range(0, 4_000, 10):
        lines[index] = f"E       AssertionError: failure number {index}"
    budget = ContextBudget(total_chars=6_000, reserve_chars=0)

    bundle = assemble("\n".join(lines), budget)
    report = bundle.truncation["log"]

    assert report.anchors_found == 400
    assert report.anchors_dropped > 0
    assert report.anchors_kept + report.anchors_dropped == report.anchors_found
    # The proximate failure is nearest the end, so it is the tail that survives.
    assert "failure number 3990" in bundle.per_section["log"]
    assert "failure number 0" not in bundle.per_section["log"]


def test_normalizers_strip_ansi_and_timestamps() -> None:
    assert strip_ansi("\x1b[31mE   boom\x1b[0m") == "E   boom"
    assert strip_timestamp_prefix("2026-09-05T14:03:59.1234567Z hello") == "hello"
    # Only a leading timestamp, and only one.
    assert strip_timestamp_prefix("prefix 2026-09-05T14:03:59.1Z x") == (
        "prefix 2026-09-05T14:03:59.1Z x"
    )


def test_normalization_lets_an_anchor_match_a_decorated_line() -> None:
    """The anchors are written against clean text, so stripping has to happen first.

    `^E\\s` cannot match a line that begins with a timestamp and a colour escape. This is
    the case that makes step 1 a prerequisite of step 2 rather than a cosmetic tidy-up.
    """
    decorated = "2026-09-05T14:03:59.1234567Z \x1b[31mE       assert 91 == 90\x1b[0m"
    bundle = assemble("\n".join(["noise"] * 100 + [decorated] + ["noise"] * 100))

    assert bundle.truncation["log"].anchors_found == 1
    assert "E       assert 91 == 90" in bundle.per_section["log"]


def test_budget_is_respected_and_reserve_is_withheld() -> None:
    budget = ContextBudget(total_chars=20_000, reserve_chars=5_000)
    bundle = assemble(synthetic_log(), budget)

    assert bundle.truncation["log"].kept_chars <= budget.total_chars - budget.reserve_chars


def filler(prefix: str, count: int = 5_000) -> str:
    return "\n".join(f"{prefix} {index}" for index in range(count))


def test_sections_are_budgeted_by_priority() -> None:
    """A higher-priority section draws from the pool first; 10 means trim last."""
    manager = ContextManager()
    bundle = manager.assemble(
        ContextRequest(
            sections=[
                Section(key="low", content=filler("low"), priority=1),
                Section(key="high", content=filler("high"), priority=10),
            ],
            budget=ContextBudget(total_chars=12_000, reserve_chars=0),
            anchor_patterns=[],
        )
    )

    assert bundle.truncation["high"].kept_chars > bundle.truncation["low"].kept_chars


def test_empty_section_does_not_explode() -> None:
    bundle = assemble("")
    assert bundle.truncation["log"].original_lines == 1
    assert bundle.truncation["log"].anchors_found == 0


@pytest.fixture
def fixture_log(repo_root: Path) -> str:
    path = repo_root / "fixtures/scenarios/real_regression/logs/job_601234567.txt"
    return path.read_text(encoding="utf-8")


def test_real_fixture_log_keeps_its_assertion(fixture_log: str) -> None:
    """The same guarantee, against the actual recorded log rather than a synthetic one."""
    assert len(fixture_log.splitlines()) > 3_000, "the fixture log must be big enough to trim"

    bundle = assemble(fixture_log)
    text = bundle.per_section["log"]
    report = bundle.truncation["log"]

    assert report.kept_lines < report.original_lines, "expected real trimming"
    assert report.anchors_dropped == 0
    assert "E       assert 91 == 90" in text
    assert "test_discount_applies" in text
    assert "assert 455 == 450" in text, "the cascading second failure survives too"
