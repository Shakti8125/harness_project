"""Tracing and redaction.

Frozen transcription of PLAN.md Appendix A.9. ``SpanHandle`` and ``SecretRegistry``
are referenced by Appendix A but never defined there; both are given the minimal shape
their use sites imply and are flagged in the Phase 0 handoff note.

The regex denylist that :class:`Redactor` applies is deliberately *not* defined here:
those patterns name specific credential formats of specific vendors, which is domain
knowledge. They are injected via ``patterns``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from src.harness.contracts import RunId, TokenUsage

# Literal replacement written in place of every registered secret value.
REDACTION_PLACEHOLDER: Final[str] = "***REDACTED***"


class Span(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str
    parent_span_id: str | None
    run_id: RunId
    name: str
    component: Literal["orchestrator", "context_manager", "gateway", "memory",
                       "evaluator", "guardrails", "agent", "llm", "api"]
    status: Literal["ok", "error"]
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None
    attributes: dict[str, JsonValue] = {}          # redacted at write time
    error: dict[str, JsonValue] | None = None


class TraceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: RunId
    spans: list[Span]
    totals: TokenUsage
    duration_ms: int
    degraded_components: list[str]


class SpanHandle(Protocol):
    """Live handle to an open span.

    NOT specified in Appendix A -- referenced only as the yield type of
    ``TraceRecorder.span``. Defined here with the minimum its use implies: an identity
    so children can parent themselves to it, and the two mutations a span body needs.
    """

    span_id: str

    def set_attribute(self, key: str, value: JsonValue) -> None: ...

    def set_error(self, error: dict[str, JsonValue]) -> None: ...


class TraceRecorder:
    """Opens spans and persists them; one trace per run."""

    @asynccontextmanager
    def span(self, name: str, component: str, **attrs: JsonValue) -> AsyncIterator[SpanHandle]:
        raise NotImplementedError


class SecretRegistry:
    """Holds resolved secret values so they can be replaced literally wherever they appear.

    NOT specified in Appendix A -- referenced only by ``Redactor.__init__``. Shape is
    derived from PLAN.md "Secrets never reach the trace": at startup the composition
    root registers the resolved value of every config field whose *name* looks like a
    credential, and the redactor substitutes each registered value verbatim. The
    name-matching rule stays in the composition root, which is the only place that can
    see config fields at all.
    """

    def __init__(self, values: Iterable[str] = ()) -> None:
        raise NotImplementedError

    def register(self, name: str, value: str) -> None:
        raise NotImplementedError

    def registered_values(self) -> frozenset[str]:
        raise NotImplementedError


class Redactor:
    """Removes registered secrets and pattern-matched credentials from any JSON value."""

    def __init__(self, registry: SecretRegistry, patterns: Sequence[re.Pattern[str]]) -> None:
        raise NotImplementedError

    def scrub(self, value: JsonValue) -> JsonValue:     # recursive over dict/list/str
        raise NotImplementedError
