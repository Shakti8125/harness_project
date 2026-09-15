"""Tracing and redaction.

Frozen transcription of PLAN.md Appendix A.9, plus the Phase 1 write path (spans
persisted to SQLite, redacted at write time) and the read path A.12 requires.

``SpanHandle`` and ``SecretRegistry`` are referenced by Appendix A but never defined
there; both are given the minimal shape their use sites imply and are flagged in the
Phase 0 handoff note.

**Deviation from Appendix A.9, recorded rather than discovered.** A.9 writes
``@asynccontextmanager def span(...)``. ``asynccontextmanager`` decorates an *async
generator*, so the implementation is necessarily ``async def``. The signature is
otherwise unchanged and no caller is affected -- ``async with recorder.span(...)`` reads
identically either way.

**Tracing never fails a run.** Every persistence call here is best-effort: a span that
cannot be written is logged and dropped. The alternative -- a failed ``INSERT`` taking
down a run that had otherwise succeeded -- inverts the point of observability.

The regex denylist that :class:`Redactor` applies is deliberately *not* defined here:
those patterns name specific credential formats of specific vendors, which is domain
knowledge. They are injected via ``patterns``.
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import json
import logging
import re
import secrets
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol, get_args

from pydantic import BaseModel, ConfigDict, JsonValue

from src.harness.contracts import RunId, TokenUsage
from src.harness.storage import apply_migrations, connect

logger = logging.getLogger("harness.observability")

# Literal replacement written in place of every registered secret value.
REDACTION_PLACEHOLDER: Final[str] = "***REDACTED***"

#: Span attribute names the trace read path aggregates into `TraceResponse.totals`.
#: Constants rather than literals at the call sites, because the writer and the reader
#: agreeing on the spelling is the entire mechanism.
ATTR_TOKENS_PROMPT: Final[str] = "tokens.prompt"
ATTR_TOKENS_COMPLETION: Final[str] = "tokens.completion"
ATTR_TOKENS_THINKING: Final[str] = "tokens.thinking"
ATTR_TOKENS_TOTAL: Final[str] = "tokens.total"

#: Span attribute naming a component that degraded during this span. Collected into
#: `TraceResponse.degraded_components`.
ATTR_DEGRADED_COMPONENT: Final[str] = "degraded_component"

#: Values below this length are never treated as a registered secret. A one- or
#: two-character "secret" (an empty or placeholder config value) would otherwise match
#: almost every string passing through `scrub` and redact the entire trace.
MIN_REDACTABLE_SECRET_LEN: Final[int] = 8

Component = Literal[
    "orchestrator", "context_manager", "gateway", "memory",
    "evaluator", "guardrails", "agent", "llm", "api",
]

_COMPONENTS: Final[frozenset[str]] = frozenset(get_args(Component))

#: Spans live in `trace_span`, the table PLAN.md Phase 3's `001_init.sql` names. Phase 1
#: kept a private `spans` table because no migration runner existed; the runner now owns
#: the whole schema (`src/harness/storage.py`) and carries a legacy `spans` table over on
#: first contact, so there is exactly one span table (dispatch decision 3).
_SPAN_TABLE: Final[str] = "trace_span"

# The span a nested `span()` call parents itself to. A ContextVar rather than an
# attribute because concurrent runs share one recorder instance and asyncio tasks each
# get their own view of it -- an instance attribute would interleave two runs' span
# trees into nonsense.
_current_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "harness_current_span_id", default=None
)

# The run an unbound recorder attributes its spans to. Same reasoning as above, and it
# solves a specific composition problem: the composition root builds ONE recorder and
# hands it to every agent at startup, long before any run id exists. Without an ambient
# id those agents' spans would have no run to belong to and would be dropped, leaving a
# trace containing only the spans opened by whoever happened to hold a bound recorder.
_current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "harness_current_run_id", default=None
)


class Span(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str
    parent_span_id: str | None
    run_id: RunId
    name: str
    component: Component
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

    def set_tokens(self, tokens: TokenUsage) -> None:
        """Record token usage under the names `TraceRecorder.read_trace` aggregates.

        On the Protocol rather than only on the concrete span because the aggregation is
        a contract between writer and reader: a caller that sets the four attributes by
        hand is one rename away from a trace whose totals are silently zero.
        """
        ...


class SecretRegistry:
    """Holds resolved secret values so they can be replaced literally wherever they appear.

    NOT specified in Appendix A -- referenced only by ``Redactor.__init__``. Shape is
    derived from PLAN.md "Secrets never reach the trace": at startup the composition
    root registers the resolved value of every config field whose *name* looks like a
    credential, and the redactor substitutes each registered value verbatim. The
    name-matching rule stays in the composition root, which is the only place that can
    see config fields at all.

    Values, not names, are what get matched -- a secret reaches a trace as the bare token
    inside an error message or a request echo, with nothing nearby to say which field it
    came from. Names are kept only so an operator can be told *which* setting leaked
    without the message quoting it.
    """

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._by_value: dict[str, str] = {}
        for value in values:
            self.register("<unnamed>", value)

    def register(self, name: str, value: str) -> None:
        """Register one secret value under a field name.

        Short and empty values are ignored: a placeholder or unset credential would match
        substrings of ordinary text and redact the whole trace, which destroys the
        artifact this class exists to keep usable.
        """
        if value and len(value) >= MIN_REDACTABLE_SECRET_LEN:
            self._by_value[value] = name

    def registered_values(self) -> frozenset[str]:
        return frozenset(self._by_value)

    def name_for(self, value: str) -> str | None:
        """The field name a registered value came from, for operator-facing messages."""
        return self._by_value.get(value)


#: A string that is nothing but base64 (4-char groups, optional padding) and long enough
#: to be worth decoding: 16 characters is 12 bytes, under any credential this codebase
#: registers or pattern-matches, so nothing shorter can hide one.
_BASE64_TEXT: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


class Redactor:
    """Removes registered secrets and pattern-matched credentials from any JSON value.

    Two tiers of pattern. ``patterns`` are credential *shapes* -- a vendor's token prefix,
    a PEM block -- precise enough to apply everywhere, including through a base64
    encoding. ``heuristic_patterns`` are *assignment* shapes (``password=…``,
    ``api_key: …``) that catch a credential a log line echoed but also match the ordinary
    text of a source file; they apply to plain text only. A base64 body may be an
    execution input -- a file an agent drafted, stored with an approval and committed
    later -- and a heuristic that rewrites ``DB_PASSWORD = env.get("DB_PASSWORD")`` inside
    it is not redaction, it is corruption of what a person approved (Phase 5 audit
    finding 1).
    """

    def __init__(
        self,
        registry: SecretRegistry,
        patterns: Sequence[re.Pattern[str]],
        *,
        heuristic_patterns: Sequence[re.Pattern[str]] = (),
    ) -> None:
        self._registry = registry
        self._patterns = tuple(patterns)
        self._heuristic_patterns = tuple(heuristic_patterns)

    def _scrub_str(self, value: str, *, through_encoding: bool = False) -> str:
        # Longest first: when one registered secret is a substring of another, replacing
        # the shorter one first would leave the tail of the longer one in the output.
        for secret in sorted(self._registry.registered_values(), key=len, reverse=True):
            if secret in value:
                value = value.replace(secret, REDACTION_PLACEHOLDER)
        for pattern in self._patterns:
            value = pattern.sub(REDACTION_PLACEHOLDER, value)
        if not through_encoding:
            for pattern in self._heuristic_patterns:
                value = pattern.sub(REDACTION_PLACEHOLDER, value)
        if _BASE64_TEXT.fullmatch(value):
            value = self._scrub_base64(value)
        return value

    def _scrub_base64(self, value: str) -> str:
        """Scrub *through* a base64 encoding, re-encoding only if something was removed.

        A file body an agent drafted travels as base64 (`content_b64`), and a token
        inside it is invisible to every pattern above while being one decode away from
        anyone holding the trace or the database file (Phase 3 audit finding 8). Nothing
        here knows that field name: any string that is entirely base64 and decodes to
        UTF-8 text gets the registry and the shape patterns -- not the heuristics, see
        the class docstring. A payload with nothing to remove is returned byte-for-byte,
        so an encoding that is later executed is unchanged unless it carried a
        credential -- which is exactly the case where changing it is right, and
        :func:`carries_redaction` is how the executor notices that it was.
        """
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            return value
        scrubbed = self._scrub_str(decoded, through_encoding=True)
        if scrubbed == decoded:
            return value
        return base64.b64encode(scrubbed.encode("utf-8")).decode("ascii")

    def scrub(self, value: JsonValue) -> JsonValue:     # recursive over dict/list/str
        if isinstance(value, str):
            return self._scrub_str(value)
        if isinstance(value, dict):
            # Keys are scrubbed too: a dict keyed by a token is not a shape this codebase
            # produces, but the redactor is the last line and should not assume it.
            return {self._scrub_str(str(k)): self.scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        return value


def carries_redaction(value: JsonValue) -> bool:
    """True when ``value`` -- recursively, and through a base64 encoding -- holds the
    placeholder the :class:`Redactor` writes.

    The question an executor of a *stored* plan asks before running it: a placeholder in
    a tool argument means the scrub at rest changed the argument, so the plan is no
    longer the one that was approved. The right answer is to refuse loudly, never to
    commit ``***REDACTED***`` into a repository as if a person had written it.
    """
    if isinstance(value, str):
        if REDACTION_PLACEHOLDER in value:
            return True
        if _BASE64_TEXT.fullmatch(value):
            try:
                decoded = base64.b64decode(value, validate=True).decode("utf-8")
            except (binascii.Error, ValueError):
                return False
            return REDACTION_PLACEHOLDER in decoded
        return False
    if isinstance(value, dict):
        return any(carries_redaction(v) for v in value.values())
    if isinstance(value, list):
        return any(carries_redaction(item) for item in value)
    return False


# --- log lines are a sink too ----------------------------------------------------------
#
# PLAN.md "Secrets never reach the trace", mechanism 2: every string written to a span
# attribute, a log line, an escalation payload or a stored row goes through the Redactor.
# Spans, payloads and rows are written by code that holds a Redactor; log lines are
# written by every module in the process, third parties included, so the scrub has to
# sit where every record is born: the log record factory. One factory, installed once;
# the redactor it consults is the most recently installed one (Phase 5 audit finding 4).

_log_redactor: Redactor | None = None
_base_record_factory: Callable[..., logging.LogRecord] | None = None


def _scrub_log_arg(redactor: Redactor, value: object) -> object:
    """One formatting argument, scrubbed without changing what ``%`` will do with it.

    Strings are scrubbed in place. An exception is replaced by its scrubbed ``str()`` --
    ``%s`` of an exception is that string, and an upstream error's text is exactly where
    a token turns up. Numbers, ``None`` and anything else pass through untouched so a
    ``%d`` keeps its integer and a formatter that unpacks the tuple sees the same shape.
    """
    if isinstance(value, str):
        return redactor.scrub(value)
    if isinstance(value, BaseException):
        return redactor.scrub(str(value))
    return value


def _redacting_record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
    assert _base_record_factory is not None
    record = _base_record_factory(*args, **kwargs)
    redactor = _log_redactor
    if redactor is None:
        return record
    if isinstance(record.msg, str):
        record.msg = redactor.scrub(record.msg)
    # `args` keeps its shape: a tuple stays a tuple of the same length, a mapping stays a
    # mapping. uvicorn's access formatter unpacks the tuple positionally, and folding
    # the arguments into `msg` broke every access line (the first live run after the
    # Phase 5 fix round found it).
    if isinstance(record.args, tuple):
        record.args = tuple(_scrub_log_arg(redactor, item) for item in record.args)
    elif isinstance(record.args, dict):
        record.args = {
            key: _scrub_log_arg(redactor, item) for key, item in record.args.items()
        }
    # A credential can straddle the boundary -- `api_key=%s` is an assignment shape only
    # once formatted. If the formatted line still changes under the scrub, fold it: the
    # shape of `args` is lost for this one record, which beats printing the secret.
    try:
        message = record.getMessage()
    except (TypeError, ValueError, KeyError):
        # A format string that does not match its arguments is the caller's bug; leave
        # the record for `logging` to report through its own `handleError` path.
        return record
    scrubbed = redactor.scrub(message)
    if scrubbed != message:
        record.msg = scrubbed
        record.args = ()
    return record


def install_log_redaction(redactor: Redactor) -> None:
    """Route every log record in the process through ``redactor`` at creation.

    Idempotent: the factory is wrapped once, and a later call only swaps the redactor it
    consults (a test builds many contexts; the newest sentinels are the ones that
    matter). The format string and each argument are scrubbed in place -- never
    pre-formatted, so a formatter that reads ``record.args`` itself still can. The
    formatted traceback of ``exc_info`` is produced later by the formatter and is not
    covered (backlog).
    """
    global _log_redactor, _base_record_factory
    _log_redactor = redactor
    if _base_record_factory is None:
        _base_record_factory = logging.getLogRecordFactory()
        logging.setLogRecordFactory(_redacting_record_factory)


class _LiveSpan:
    """Concrete :class:`SpanHandle`. Mutable for the duration of the ``async with`` body."""

    def __init__(self, span_id: str) -> None:
        self.span_id = span_id
        self.attributes: dict[str, JsonValue] = {}
        self.error: dict[str, JsonValue] | None = None

    def set_attribute(self, key: str, value: JsonValue) -> None:
        self.attributes[key] = value

    def set_error(self, error: dict[str, JsonValue]) -> None:
        self.error = error

    def set_tokens(self, tokens: TokenUsage) -> None:
        """Record token usage under the names the trace read path aggregates."""
        self.attributes[ATTR_TOKENS_PROMPT] = tokens.prompt
        self.attributes[ATTR_TOKENS_COMPLETION] = tokens.completion
        self.attributes[ATTR_TOKENS_THINKING] = tokens.thinking
        self.attributes[ATTR_TOKENS_TOTAL] = tokens.total


def new_span_id() -> str:
    return "sp_" + secrets.token_hex(8)


class TraceRecorder:
    """Opens spans and persists them; one trace per run."""

    def __init__(
        self,
        *,
        db_path: Path,
        redactor: Redactor,
        run_id: RunId | None = None,
    ) -> None:
        """Bind the recorder to its store and, optionally, to one run.

        ``run_id`` is optional at the composition root because a recorder is built once,
        at startup, while run ids are minted per request. :meth:`bind` returns a recorder
        fixed to one run; ``span()`` on an unbound recorder is a no-op rather than an
        error, so a component that traces unconditionally stays usable outside a run.

        No connection is held open between calls. Each write opens, writes and closes,
        which costs a file open per span and buys not having to reason about a
        long-lived connection shared across concurrent runs. Every connection comes
        from `storage.connect`, so PLAN.md's SQLite tuning (WAL, `busy_timeout`,
        `synchronous=NORMAL`) applies to span writes exactly as it does to the memory
        store's, which shares the file.
        """
        self.db_path = db_path
        self.redactor = redactor
        self.run_id = run_id

    def bind(self, run_id: RunId) -> TraceRecorder:
        """A recorder writing to the same store, fixed to ``run_id``."""
        return TraceRecorder(db_path=self.db_path, redactor=self.redactor, run_id=run_id)

    @contextmanager
    def run_scope(self, run_id: RunId) -> Iterator[TraceRecorder]:
        """Make ``run_id`` the ambient run for everything called inside the block.

        Yields a recorder bound to it, and — the part that matters — also sets the
        ambient id that every *other* recorder sharing this process picks up. The agents
        were handed their recorder at startup and cannot be re-bound per run; this is what
        makes their spans land in the right trace instead of being dropped for having no
        run to belong to.
        """
        token = _current_run_id.set(run_id)
        try:
            yield self.bind(run_id)
        finally:
            _current_run_id.reset(token)

    async def initialize(self) -> None:
        """Bring the shared schema up to date. Called once at startup by the composition root.

        The same migration runner the memory store calls; idempotent, so whichever of the
        two initialises first does the work and the other finds nothing to do.
        """
        await apply_migrations(self.db_path)

    @asynccontextmanager
    async def span(
        self, name: str, component: str, **attrs: JsonValue
    ) -> AsyncIterator[SpanHandle]:
        """Open a span, yield its handle, and persist it when the body leaves.

        An exception escaping the body is recorded as ``status="error"`` and re-raised --
        the span is a record of what happened, not a handler for it.
        """
        handle = _LiveSpan(new_span_id())
        handle.attributes.update(attrs)
        parent_id = _current_span_id.get()
        started_at = datetime.now(UTC)
        token = _current_span_id.set(handle.span_id)
        status: Literal["ok", "error"] = "ok"
        try:
            yield handle
        except Exception as exc:
            status = "error"
            if handle.error is None:
                handle.set_error({"type": type(exc).__name__, "message": str(exc)})
            raise
        finally:
            _current_span_id.reset(token)
            if handle.error is not None:
                status = "error"
            ended_at = datetime.now(UTC)
            await self._persist(
                handle=handle,
                parent_id=parent_id,
                component=component,
                name=name,
                status=status,
                started_at=started_at,
                ended_at=ended_at,
            )

    async def _persist(
        self,
        *,
        handle: _LiveSpan,
        parent_id: str | None,
        component: str,
        name: str,
        status: Literal["ok", "error"],
        started_at: datetime,
        ended_at: datetime,
    ) -> None:
        run_id = self.run_id or _current_run_id.get()
        if run_id is None:
            return
        if component not in _COMPONENTS:
            # A typo'd component would fail `Span` validation on read, turning a
            # mislabelled span into a broken trace endpoint. Relabel and keep the
            # original as an attribute: the trace stays readable and the mistake stays
            # visible.
            logger.warning("trace: unknown component %r on span %r", component, name)
            handle.attributes["component.requested"] = component
            component = "orchestrator"

        attributes = self.redactor.scrub(handle.attributes)
        error = self.redactor.scrub(handle.error) if handle.error is not None else None
        duration_ms = int((ended_at - started_at).total_seconds() * 1000)
        try:
            async with connect(self.db_path) as db:
                await db.execute(
                    f"INSERT INTO {_SPAN_TABLE} (span_id, parent_span_id, run_id, name,"
                    " component, status, started_at, ended_at, duration_ms, attributes_json,"
                    " error_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        handle.span_id,
                        parent_id,
                        run_id,
                        name,
                        component,
                        status,
                        started_at.isoformat(timespec="microseconds"),
                        ended_at.isoformat(timespec="microseconds"),
                        duration_ms,
                        json.dumps(attributes),
                        json.dumps(error) if error is not None else None,
                    ),
                )
                await db.commit()
        except Exception:  # noqa: BLE001 - tracing must never fail the run it observes
            logger.exception("trace: failed to persist span %r", name)

    async def read_trace(self, run_id: RunId) -> TraceResponse | None:
        """The trace read path A.12 requires (``GET /v1/runs/{run_id}/trace``).

        Owned by the recorder rather than by the memory store: the recorder is what
        writes spans, and in Phase 1 no memory store exists at all. Returns ``None`` when
        the run has no spans, which the route renders as a 404 -- an empty
        ``TraceResponse`` would claim a run exists and merely did nothing.

        ``totals`` and ``degraded_components`` are aggregated from span attributes rather
        than stored a second time on a run row, so a trace can never disagree with the
        spans it is made of.
        """
        async with connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            # Insertion order, exactly as Phase 1's `seq` column gave: a span is persisted
            # when its body leaves, so children precede their parent. `started_at` would
            # read more naturally but ties are routine on a coarse clock, and a stable
            # order beats a natural one for a reader diffing two traces.
            async with db.execute(
                "SELECT span_id, parent_span_id, run_id, name, component, status,"
                " started_at, ended_at, duration_ms, attributes_json, error_json"
                f" FROM {_SPAN_TABLE} WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return None

        spans: list[Span] = []
        totals = {"prompt": 0, "completion": 0, "thinking": 0, "total": 0}
        degraded: list[str] = []
        for row in rows:
            attributes = json.loads(row["attributes_json"])
            spans.append(
                Span(
                    span_id=row["span_id"],
                    parent_span_id=row["parent_span_id"],
                    run_id=row["run_id"],
                    name=row["name"],
                    component=row["component"],
                    status=row["status"],
                    started_at=datetime.fromisoformat(row["started_at"]),
                    ended_at=(
                        datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None
                    ),
                    duration_ms=row["duration_ms"],
                    attributes=attributes,
                    error=json.loads(row["error_json"]) if row["error_json"] else None,
                )
            )
            # Only `llm` spans are summed. Tokens are consumed by provider calls, and a
            # provider call is always an `llm` span; the same counts are also written onto
            # the enclosing `agent` span as a per-stage roll-up, and adding both would
            # double every figure. Filtering by component keeps the roll-up available for
            # reading a single span without corrupting the total.
            if row["component"] == "llm":
                for field, attribute in (
                    ("prompt", ATTR_TOKENS_PROMPT),
                    ("completion", ATTR_TOKENS_COMPLETION),
                    ("thinking", ATTR_TOKENS_THINKING),
                    ("total", ATTR_TOKENS_TOTAL),
                ):
                    value = attributes.get(attribute)
                    if isinstance(value, int):
                        totals[field] += value
            # Accepts a bare name or a list of them: a span that degraded two components
            # cannot say so twice in a dict-shaped attribute bag.
            reported = attributes.get(ATTR_DEGRADED_COMPONENT)
            names = [reported] if isinstance(reported, str) else reported
            if isinstance(names, list):
                for component in names:
                    if isinstance(component, str) and component not in degraded:
                        degraded.append(component)

        starts = [span.started_at for span in spans]
        ends = [span.ended_at for span in spans if span.ended_at is not None]
        duration_ms = (
            int((max(ends) - min(starts)).total_seconds() * 1000) if ends else 0
        )
        return TraceResponse(
            run_id=run_id,
            spans=spans,
            totals=TokenUsage(**totals),
            duration_ms=duration_ms,
            degraded_components=degraded,
        )
