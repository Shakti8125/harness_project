"""Deterministic context budgeting.

Frozen transcription of PLAN.md Appendix A.3. The assembler has no opinion about what
matters in a section: the caller supplies ``anchor_patterns`` (regexes marking lines
worth preserving) and per-section priorities, so nothing here needs to know what an
error looks like in any particular domain.

Default budget numbers mirror PLAN.md "Concrete numbers in one place"; they are named
constants so the table has exactly one representation in code.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_TOTAL_CHARS: Final[int] = 120_000       # ~30 k tokens
DEFAULT_ANCHOR_WINDOW_LINES: Final[int] = 20    # +/- lines around each anchor
DEFAULT_HEAD_LINES: Final[int] = 200
DEFAULT_TAIL_LINES: Final[int] = 400
DEFAULT_RESERVE_CHARS: Final[int] = 8_000


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


class ContextManager:
    """Assembles a budgeted, anchor-preserving text bundle from prioritised sections."""

    def assemble(self, req: ContextRequest) -> ContextBundle:
        raise NotImplementedError


ContextRequest.model_rebuild()
