"""Error fingerprint normalization + signature key computation.

PLAN.md Phase 3, "Fingerprint algorithm", implemented as pure functions over a log text:

1. From the **anchor lines**, take the exception to key on -- for a pytest log the first
   `FAILED <nodeid>` line names the subject and the first `E   Type: message` line is its
   exception; for anything else the last `Type: message` line, falling back to the last
   anchor line.
2. Extract `exc_type` and `message`.
3. Normalize the message (paths, addresses, UUIDs, timestamps, hex runs, durations,
   integers, whitespace, length).
4. `fingerprint = sha256(f"{exc_type}|{normalized}")`.
5. `signature_id` is the harness's business (`memory.signature_id_for`); the integration
   supplies `scope` / `subject_key` / `fingerprint`.

**Anchor lines only** (dispatch decision 6). The Context Manager never trims an anchor
line, so a fingerprint computed here from the raw log and one computed from the budgeted
excerpt agree whenever no anchor block was dropped. That is what lets the key travel on the
bundle and still be recomputable from what the bundle carries.

**Order of the normalisation rules matters in one place**: durations are replaced before
integers. PLAN.md lists `<N>` before `<DUR>`, but applied in that order `1.207s` becomes
`1.<N>s` and nothing is left for the duration rule to match -- two logs differing only in
a measured duration would then fingerprint differently, which is exactly the instability
the rule exists to remove. `[gwN]` (pytest-xdist worker ids) → `<WORKER>` is added for the
same reason: PLAN.md's stability test names it, its rule list does not.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

from pydantic import BaseModel, ConfigDict

from src.harness.memory import SignatureKey

#: PLAN.md Phase 1, step 2 of the Context Manager algorithm. This list lives in the
#: integration and is passed in, because every entry names a convention of a specific
#: toolchain -- the harness's Context Manager takes anchors as a parameter and knows
#: nothing about test runners or workflow log formats. Defined here (Phase 3) rather than
#: in the Investigator because the fingerprint is computed from exactly these lines and
#: the Investigator imports this module; the agents import the constant from here.
ANCHOR_PATTERNS: Final[tuple[str, ...]] = (
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
)

#: `<TS>` etc. are the placeholders PLAN.md names. `<WORKER>` is this module's addition.
MESSAGE_MAX_CHARS: Final[int] = 200

_ANSI: Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: The workflow-runner timestamp prefix on every log line (`2026-09-08T10:16:38.4190510Z `).
_LINE_PREFIX_TS: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\s+"
)

#: One alternation over every anchor pattern: a single `search` per line rather than
#: thirteen, on logs that run to thousands of lines.
_ANY_ANCHOR: Final[re.Pattern[str]] = re.compile("|".join(f"(?:{p})" for p in ANCHOR_PATTERNS))

#: `FAILED tests/test_x.py::test_y` with an optional ` - Type: message` suffix (pytest
#: prints the suffix under `-rA`/`-ra`; the summary line is otherwise bare).
_FAILED_LINE: Final[re.Pattern[str]] = re.compile(
    r"^FAILED\s+(?P<nodeid>\S+)(?:\s+-\s+(?P<rest>.*))?$"
)
#: The exception spelling both Python tracebacks and pytest's `E` lines share:
#: `Type: message`, where `Type` is dotted, CamelCase-ish, and ends like an exception.
_EXCEPTION: Final[re.Pattern[str]] = re.compile(
    r"(?P<type>[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*"
    r"(?:Error|Exception|Exceeded|Timeout|Warning|Fault|Failure|Interrupt|Exit|"
    r"NotFound|Denied|Refused|Reset|Abort|Aborted))"
    r"\s*:\s*(?P<message>.*)$"
)
_PYTEST_E_LINE: Final[re.Pattern[str]] = re.compile(r"^E\s+(?P<body>.*)$")
#: pytest's location line under a failure section: `tests/test_x.py:34: AssertionError`.
#: The type is here, and only here, when the assertion was a bare `assert a == b`.
_LOCATION_LINE: Final[re.Pattern[str]] = re.compile(
    r"^\S+:\d+:\s+(?P<type>[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*)$"
)

_ABS_PATH: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])/(?:[^\s/:'\"`]+/)+([^\s/:'\"`]+)")
_ADDR: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]+")
_UUID: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ISO_TS: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_CLOCK_TS: Final[re.Pattern[str]] = re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b")
#: Hex runs of six or more that contain at least one digit -- so `decade` and `beefed`
#: survive but a sha, a job id in hex, or a worker hash do not.
_HEX_RUN: Final[re.Pattern[str]] = re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{6,}\b")
_WORKER: Final[re.Pattern[str]] = re.compile(r"\[gw\d+\]|\bgw\d+\b")
_DURATION: Final[re.Pattern[str]] = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds?)\b")
_INTEGER: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])\d{2,}(?![\w.])")
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


class FailureAnchor(BaseModel):
    """What step 1 and 2 extracted: the exception, and the test it belongs to if any."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exc_type: str
    message: str
    test_id: str | None = None


