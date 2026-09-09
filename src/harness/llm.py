"""Structured-generation seam.

Frozen transcription of PLAN.md Appendix A.10, plus the Phase 1 implementations of
``to_gemini_schema`` and the one concrete :class:`LlmClient`. The harness sends a prompt
plus an already-flattened schema and receives raw text plus usage; parsing back into a
typed model is the caller's responsibility (PLAN.md: the response is *still* validated
with ``model_validate_json``, because a response schema is a strong hint, not a
guarantee).

Transport failures raise. That is the one place in this codebase where an external
system's failure is an exception rather than data, and it is deliberate: the only caller
is :func:`~src.harness.recovery.retry_structured`, whose entire job is to classify these
into attempts and finally into an ``AgentError``. Nothing above that function ever sees
one.
"""

from __future__ import annotations

import logging
import math
import time
import warnings
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from src.harness.contracts import TokenUsage
from src.harness.errors import SchemaTranslationError

logger = logging.getLogger("harness.llm")

# PLAN.md "Concrete numbers in one place": provider request timeout.
DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 60.0

# Appendix A.10 defaults, named so that callers building an `LlmRequest` field-by-field
# (`LLMAgent`) reference one home for each value instead of re-typing the literal. The
# values are the ones in the listing; only their spelling moved, exactly as
# `DEFAULT_REQUEST_TIMEOUT_S` above already did for `timeout_s`.
DEFAULT_TEMPERATURE: Final[float] = 0.0
DEFAULT_MAX_OUTPUT_TOKENS: Final[int] = 4096

#: Appendix B.1: the auth failure message is fixed text, and the key value never appears
#: in it. Pinned as a constant so the string cannot drift away from the contract.
AUTH_FAILURE_MESSAGE: Final[str] = (
    "Gemini authentication failed; check HARNESS_GEMINI_API_KEY"
)

with warnings.catch_warnings():
    # `schema` is a deprecated classmethod on `BaseModel`, so declaring a field of that
    # name makes Pydantic v2 emit a shadowing UserWarning. Appendix A names the field
    # `schema`; renaming it would be a contract change, so the warning is suppressed at
    # the single point where it is raised rather than the field being renamed.
    warnings.filterwarnings(
        "ignore",
        message=r'Field name "schema".*shadows an attribute',
        category=UserWarning,
    )

    class LlmRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        model: str
        prompt: str
        # The ignore below pairs with the warnings filter above: this field deliberately
        # shadows the deprecated `BaseModel.schema` classmethod, because Appendix A names
        # the field `schema` and renaming it would be a contract change.
        schema: dict[str, JsonValue] | None  # type: ignore[assignment]  # provider-flattened
        temperature: float = DEFAULT_TEMPERATURE
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
        thinking_budget: int | None = None
        timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S


class RawLlmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    tokens: TokenUsage
    finish_reason: str
    model: str
    latency_ms: int


class LlmClient(Protocol):
    """A single-shot text generator. Concrete clients are injected at the composition root."""

    async def generate(self, req: LlmRequest) -> RawLlmResponse: ...


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------
# NOT specified in Appendix A. Derived from Appendix B.1, which enumerates the
# conditions and the distinct behaviour each one must get: a rate limit is worth waiting
# out, an auth failure never is, an oversized request wants a smaller context rather than
# a longer sleep. `retry_structured` cannot make those calls from a single opaque
# exception type, so the classification happens here, at the only place that can see the
# provider's own error shape.


class LlmTransportError(Exception):
    """Base for a provider call that did not return a usable response."""

    #: Maps onto `AgentError.kind` when the retry budget is exhausted.
    agent_error_kind: str = "llm_upstream"

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class LlmTimeout(LlmTransportError):
    """The provider did not answer within `timeout_s`."""

    agent_error_kind = "llm_timeout"


class LlmRateLimited(LlmTransportError):
    """429, or a provider-specific quota signal. Carries `Retry-After` when supplied."""

    agent_error_kind = "llm_rate_limited"


class LlmAuthError(LlmTransportError):
    """401/403. Never retried, and never carries the credential in its message."""

    agent_error_kind = "llm_auth"


class LlmUpstreamError(LlmTransportError):
    """5xx, connection failure, or any other transport fault worth one more attempt."""

    agent_error_kind = "llm_upstream"


class LlmContextTooLarge(LlmTransportError):
    """400 because the request exceeded the model's input window.

    Distinct from the others because the useful response is to send *less*, not to wait
    (Appendix B.1: re-assemble at half budget and retry once).
    """

    agent_error_kind = "invalid_output"


