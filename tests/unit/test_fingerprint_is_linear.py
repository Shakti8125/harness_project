"""SEC-09: fingerprinting a hostile CI log costs linear time, and no signature moved.

`_EXCEPTION.search` restarted inside every identifier, so one `Error: ` line followed by
16,000 identifier characters took about 24 s; pytest's section-header pattern was cubic
in a run of spaces. A log is whatever a commit's test run prints, up to 2 MiB, and the
fingerprint ran synchronously on the event loop. The five scenarios' fingerprints are
pinned at their pre-fix values: a signature that moved would orphan every memory row
recorded under it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.integrations.cicd.fingerprint import extract_anchor, fingerprint_for

PINNED = {
    "cold_start": ("tests/test_scheduler.py::test_job_runs_within_deadline",
                   "675e00782527ddce87743de24d29e55507d1dadf3411f2705511eb4db8fe35ec"),
    "dependency_break": (None, "747074f5ec9407f03eae79761e6ea7eed0f559d5d10d6b960cf11cded92b1ef5"),
    "flaky_test": ("tests/test_scheduler.py::test_job_runs_within_deadline",
                   "675e00782527ddce87743de24d29e55507d1dadf3411f2705511eb4db8fe35ec"),
    "infra_timeout": (None, "f2b5a3317e8802780c4c5ebe2fe1f38c213f889a5d7c8e9abab7ed09b0e16f92"),
    "real_regression": ("tests/test_pricing.py::test_discount_applies",
                        "7b5cb7cc870feb38192f5ec911958be6e6ac2153da82224a03a41d9cd7952e0d"),
}


@pytest.mark.parametrize("scenario", sorted(PINNED))
def test_every_scenario_keeps_its_fingerprint(repo_root: Path, scenario: str) -> None:
    (log,) = sorted((repo_root / "fixtures/scenarios" / scenario / "logs").glob("*.txt"))
    anchor = extract_anchor(log.read_text(encoding="utf-8"))
    assert (anchor.test_id, fingerprint_for(anchor)) == PINNED[scenario]


def hostile_log() -> str:
    lines = [
        "FAILED tests/test_x.py::test_x",
        "___" + " " * 800 + "x AssertionError",
        "____________ test_x ____________",
    ]
    identifier_line = "Error: " + "a" * 16_000
    while sum(len(line) + 1 for line in lines) < 2 * 1024 * 1024:
        lines.append(identifier_line)
        lines.append("___" + " " * 800 + "x AssertionError")
    return "\n".join(lines)


def test_a_two_mebibyte_hostile_log_fingerprints_in_under_a_second() -> None:
    log = hostile_log()

    started = time.perf_counter()
    anchor = extract_anchor(log)
    fingerprint_for(anchor)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"{elapsed:.2f} s"
    assert anchor.test_id == "tests/test_x.py::test_x"


def test_a_hostile_log_without_a_failed_line_fingerprints_in_under_a_second() -> None:
    log = "\n".join(["Error: " + "a" * 16_000] * 131)

    started = time.perf_counter()
    fingerprint_for(extract_anchor(log))
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"{elapsed:.2f} s"