def clean_line(line: str) -> str:
    """Strip ANSI colour codes and the runner's timestamp prefix from one log line."""
    return _LINE_PREFIX_TS.sub("", _ANSI.sub("", line)).rstrip()


def anchor_lines(log_text: str) -> list[str]:
    """Every cleaned line matching one of the integration's anchor patterns, in order."""
    lines: list[str] = []
    for raw in log_text.splitlines():
        line = clean_line(raw)
        if _ANY_ANCHOR.search(line):
            lines.append(line)
    return lines


def _split_exception(text: str) -> tuple[str, str] | None:
    match = _EXCEPTION.search(text)
    if match is None:
        return None
    return match.group("type"), match.group("message").strip()


def extract_anchor(log_text: str) -> FailureAnchor:
    """Steps 1 and 2: which exception this log is about, and the test it belongs to."""
    lines = anchor_lines(log_text)

    failed = next((m for m in map(_FAILED_LINE.match, lines) if m is not None), None)
    if failed is not None:
        return _pytest_anchor(lines, failed.group("nodeid"), failed.group("rest"))

    for line in reversed(lines):
        split = _split_exception(line)
        if split is not None:
            return FailureAnchor(exc_type=split[0], message=split[1])
    if lines:
        return FailureAnchor(exc_type="<none>", message=lines[-1])
    return FailureAnchor(exc_type="<none>", message="")


def _pytest_anchor(lines: list[str], test_id: str, summary_rest: str | None) -> FailureAnchor:
    """The first failed test's exception, from the two places pytest can put it.

    The summary suffix (`FAILED nodeid - AssertionError: msg`, or just `- assert 91 == 90`
    for a bare assertion) when present; otherwise the first `E` line, which belongs to the
    first failed test because pytest prints failure sections in summary order. A bare
    assertion carries no type on either line -- it sits on the location line
    (`tests/test_x.py:34: AssertionError`), which is where the fallback reads it.
    """
    message = (summary_rest or "").strip()
    if not message:
        for line in lines:
            e_line = _PYTEST_E_LINE.match(line)
            if e_line is not None:
                message = e_line.group("body").strip()
                break
    split = _split_exception(message) if message else None
    if split is not None:
        return FailureAnchor(exc_type=split[0], message=split[1], test_id=test_id)
    location = next((m for m in map(_LOCATION_LINE.match, lines) if m is not None), None)
    exc_type = location.group("type") if location is not None else "<unknown>"
    return FailureAnchor(exc_type=exc_type, message=message, test_id=test_id)


def normalize_message(message: str) -> str:
    """Step 3, in the order stated in the module docstring."""
    text = _ABS_PATH.sub(r"\1", message)
    text = _ADDR.sub("<ADDR>", text)
    text = _UUID.sub("<UUID>", text)
    text = _ISO_TS.sub("<TS>", text)
    text = _CLOCK_TS.sub("<TS>", text)
    text = _HEX_RUN.sub("<HEX>", text)
    text = _WORKER.sub("<WORKER>", text)
    text = _DURATION.sub("<DUR>", text)
    text = _INTEGER.sub("<N>", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text[:MESSAGE_MAX_CHARS]


def fingerprint_for(anchor: FailureAnchor) -> str:
    """Step 4: `sha256(f"{exc_type}|{normalized}")`."""
    raw = f"{anchor.exc_type}|{normalize_message(anchor.message)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def subject_key_for(workflow_name: str, job_name: str, test_id: str | None) -> str:
    """The schema comment's `"workflow|job|test_id"`; an empty third part when no test."""
    return f"{workflow_name}|{job_name}|{test_id or ''}"


def scope_for(repo: str) -> str:
    """The per-repository partition, `"repo:owner/name"`."""
    return f"repo:{repo}"


def signature_key_for(
    *, repo: str, workflow_name: str, job_name: str, log_text: str
) -> SignatureKey:
    """Steps 1-4 end to end: the key the memory store is queried and written under."""
    anchor = extract_anchor(log_text)
    return SignatureKey(
        scope=scope_for(repo),
        subject_key=subject_key_for(workflow_name, job_name, anchor.test_id),
        fingerprint=fingerprint_for(anchor),
    )