# ---------------------------------------------------------------------------
# Schema translation
# ---------------------------------------------------------------------------

#: The provider's schema dialect is a subset of OpenAPI, not JSON Schema. Anything not on
#: this list is dropped rather than passed through: an unrecognised keyword is either
#: ignored (harmless but misleading, because the contract then says something the model
#: never saw) or rejected outright with an opaque 400.
_ALLOWED_SCHEMA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "items",
        "properties",
        "required",
        "propertyOrdering",
        "anyOf",
        "minItems",
        "maxItems",
    }
)

#: JSON Schema type names to the dialect's upper-case spelling.
_TYPE_NAMES: Final[Mapping[str, str]] = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}


def _is_null_schema(node: JsonValue) -> bool:
    return isinstance(node, dict) and node.get("type") == "null"


def _translate(
    node: dict[str, Any],
    defs: Mapping[str, Any],
    ref_stack: tuple[str, ...],
) -> dict[str, JsonValue]:
    """Translate one schema node, dereferencing as it goes.

    ``ref_stack`` carries the ``$ref`` names currently being expanded. A model that
    refers to itself would otherwise inline forever; the dialect has no ``$ref`` to fall
    back on, so a cycle is genuinely untranslatable and is reported as such.
    """
    # A discriminated union survives neither the deref nor the dialect: there is no
    # keyword for "pick the branch whose `kind` field says so", so the provider would
    # silently ignore it and return a shape that fails validation later, at a point far
    # from the cause. PLAN.md requires this be rejected here instead.
    if "discriminator" in node:
        raise SchemaTranslationError(
            "nested discriminated union cannot be expressed in the provider schema "
            "dialect; flatten the union into a single model with optional fields"
        )

    if "$ref" in node:
        ref = str(node["$ref"])
        name = ref.rsplit("/", 1)[-1]
        if name in ref_stack:
            raise SchemaTranslationError(
                f"recursive model reference {name!r} cannot be expressed in the provider "
                "schema dialect, which has no $ref"
            )
        target = defs.get(name)
        if target is None:
            raise SchemaTranslationError(f"unresolvable schema reference {ref!r}")
        merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        return _translate(merged, defs, (*ref_stack, name))

    out: dict[str, JsonValue] = {}

    # `anyOf: [T, null]` is how Pydantic spells `T | None`. The dialect spells it as T
    # with `nullable: true`, so the two-branch optional case is rewritten rather than
    # passed through; a genuine multi-branch union stays an `anyOf`.
    any_of = node.get("anyOf")
    if isinstance(any_of, list):
        non_null = [branch for branch in any_of if not _is_null_schema(branch)]
        nullable = len(non_null) != len(any_of)
        if len(non_null) == 1:
            out = _translate(dict(non_null[0]), defs, ref_stack)
            if nullable:
                out["nullable"] = True
        else:
            out["anyOf"] = [_translate(dict(branch), defs, ref_stack) for branch in non_null]
            if nullable:
                out["nullable"] = True
        if "description" in node:
            out.setdefault("description", str(node["description"]))
        return out

    declared_type = node.get("type")
    if isinstance(declared_type, list):
        # `type: [T, "null"]` -- the other spelling of an optional.
        names = [name for name in declared_type if name != "null"]
        if len(names) != 1:
            raise SchemaTranslationError(
                f"multi-typed schema {declared_type!r} cannot be expressed in the "
                "provider schema dialect"
            )
        out["type"] = _TYPE_NAMES.get(str(names[0]), "STRING")
        if len(names) != len(declared_type):
            out["nullable"] = True
    elif isinstance(declared_type, str):
        out["type"] = _TYPE_NAMES.get(declared_type, "STRING")

    for key in ("description", "format", "enum", "minItems", "maxItems", "nullable"):
        if key in node and key in _ALLOWED_SCHEMA_KEYS:
            out[key] = node[key]

    # A single-valued `Literal` is emitted by Pydantic as `const`, which the dialect does
    # not carry. A one-member `enum` says the same thing in a keyword it does carry.
    if "const" in node:
        out["enum"] = [node["const"]]
        out.setdefault("type", "STRING")

    if "items" in node and isinstance(node["items"], dict):
        out["items"] = _translate(dict(node["items"]), defs, ref_stack)

    properties = node.get("properties")
    if isinstance(properties, dict):
        translated: dict[str, JsonValue] = {}
        for name, child in properties.items():
            if isinstance(child, dict):
                translated[name] = _translate(dict(child), defs, ref_stack)
        out["type"] = "OBJECT"
        out["properties"] = translated
        # The whole reason `propertyOrdering` is set: the dialect generates fields in the
        # order given, and PLAN.md places every reasoning field before the conclusion it
        # supports. Pydantic preserves declaration order in `model_json_schema`, so
        # copying the key order here is what makes "the model reasons before it
        # concludes" true at generation time rather than merely true on paper.
        out["propertyOrdering"] = list(translated)
        required = node.get("required")
        if isinstance(required, list):
            out["required"] = [name for name in required if name in translated]

    return out


