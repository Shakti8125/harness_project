"""SEC-01: the PEM pattern scans a bounded body, so redaction is linear in its input.

The old pattern's lazy body ran to the end of the input once per unterminated `BEGIN`
line: 62 KB of repeated markers took about a second, and the time quadrupled with every
doubling. Redaction is synchronous work on the one event loop, so that was every route
stalled. The fix must not cost the one thing the pattern is for: a real key block is
still redacted whole.
"""

from __future__ import annotations

import time

import pytest
from pydantic import SecretStr

from src.api.deps import build_redactor
from src.harness.observability import REDACTION_PLACEHOLDER
from src.settings import get_settings

MARKER = "-----BEGIN RSA PRIVATE KEY-----"
PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
    "VTLw7onLRnrq0/IzW7yWR7QkrmBL7jTKEn5u+qKhbwKfBstIs+bMY2Zkp18gnTxK\n"
    "-----END RSA PRIVATE KEY-----"
)


def test_a_megabyte_of_unterminated_begin_lines_scrubs_in_under_half_a_second() -> None:
    redactor = build_redactor(get_settings())
    hostile = MARKER * (1024 * 1024 // len(MARKER))

    started = time.perf_counter()
    scrubbed = redactor.scrub(hostile)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5, f"{elapsed:.2f} s"
    assert isinstance(scrubbed, str) and MARKER not in scrubbed


def test_a_real_key_block_is_still_redacted_whole() -> None:
    redactor = build_redactor(get_settings())

    scrubbed = redactor.scrub(f"key: {PEM} and after")

    assert scrubbed == f"key: {REDACTION_PLACEHOLDER} and after"


def test_the_operator_token_is_redacted_by_type(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "an-operator-token-of-adequate-length-0123456789"
    monkeypatch.setenv("HARNESS_OPERATOR_TOKEN", token)
    get_settings.cache_clear()
    settings = get_settings()
    assert isinstance(settings.operator_token, SecretStr)

    scrubbed = build_redactor(settings).scrub(f"Authorization: Bearer-ish {token}")

    assert token not in str(scrubbed)
