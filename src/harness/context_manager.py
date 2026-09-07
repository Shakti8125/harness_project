"""Deterministic context budgeting.

Frozen transcription of PLAN.md Appendix A.3, plus the seven-step ``assemble()``
algorithm PLAN.md specifies for Phase 1. The assembler has no opinion about what
matters in a section: the caller supplies ``anchor_patterns`` (regexes marking lines
worth preserving) and per-section priorities, so nothing here needs to know what an
error looks like in any particular domain.

Default budget numbers mirror PLAN.md "Concrete numbers in one place"; they are named
constants so the table has exactly one representation in code.

**The load-bearing property.** Anchor windows are selected *before* the budget fill and
are never trimmed by it. A line matching an anchor pattern survives to the output
verbatim, or it is reported as dropped in ``TruncationReport.anchors_dropped`` -- there
is no third outcome in which it silently disappears. That is the difference between this
and a similarity ranker, and it is what ``test_error_lines_never_trimmed`` pins.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_TOTAL_CHARS: Final[int] = 120_000       # ~30 k tokens
DEFAULT_ANCHOR_WINDOW_LINES: Final[int] = 20    # +/- lines around each anchor
DEFAULT_HEAD_LINES: Final[int] = 200
DEFAULT_TAIL_LINES: Final[int] = 400
DEFAULT_RESERVE_CHARS: Final[int] = 8_000

#: Rough chars-per-token used for `ContextBundle.estimated_tokens`. An estimate, not a
#: tokenizer: the budget is enforced in characters, and this figure exists only so the
#: trace can show an order of magnitude next to it.
CHARS_PER_TOKEN: Final[int] = 4

# ANSI SGR / CSI escape sequences. Colour and cursor control carry no meaning once a log
# is being read by a model, and they inflate every line's character cost against the
# budget.
_ANSI_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# A leading RFC-3339 timestamp, which several hosted log providers prepend to every
# line. Stripped for the same reason as the escapes: ~29 identical-shaped characters per
# line is a large fraction of the budget spent on something the line already implies by
# its position. Deliberately anchored and shape-matched rather than named after any
# particular provider -- this module may not know who produced its input.
_TIMESTAMP_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s"
)


def strip_ansi(line: str) -> str:
    """Remove ANSI escape sequences from one line."""
    return _ANSI_ESCAPE_RE.sub("", line)


def strip_timestamp_prefix(line: str) -> str:
    """Remove a leading RFC-3339 timestamp prefix from one line, if present."""
    return _TIMESTAMP_PREFIX_RE.sub("", line, count=1)


#: Applied in order to every line in step 1. Injectable via `ContextManager(normalizers=)`
#: because "this text has escape codes and timestamp prefixes" is an assumption about the
#: shape of the input, not a universal truth.
DEFAULT_NORMALIZERS: Final[tuple[Callable[[str], str], ...]] = (
    strip_ansi,
    strip_timestamp_prefix,
)


class ContextBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total_chars: int = DEFAULT_TOTAL_CHARS
    anchor_window_lines: int = DEFAULT_ANCHOR_WINDOW_LINES
    head_lines: int = DEFAULT_HEAD_LINES
    tail_lines: int = DEFAULT_TAIL_LINES
    # held back for prompt scaffolding + other sections
    reserve_chars: int = DEFAULT_RESERVE_CHARS


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sections: list[Section]
    budget: ContextBudget
    anchor_patterns: list[str]             # supplied by the INTEGRATION, not hardcoded here


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str                               # caller-defined section name
    content: str
    priority: int = Field(ge=0, le=10)     # 10 = trim last
    inviolable_ranges: list[tuple[int, int]] = []


class TruncationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    original_chars: int
    kept_chars: int
    original_lines: int
    kept_lines: int
    anchors_found: int
    anchors_kept: int
    anchors_dropped: int
    elisions: list[tuple[int, int]]        # (start_line, end_line) of each elided range


class ContextBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    per_section: dict[str, str]
    truncation: dict[str, TruncationReport]
    estimated_tokens: int
    cold_start: bool = False


def elision_marker(line_count: int) -> str:
    """The explicit marker written in place of an elided range (step 6).

    PLAN.md fixes the rendering, thousands separator included:
    an ellipsis, the bracketed count, an ellipsis.
    """
    return f"… [{line_count:,} lines elided] …"


class _Block:
    """One protected, contiguous run of lines: an anchor window or a caller range.

    ``start`` and ``end`` are both *inclusive* 0-based line indices, because every range
    in this module is inclusive and mixing the two conventions inside one file is how
    off-by-ones get written.
    """

    __slots__ = ("anchors", "end", "start")

    def __init__(self, start: int, end: int, anchors: int) -> None:
        self.start = start
        self.end = end
        self.anchors = anchors


class ContextManager:
    """Assembles a budgeted, anchor-preserving text bundle from prioritised sections."""

    def __init__(
        self,
        *,
        default_budget: ContextBudget | None = None,
        normalizers: Sequence[Callable[[str], str]] | None = None,
    ) -> None:
        """Bind the assembler to its default budget and line normalizers.

        ``default_budget`` is not consulted by :meth:`assemble` -- ``ContextRequest``
        carries the budget that request is assembled under, per Appendix A.3, and that
        stays true. It exists so the composition root has exactly one place to thread the
        configured character budget into, and every caller building a ``ContextRequest``
        can read it back off the manager instead of each constructing a bare
        ``ContextBudget()`` and silently making the configured value a no-op.

        ``normalizers`` default to stripping ANSI escapes and RFC-3339 line prefixes.
        Pass an empty sequence for input that must reach the model byte-for-byte.
        """
        self.default_budget = default_budget if default_budget is not None else ContextBudget()
        self.normalizers = (
            tuple(normalizers) if normalizers is not None else DEFAULT_NORMALIZERS
        )

    # -- step 1 ------------------------------------------------------------------
    def _normalize(self, content: str) -> list[str]:
        """Split into indexed lines and apply every normalizer to each (step 1).

        Line *count* is preserved: normalizers rewrite lines, they never add or remove
        them, so a line number in a `TruncationReport` still indexes the caller's input.
        """
        lines = content.split("\n")
        for normalizer in self.normalizers:
            lines = [normalizer(line) for line in lines]
        return lines

    # -- step 2 ------------------------------------------------------------------
    @staticmethod
    def _find_anchors(lines: Sequence[str], patterns: Sequence[re.Pattern[str]]) -> list[int]:
        """0-based indices of every line matching any anchor pattern (step 2)."""
        return [
            index
            for index, line in enumerate(lines)
            if any(pattern.search(line) for pattern in patterns)
        ]

    # -- step 3 ------------------------------------------------------------------
    @staticmethod
    def _protected_blocks(
        anchors: Sequence[int],
        caller_ranges: Sequence[tuple[int, int]],
        line_count: int,
        window: int,
    ) -> list[_Block]:
        """Anchor windows and caller ranges, clamped, sorted and merged (step 3).

        ``caller_ranges`` are 1-based inclusive line numbers, matching the convention
        `TruncationReport.elisions` reports in; anchor indices are 0-based internally.
        """
        if line_count == 0:
            return []

        raw: list[_Block] = [
            _Block(max(0, index - window), min(line_count - 1, index + window), 1)
            for index in anchors
        ]
        for start, end in caller_ranges:
            low = max(0, min(start, end) - 1)
            high = min(line_count - 1, max(start, end) - 1)
            if low <= high:
                raw.append(_Block(low, high, 0))

        raw.sort(key=lambda block: (block.start, block.end))
        merged: list[_Block] = []
        for block in raw:
            # `<= end + 1` merges adjacent as well as overlapping runs, so two anchors
            # exactly one window apart produce a single block rather than two separated
            # by a "0 lines elided" marker.
            if merged and block.start <= merged[-1].end + 1:
                merged[-1].end = max(merged[-1].end, block.end)
                merged[-1].anchors += block.anchors
            else:
                merged.append(_Block(block.start, block.end, block.anchors))
        return merged

    @staticmethod
    def _cost(lines: Sequence[str], start: int, end: int) -> int:
        """Characters a line range occupies, counting the newline that joins each line."""
        return sum(len(lines[index]) + 1 for index in range(start, end + 1))

    def _assemble_section(
        self,
        section: Section,
        patterns: Sequence[re.Pattern[str]],
        budget: ContextBudget,
        char_allowance: int,
    ) -> tuple[str, TruncationReport, list[int]]:
        """Run steps 1-7 for one section. Returns (text, report, kept anchor line numbers)."""
        lines = self._normalize(section.content)
        line_count = len(lines)
        # Measured after normalization, as `kept_chars` is, so the two are comparable and
        # their ratio means "how much of what we were budgeting survived".
        original_chars = sum(len(line) + 1 for line in lines)

        anchors = self._find_anchors(lines, patterns)
        blocks = self._protected_blocks(
            anchors, section.inviolable_ranges, line_count, budget.anchor_window_lines
        )

        # -- step 4: inviolable content alone may exceed the allowance. Keep the LAST
        # windows that fit -- the proximate failure is nearest the end -- and count the
        # anchors in the windows that did not survive. Whole blocks only: a half-kept
        # anchor window is exactly the silent truncation this class exists to prevent.
        kept_blocks: list[_Block] = []
        spent = 0
        for block in reversed(blocks):
            block_cost = self._cost(lines, block.start, block.end)
            if spent + block_cost > char_allowance:
                continue
            spent += block_cost
            kept_blocks.append(block)
        kept_blocks.reverse()

        kept: set[int] = set()
        for block in kept_blocks:
            kept.update(range(block.start, block.end + 1))

        anchors_kept = sum(block.anchors for block in kept_blocks)
        anchors_dropped = len(anchors) - anchors_kept

        # -- step 5: fill what is left with the first `head_lines` and last `tail_lines`.
        # Tail first: PLAN.md's whole premise is that the proximate failure sits near the
        # end, so when the remainder cannot hold both, the end is the half worth keeping.
        remaining = char_allowance - spent
        tail_start = max(0, line_count - budget.tail_lines)
        for index in range(line_count - 1, tail_start - 1, -1):
            if index in kept:
                continue
            cost = len(lines[index]) + 1
            if cost > remaining:
                break
            remaining -= cost
            kept.add(index)
        for index in range(min(budget.head_lines, line_count)):
            if index in kept:
                continue
            cost = len(lines[index]) + 1
            if cost > remaining:
                break
            remaining -= cost
            kept.add(index)

        # -- steps 6 and 7: merge the kept indices back into contiguous ranges, join them
        # with explicit markers, and report every gap.
        ordered = sorted(kept)
        ranges: list[tuple[int, int]] = []
        for index in ordered:
            if ranges and index == ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], index)
            else:
                ranges.append((index, index))

        parts: list[str] = []
        elisions: list[tuple[int, int]] = []
        cursor = 0
        for start, end in ranges:
            if start > cursor:
                elisions.append((cursor + 1, start))       # 1-based, inclusive
                parts.append(elision_marker(start - cursor))
            parts.append("\n".join(lines[start : end + 1]))
            cursor = end + 1
        if cursor < line_count:
            elisions.append((cursor + 1, line_count))
            parts.append(elision_marker(line_count - cursor))

        text = "\n".join(parts)
        kept_chars = sum(len(lines[index]) + 1 for index in ordered)
        report = TruncationReport(
            original_chars=original_chars,
            kept_chars=kept_chars,
            original_lines=line_count,
            kept_lines=len(ordered),
            anchors_found=len(anchors),
            anchors_kept=anchors_kept,
            anchors_dropped=anchors_dropped,
            elisions=elisions,
        )
        anchor_line_numbers = [index + 1 for index in anchors if index in kept]
        return text, report, anchor_line_numbers

    def anchor_line_numbers(self, section: Section, anchor_patterns: Sequence[str]) -> list[int]:
        """1-based line numbers of every anchor in a section, before any budgeting.

        Exposed because a caller recording what it sent (an excerpt model carrying
        ``anchor_line_numbers``) needs the same normalization and the same regexes this
        class applies internally -- recomputing them outside would be a second, drifting
        implementation of step 1.
        """
        lines = self._normalize(section.content)
        patterns = [re.compile(pattern) for pattern in anchor_patterns]
        return [index + 1 for index in self._find_anchors(lines, patterns)]

    def assemble(self, req: ContextRequest) -> ContextBundle:
        """Budget every section, preserving anchor windows ahead of everything else.

        Sections are budgeted in descending ``priority`` order, ties broken by their
        position in ``req.sections``: the highest-priority section draws from the pool
        first and a low-priority one lives on what is left, which is what "10 = trim
        last" means. ``budget.reserve_chars`` is withheld from the pool entirely, for the
        prompt scaffolding the caller wraps this text in.

        The rendered ``text`` concatenates the sections back in the caller's original
        order under ``### <key>`` headers. A caller that wants a different arrangement
        renders ``per_section`` itself; the budgeting is the part worth centralising, not
        the layout.
        """
        patterns = [re.compile(pattern) for pattern in req.anchor_patterns]
        pool = max(0, req.budget.total_chars - req.budget.reserve_chars)

        rendered: dict[str, str] = {}
        reports: dict[str, TruncationReport] = {}
        order = sorted(
            range(len(req.sections)),
            key=lambda index: (-req.sections[index].priority, index),
        )
        for index in order:
            section = req.sections[index]
            text, report, _ = self._assemble_section(section, patterns, req.budget, pool)
            rendered[section.key] = text
            reports[section.key] = report
            pool = max(0, pool - report.kept_chars)

        joined = "\n\n".join(
            f"### {section.key}\n{rendered[section.key]}" for section in req.sections
        )
        return ContextBundle(
            text=joined,
            per_section=rendered,
            truncation=reports,
            estimated_tokens=len(joined) // CHARS_PER_TOKEN,
        )


ContextRequest.model_rebuild()