def to_gemini_schema(model: type[BaseModel]) -> dict[str, JsonValue]:
    """Rewrite a Pydantic JSON Schema into the constrained dialect the provider accepts.

    Dereferences ``$defs``/``$ref`` inline, rewrites ``anyOf: [T, null]`` into ``T`` plus
    ``nullable: true``, strips keywords the provider rejects, adds ``propertyOrdering``
    from the declared field order, and raises :class:`SchemaTranslationError` for a
    nested discriminated union, which must be flattened instead.
    """
    raw = model.model_json_schema(ref_template="#/$defs/{model}")
    defs = raw.get("$defs", {})
    return _translate({k: v for k, v in raw.items() if k != "$defs"}, defs, ())


# ---------------------------------------------------------------------------
# The concrete client
# ---------------------------------------------------------------------------


def _status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status for a provider exception.

    Duck-typed across the two shapes the provider SDK and its transport use, rather than
    matched against either one's exception classes: the classification below is what
    Appendix B.1 specifies, and it should not stop working because the SDK reorganised
    its exception hierarchy in a patch release.
    """
    for attribute in ("code", "status_code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


#: Phrases that mean "your credential is wrong", whatever status they arrive under.
#: Matched on the message because this provider rejects a bad key with **400
#: INVALID_ARGUMENT / API_KEY_INVALID**, not the 401 or 403 Appendix B.1 names it by. A
#: status-only classifier reads that as a generic bad request and spends the full
#: transient retry budget on a credential that cannot become valid — four round trips and
#: four audit-log entries to reach a conclusion available on the first. The rule B.1 is
#: expressing is "an auth failure is not retryable"; the status code was shorthand for it.
_AUTH_FAILURE_PHRASES: Final[tuple[str, ...]] = (
    "api key not valid",
    "api_key_invalid",
    "invalid api key",
    "api key expired",
    "api key is missing",
    "unauthenticated",
    "permission_denied",
)


#: Upper bound, in seconds, on a provider-supplied retry delay that will actually be slept.
#: The delay is a hint from an external system and is treated as untrusted input: a
#: malformed, absurd or hostile value must not turn one attempt into an unbounded sleep,
#: and the error path is the last place that can afford to misbehave. A longer stated delay
#: is *clamped*, not discarded: falling back to a sub-second jittered backoff because the
#: provider asked for an hour is the one response strictly worse than waiting 20 s.
#:
#: 20 s, lowered from the 60 s this constant started at. 60 s mirrored the only cap PLAN.md
#: states for honouring a provider-supplied delay (Appendix B.2's primary rate-limit row,
#: "capped at 60 s") -- but that row bounds *one* sleep in a different layer, whereas a
#: single caller here can reach this ceiling once per retry, several times per request. At
#: 60 s the sleeps alone dominated the wall clock of a request a human is waiting on. The
#: delays actually worth honouring are the 5-20 s ones a quota refusal states, and those
#: still are.
#:
#: This is a *per-sleep* ceiling and deliberately not the only bound: the sum of the sleeps
#: taken inside one generation loop is bounded separately, by ``RETRY_DELAY_BUDGET_S`` in
#: :mod:`src.harness.recovery`. N sleeps of this size are still N times too long, and a
#: per-sleep clamp cannot see N.
MAX_RETRY_AFTER_S: Final[float] = 20.0

#: Keys under which a provider states "wait this long before asking again". Both spellings
#: of the same field are accepted because the JSON body uses lower camel case while a
#: locally-constructed or proto-derived payload may carry the snake case name.
_RETRY_DELAY_KEYS: Final[tuple[str, ...]] = ("retryDelay", "retry_delay")

#: Depth limit for the payload walk below. The delay is nested two or three levels down in
#: practice; the limit exists so that a cyclic or pathologically deep body cannot turn error
#: classification into a long walk.
_MAX_PAYLOAD_DEPTH: Final[int] = 6


def _duration_to_seconds(value: object) -> float | None:
    """Parse one stated delay into a usable number of seconds, or ``None``.

    Accepts the duration spelling the provider's JSON error body uses (``"41s"``,
    ``"7.5s"``) and a bare number of seconds, which is the other legal form of the
    ``Retry-After`` header. Everything else -- a wrong type, an unparseable string, a
    negative, a zero, or a non-finite value -- returns ``None`` rather than being coerced.
    That includes a duration in its *object* form, ``{"seconds": 41, "nanos": 0}``: proto3
    JSON serialises a duration as the string ``"41s"``, so the object form is not a
    JSON-over-HTTP wire form and cannot arrive here from a decoded body. Not parsing it is
    a recorded limit, not a defect -- if a future transport ever hands over an already
    deserialised message rather than decoded JSON, this is the line to revisit.

    Zero is rejected on purpose. Sleeping for nothing after being told to slow down is how
    a retry budget is spent in milliseconds, which is the failure this whole function
    exists to prevent; ``None`` sends the caller back to jittered backoff instead.
    """
    seconds: float
    if isinstance(value, bool):
        # `bool` is an `int` subclass, and `True` would otherwise parse as one second.
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if text[-1:] in ("s", "S"):
            text = text[:-1]
        try:
            seconds = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(seconds) or seconds <= 0.0:
        return None
    if seconds > MAX_RETRY_AFTER_S:
        # Say so out loud. Overriding the provider's stated delay is invisible from the
        # outside -- the loop simply comes back sooner than it was told to -- and "we
        # waited 20 s after being asked to wait an hour" is the one line that explains an
        # otherwise inexplicable burst of retries when someone reads this back later.
        # `info`, not `debug`: this is a decision taken against an external instruction,
        # not a trace of ordinary parsing.
        logger.info(
            "clamping provider-stated retry delay: stated %.3fs, will sleep %.3fs",
            seconds,
            MAX_RETRY_AFTER_S,
        )
        return MAX_RETRY_AFTER_S
    return seconds


def _retry_delay_from_payload(node: object, depth: int = 0) -> float | None:
    """Search a decoded error body for a stated retry delay.

    The body is a structured status object: a top-level envelope, an error object inside
    it, and a list of typed detail entries, one of which states the delay. The search is by
    key name rather than by position or by the detail entry's type URL, because the shape
    around the key differs between the transports the SDK can be running on (it hands over
    either the whole envelope or just the inner error object) and because a payload is
    exactly the kind of thing that gains a level of nesting in a patch release.
    """
    if depth > _MAX_PAYLOAD_DEPTH:
        return None
    if isinstance(node, Mapping):
        for key in _RETRY_DELAY_KEYS:
            if key in node:
                seconds = _duration_to_seconds(node[key])
                if seconds is not None:
                    return seconds
        for value in node.values():
            found = _retry_delay_from_payload(value, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(node, (list, tuple)):
        for item in node:
            found = _retry_delay_from_payload(item, depth + 1)
            if found is not None:
                return found
    return None


def _retry_delay_from_headers(exc: BaseException) -> float | None:
    """Read a `Retry-After` header off the response attached to a provider exception.

    Duck-typed for the same reason `_status_of` is: the response object hanging off the
    exception is whichever one the configured transport produced, and all of them expose a
    mapping-like `headers`.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    raw = getter("retry-after")
    if raw is None:
        return None
    seconds = _duration_to_seconds(raw)
    if seconds is not None:
        return seconds
    # The header's other legal form is an absolute date. A clock skewed far enough into the
    # past yields a negative delta, which `_duration_to_seconds` rejects, and one skewed far
    # into the future is clamped by the same ceiling as everything else.
    try:
        when = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return _duration_to_seconds((when - datetime.now(UTC)).total_seconds())


