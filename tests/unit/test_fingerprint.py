"""PLAN.md Phase 3 Verify step 1: fingerprint stability -- the property memory rests on.

`test_stable_across_noise` and `test_distinguishes_real_difference` are the two tests the
plan names. The rest pin the extraction rules on the shipped fixture logs and the one
property dispatch decision 6 relies on: a key computed from the raw log and one computed
from the Context Manager's budgeted excerpt agree, because both are computed from anchor
lines and anchors are never trimmed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.harness.context_manager import ContextBudget, ContextManager, ContextRequest, Section
from src.harness.memory import signature_id_for
from src.integrations.cicd.fingerprint import (
    ANCHOR_PATTERNS,
    anchor_lines,
    clean_line,
    extract_anchor,
    fingerprint_for,
    normalize_message,
    signature_key_for,
)

REPO = "octo-org/harness-demo-repo"

# Two runs of the same flaky test on different days, workers, temp dirs and runners. Every
# difference between them is noise PLAN.md step 3 names; nothing about the failure differs.
def _noisy_log(
    *, day: str, worker: str, elapsed: str, tmp: str, addr: str, clock: str, uid: str, sha: str
) -> str:
    ts = f"2026-09-{day}T{clock}.0000000Z"
    e_line = (
        f"{ts} E       AssertionError: job took {elapsed}s, expected < 1.0s "
        f"(worker {worker}, tmp /tmp/pytest-of-runner/{tmp}/test_job0/data.json, "
        f"obj <Scheduler object at {addr}>, at {clock}, id {uid}, sha {sha})"
    )
    return "\n".join(
        [
            f"{ts} [{worker}] [ 28%] FAILED tests/test_scheduler.py::test_job_runs_within_deadline",
            f"{ts} ________________ test_job_runs_within_deadline _________________",
            f'{ts} >       assert elapsed < 1.0, f"job took {{elapsed:.3f}}s, expected < 1.0s"',
            e_line,
            f"{ts} tests/test_scheduler.py:41: AssertionError",
            f"{ts} FAILED tests/test_scheduler.py::test_job_runs_within_deadline",
            "",
        ]
    )


LOG_A = _noisy_log(
    day="08", worker="gw3", elapsed="1.207", tmp="pytest-12", addr="0x7f2a1c0d4a10",
    clock="10:16:38", uid="3f2a9c1e-1111-4222-8333-944455556666", sha="4f1e2d3c9b8a",
)
LOG_B = _noisy_log(
    day="09", worker="gw7", elapsed="1.334", tmp="pytest-31", addr="0x7f9b8e00ff00",
    clock="22:02:00", uid="8badf00d-2222-4333-8444-955566667777", sha="a1b2c3d4e5f6",
)
# Same test, a genuinely different failure.
LOG_C = LOG_A.replace(
    "AssertionError: job took 1.207s, expected < 1.0s",
    "TimeoutError: job took 1.207s, expected < 1.0s",
).replace("tests/test_scheduler.py:41: AssertionError", "tests/test_scheduler.py:41: TimeoutError")


def key(log: str):  # noqa: ANN202 - test helper
    return signature_key_for(repo=REPO, workflow_name="CI", job_name="test (3.12)", log_text=log)


def test_stable_across_noise() -> None:
    """Timestamps, temp paths, durations, xdist worker ids, addresses, UUIDs and shas differ;
    the signature does not."""
    assert key(LOG_A) == key(LOG_B)
    assert signature_id_for(key(LOG_A)) == signature_id_for(key(LOG_B))


def test_distinguishes_real_difference() -> None:
    """AssertionError vs TimeoutError on the same test are different signatures."""
    assert key(LOG_A).fingerprint != key(LOG_C).fingerprint
    assert signature_id_for(key(LOG_A)) != signature_id_for(key(LOG_C))
    # ... and the same fingerprint under a different subject is a different signature.
    other_job = signature_key_for(
        repo=REPO, workflow_name="CI", job_name="test (3.13)", log_text=LOG_A
    )
    assert other_job.fingerprint == key(LOG_A).fingerprint
    assert signature_id_for(other_job) != signature_id_for(key(LOG_A))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("job took 1.207s, expected < 1.0s", "job took <DUR>, expected < <DUR>"),
        ("waited 350 ms then 2 s", "waited <DUR> then <DUR>"),
        ("object at 0x7f2a1c0d4a10", "object at <ADDR>"),
        ("id 3f2a9c1e-1111-4222-8333-944455556666 gone", "id <UUID> gone"),
        ("at 2026-09-08T10:16:38.4190510Z and 10:16:38", "at <TS> and <TS>"),
        ("commit 4f1e2d3c9b8a7f6e", "commit <HEX>"),
        ("a decade of beefed-up code", "a decade of beefed-up code"),
        ("/tmp/pytest-of-runner/pytest-12/test_job0/data.json", "data.json"),
        ("[gw3] worker gw12", "<WORKER> worker <WORKER>"),
        ("assert 91 == 90", "assert <N> == <N>"),
        ("port=443 v1.2.3 x9", "port=<N> v1.2.3 x9"),
        ("  too   much\t space  ", "too much space"),
        ("y" * 500, "y" * 200),
    ],
)
def test_normalize_message(raw: str, expected: str) -> None:
    assert normalize_message(raw) == expected


def test_clean_line_strips_ansi_and_the_runner_timestamp() -> None:
    assert (
        clean_line("2026-09-08T10:16:41.3574570Z \x1b[31mFAILED tests/t.py::t\x1b[0m")
        == "FAILED tests/t.py::t"
    )


def test_anchor_patterns_are_the_investigator_set() -> None:
    """The fingerprint reads exactly the lines the Context Manager protects."""
    from src.integrations.cicd.agents.investigator import ANCHOR_PATTERNS as investigator_set

    assert investigator_set is ANCHOR_PATTERNS


# ---------------------------------------------------------------------------
# Extraction on the shipped fixture logs
# ---------------------------------------------------------------------------


def _log(repo_root: Path, scenario: str, job_id: int) -> str:
    path = repo_root / "fixtures" / "scenarios" / scenario / "logs" / f"job_{job_id}.txt"
    return path.read_text(encoding="utf-8")


def test_flaky_fixture_keys_on_the_first_failed_test(repo_root: Path) -> None:
    anchor = extract_anchor(_log(repo_root, "flaky_test", 601234890))
    assert anchor.test_id == "tests/test_scheduler.py::test_job_runs_within_deadline"
    assert anchor.exc_type == "AssertionError"
    assert normalize_message(anchor.message) == "job took <DUR>, expected < <DUR>"


def test_regression_fixture_reads_the_type_from_the_location_line(repo_root: Path) -> None:
    """A bare `assert a == b` carries no type on the `E` line or the summary suffix; pytest
    puts it on `tests/test_x.py:34: AssertionError`."""
    anchor = extract_anchor(_log(repo_root, "real_regression", 601234567))
    assert anchor.test_id == "tests/test_pricing.py::test_discount_applies"
    assert anchor.exc_type == "AssertionError"
    assert anchor.message == "assert 91 == 90"


def test_infra_fixture_without_pytest_keys_on_the_last_exception(repo_root: Path) -> None:
    anchor = extract_anchor(_log(repo_root, "infra_timeout", 601235102))
    assert anchor.test_id is None
    assert anchor.exc_type == "ModuleNotFoundError"
    assert "No module named" in anchor.message


def test_empty_log_has_a_fingerprint() -> None:
    anchor = extract_anchor("")
    assert anchor.exc_type == "<none>"
    assert fingerprint_for(anchor)


def test_subject_key_shape(repo_root: Path) -> None:
    k = signature_key_for(
        repo=REPO, workflow_name="CI", job_name="test (3.12)",
        log_text=_log(repo_root, "flaky_test", 601234890),
    )
    assert k.scope == "repo:octo-org/harness-demo-repo"
    assert k.subject_key == "CI|test (3.12)|tests/test_scheduler.py::test_job_runs_within_deadline"
    assert len(k.fingerprint) == 64


@pytest.mark.parametrize(
    ("scenario", "job_id", "budget"),
    [
        ("flaky_test", 601234890, 60_000),
        ("real_regression", 601234567, 60_000),
        ("infra_timeout", 601235102, 25_000),
    ],
)
def test_key_from_the_budgeted_excerpt_matches_the_raw_log(
    repo_root: Path, scenario: str, job_id: int, budget: int
) -> None:
    """Dispatch decision 6: anchors are never trimmed, so the excerpt yields the same key.

    A budget well under each log's size forces real trimming (hundreds to thousands of
    lines elided); the key still agrees because every line the fingerprint reads survived.
    """
    raw = _log(repo_root, scenario, job_id)
    manager = ContextManager(default_budget=ContextBudget(total_chars=budget))
    assembled = manager.assemble(
        ContextRequest(
            sections=[Section(key="log", content=raw, priority=10)],
            budget=ContextBudget(total_chars=budget),
            anchor_patterns=list(ANCHOR_PATTERNS),
        )
    )
    excerpt = assembled.per_section["log"]
    assert assembled.truncation["log"].kept_lines < assembled.truncation["log"].original_lines
    assert assembled.truncation["log"].anchors_dropped == 0
    assert anchor_lines(excerpt) == anchor_lines(raw)
    assert signature_key_for(
        repo=REPO, workflow_name="CI", job_name="j", log_text=excerpt
    ) == signature_key_for(repo=REPO, workflow_name="CI", job_name="j", log_text=raw)
