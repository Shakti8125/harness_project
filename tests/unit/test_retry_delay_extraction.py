"""Wave-3 audit finding 2: honouring a provider-stated retry delay.

`classify_provider_error` used to construct `LlmRateLimited(retry_after_s=None)`
unconditionally, which left `recovery.py`'s "prefer the provider's own delay" branch dead
-- the harness always fell back to a `[0, backoff_max_s]` jittered draw, burning the whole
4-attempt transient budget in under 4 seconds against a 429 that asked for tens of
seconds.

This file pins the *extraction*, hostile input by hostile input, with no network and no
provider quota spent: everything here is either a bare value fed to the private parser, a
hand-built duck-typed stand-in for a transport exception, or a REAL
`google.genai.errors.ClientError` built the way the SDK itself builds one (an httpx
Response wrapping the exact JSON envelope `google.rpc.RetryInfo` puts on the wire). The
formerly-dead branch in `recovery.retry_structured` gets its own test in
`test_recovery_retry_delay.py`, since that is an integration between this module and the
retry loop, not a property of extraction alone.
"""

from __future__ import annotations

import httpx
import pytest
from google.genai import errors as genai_errors

from src.harness.llm import (
    MAX_RETRY_AFTER_S,
    LlmRateLimited,
    LlmUpstreamError,
    _duration_to_seconds,  # noqa: PLC2701 - whitebox test of the hostile-input parser
    _retry_delay_from_headers,  # noqa: PLC2701
    _retry_delay_from_payload,  # noqa: PLC2701
    classify_provider_error,
    retry_after_seconds,
)

# --- _duration_to_seconds: the hostile-input parser -------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("41s", 41.0),
        ("7.5s", 7.5),
        ("41S", 41.0),
        (41, 41.0),
        (41.0, 41.0),
        ("41", 41.0),
        (" 41s ", 41.0),
    ],
)
def test_duration_to_seconds_accepts_the_legal_shapes(value: object, expected: float) -> None:
    assert _duration_to_seconds(value) == pytest.approx(expected)


def test_duration_to_seconds_clamps_an_absurd_delay() -> None:
    assert _duration_to_seconds("999999s") == MAX_RETRY_AFTER_S
    assert _duration_to_seconds(999999) == MAX_RETRY_AFTER_S


@pytest.mark.parametrize(
    "value",
    [
        "-5s",       # negative
        -5,          # negative, no unit
        "0s",        # zero -- rejected on purpose, see the source docstring
        0,
        "nans",      # parses as float("nan") then fails math.isfinite
        float("nan"),
        "infs",      # parses as float("inf") then fails math.isfinite
        float("inf"),
        "soon",      # not a number at all
        "",
        True,        # bool is an int subclass; True would otherwise read as 1 second
        False,
        None,
        [41],        # wrong type entirely
        {"seconds": 41},
        object(),
    ],
)
def test_duration_to_seconds_rejects_everything_else(value: object) -> None:
    assert _duration_to_seconds(value) is None


# --- _retry_delay_from_payload: the decoded-body walk -----------------------------


def test_payload_search_finds_retry_delay_in_the_typed_details_list() -> None:
    body = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"},
            ],
        }
    }
    assert _retry_delay_from_payload(body) == 41.0


def test_payload_search_accepts_the_snake_case_spelling_too() -> None:
    body = {"error": {"details": [{"retry_delay": "12s"}]}}
    assert _retry_delay_from_payload(body) == 12.0


def test_payload_search_handles_the_inner_error_object_shape() -> None:
    """The replay transport hands over the inner object, not the `{"error": ...}` envelope."""
    inner = {"code": 429, "details": [{"retryDelay": "9s"}]}
    assert _retry_delay_from_payload(inner) == 9.0


def test_payload_search_returns_none_for_a_body_with_no_delay() -> None:
    body = {"error": {"code": 429, "details": [{"@type": "...QuotaFailure", "violations": []}]}}
    assert _retry_delay_from_payload(body) is None


def test_payload_search_is_none_for_none_or_garbage() -> None:
    assert _retry_delay_from_payload(None) is None
    assert _retry_delay_from_payload("not a mapping") is None
    assert _retry_delay_from_payload(12345) is None


def test_payload_search_terminates_on_a_self_referential_body() -> None:
    """A cyclic body must not hang the classification path; the depth cap terminates it."""
    cyclic: dict[str, object] = {"retryDelay": None}
    cyclic["self"] = cyclic
    assert _retry_delay_from_payload(cyclic) is None