def retry_after_seconds(exc: BaseException) -> float | None:
    """The provider's own "wait this long", in seconds, or ``None`` if it did not say.

    ``None`` is the ordinary answer and means exactly what it meant before this function
    existed: the retry loop falls back to jittered exponential backoff. A returned value is
    always positive, finite, and no greater than :data:`MAX_RETRY_AFTER_S`.

    The header is consulted first because it is the protocol-level instruction Appendix B.1
    names; the decoded body is the fallback, and in practice the one that answers, since a
    quota refusal states its delay as a typed detail entry inside the error object rather
    than as a header.

    Every failure here is swallowed. This runs on the error path, where an exception raised
    while classifying an exception replaces a recoverable rate limit with an unrecoverable
    crash, and where the input is an arbitrary object from an external SDK whose attributes
    may do anything at all when touched.
    """
    try:
        from_header = _retry_delay_from_headers(exc)
        if from_header is not None:
            return from_header
        return _retry_delay_from_payload(getattr(exc, "details", None))
    except Exception:  # noqa: BLE001 - see docstring: this must never raise
        logger.debug("could not read a retry delay from %s", type(exc).__name__)
        return None


def classify_provider_error(exc: BaseException) -> LlmTransportError:
    """Map a provider/transport exception onto the Appendix B.1 behaviour classes."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    status = _status_of(exc)

    if status in (401, 403) or any(phrase in text for phrase in _AUTH_FAILURE_PHRASES):
        # Appendix B.1: fixed message, no retry, and the key value never appears in it.
        return LlmAuthError(AUTH_FAILURE_MESSAGE)
    if status == 429:
        return LlmRateLimited(
            "provider rate limited the request", retry_after_s=retry_after_seconds(exc)
        )
    if status == 400 and ("too large" in text or "exceeds" in text or "token" in text):
        return LlmContextTooLarge("request exceeded the model input window")
    if status is not None and 500 <= status < 600:
        # Appendix B.1 gives 503/504 the same row as 429, "honour `Retry-After`" included.
        return LlmUpstreamError(
            f"provider returned {status}", retry_after_s=retry_after_seconds(exc)
        )
    if "timeout" in name or "timeout" in text or "timed out" in text:
        return LlmTimeout("provider call timed out")
    if "connect" in name or "connection" in text:
        return LlmUpstreamError("provider unreachable")
    # The exception type and status are enum-shaped and safe to surface; the provider's
    # own message body is not repeated, since it can echo request content back.
    detail = f" ({status})" if status is not None else ""
    return LlmUpstreamError(f"provider call failed: {type(exc).__name__}{detail}")


class GeminiClient:
    """`LlmClient` backed by the google-genai SDK.

    The SDK is imported inside ``__init__`` rather than at module scope so that importing
    this module -- which every agent does, for `LlmRequest` and `to_gemini_schema` -- does
    not require the provider package to be installed or an API key to exist. Only the
    composition root that actually builds a client pays for either.
    """

    def __init__(
        self,
        *,
        api_key: str,
        default_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        from google import genai  # noqa: PLC0415 - deliberately deferred, see docstring

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self._default_timeout_s = default_timeout_s

    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        from google.genai import types  # noqa: PLC0415 - deliberately deferred

        config: dict[str, Any] = {
            "temperature": req.temperature,
            "max_output_tokens": req.max_output_tokens,
            "http_options": types.HttpOptions(
                timeout=int((req.timeout_s or self._default_timeout_s) * 1000)
            ),
        }
        if req.schema is not None:
            # `response_mime_type` is what turns the schema from a suggestion in the
            # prompt into a decoding constraint. Setting one without the other is the
            # common way to get free-form prose back from a "structured" call.
            config["response_mime_type"] = "application/json"
            config["response_schema"] = req.schema
        if req.thinking_budget is not None:
            config["thinking_config"] = types.ThinkingConfig(
                thinking_budget=req.thinking_budget
            )

        started = time.monotonic()
        try:
            response = await self._client.aio.models.generate_content(
                model=req.model,
                contents=req.prompt,
                config=types.GenerateContentConfig(**config),
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as a classified transport error
            raise classify_provider_error(exc) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = getattr(response, "usage_metadata", None)
        tokens = TokenUsage(
            prompt=int(getattr(usage, "prompt_token_count", 0) or 0),
            completion=int(getattr(usage, "candidates_token_count", 0) or 0),
            thinking=int(getattr(usage, "thoughts_token_count", 0) or 0),
            total=int(getattr(usage, "total_token_count", 0) or 0),
        )

        candidates = getattr(response, "candidates", None) or []
        finish_reason = "UNKNOWN"
        if candidates:
            raw_reason = getattr(candidates[0], "finish_reason", None)
            finish_reason = str(getattr(raw_reason, "name", raw_reason) or "UNKNOWN")

        return RawLlmResponse(
            text=response.text or "",
            tokens=tokens,
            finish_reason=finish_reason,
            model=req.model,
            latency_ms=latency_ms,
        )