def test_payload_search_terminates_on_pathological_nesting_depth() -> None:
    node: dict[str, object] = {"retryDelay": "41s"}
    deep: object = node
    for _ in range(50):
        deep = {"wrapper": deep}
    # 50 levels exceeds `_MAX_PAYLOAD_DEPTH`; the delay is unreachable and must not hang.
    assert _retry_delay_from_payload(deep) is None


# --- retry_after_seconds: header first, body fallback, and hostile exceptions -----


def test_retry_after_seconds_prefers_the_header_over_the_body() -> None:
    class FakeResponse:
        headers = {"retry-after": "5"}

    class FakeExc(Exception):
        response = FakeResponse()
        details = {"error": {"details": [{"retryDelay": "99s"}]}}

    assert retry_after_seconds(FakeExc()) == 5.0


def test_retry_after_seconds_falls_back_to_the_body_when_no_header() -> None:
    class FakeExc(Exception):
        details = {"error": {"details": [{"retryDelay": "23s"}]}}

    assert retry_after_seconds(FakeExc()) == 23.0


def test_retry_after_seconds_swallows_an_exception_whose_attributes_explode() -> None:
    """The provider exception is an arbitrary object on the error path; touching its
    attributes must never itself raise -- that would turn a recoverable rate limit into
    an unrecoverable crash while classifying the failure."""

    class ExplodingExc(Exception):
        @property
        def details(self) -> object:
            raise RuntimeError("details blew up")

        @property
        def response(self) -> object:
            raise RuntimeError("response blew up")

    assert retry_after_seconds(ExplodingExc()) is None


def test_retry_after_seconds_is_none_when_nothing_is_present() -> None:
    assert retry_after_seconds(Exception("plain failure")) is None


def test_retry_delay_from_headers_accepts_an_http_date() -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    future = datetime.now(UTC) + timedelta(seconds=30)

    class FakeResponse:
        headers = {"retry-after": format_datetime(future)}

    class FakeExc(Exception):
        response = FakeResponse()

    seconds = _retry_delay_from_headers(FakeExc())
    assert seconds is not None
    assert 25.0 < seconds <= 30.0


def test_retry_delay_from_headers_is_none_without_a_response() -> None:
    assert _retry_delay_from_headers(Exception("no response attribute")) is None


# --- classify_provider_error: the real SDK exception shape ------------------------


def _client_error(status: int, body: dict[str, object], *, headers: dict[str, str] | None = None
                   ) -> genai_errors.ClientError:
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com/x")
    response = httpx.Response(status, json=body, request=request, headers=headers or {})
    return genai_errors.ClientError(status, body, response)


def test_classify_a_real_429_extracts_and_clamps_the_retry_delay() -> None:
    """The exact shape `harness-core.md` records seeing on the free tier."""
    body = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "quota exceeded",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}],
                },
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"},
            ],
        }
    }
    exc = _client_error(429, body)

    classified = classify_provider_error(exc)

    assert isinstance(classified, LlmRateLimited)
    assert classified.retry_after_s == 41.0


def test_classify_a_real_429_clamps_an_absurd_body_delay() -> None:
    body = {"error": {"code": 429, "details": [{"retryDelay": "3600s"}]}}
    exc = _client_error(429, body)

    classified = classify_provider_error(exc)

    assert isinstance(classified, LlmRateLimited)
    assert classified.retry_after_s == MAX_RETRY_AFTER_S


def test_classify_a_real_429_with_no_delay_in_the_body_is_none() -> None:
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": []}}
    exc = _client_error(429, body)

    classified = classify_provider_error(exc)

    assert isinstance(classified, LlmRateLimited)
    assert classified.retry_after_s is None


def test_classify_a_real_503_also_carries_the_delay() -> None:
    """B.1 gives 503/504 the same row as 429, `Retry-After` included."""
    body = {"error": {"code": 503, "status": "UNAVAILABLE", "details": [{"retryDelay": "8s"}]}}
    exc = _client_error(503, body)

    classified = classify_provider_error(exc)

    assert isinstance(classified, LlmUpstreamError)
    assert classified.retry_after_s == 8.0


def test_classify_prefers_the_header_when_both_are_present() -> None:
    body = {"error": {"code": 429, "details": [{"retryDelay": "99s"}]}}
    exc = _client_error(429, body, headers={"retry-after": "5"})

    classified = classify_provider_error(exc)

    assert isinstance(classified, LlmRateLimited)
    assert classified.retry_after_s == 5.0
