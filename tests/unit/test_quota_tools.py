"""Step-5 plan Stage 1b/1c: `scripts/quota_ledger.py` and `scripts/probe_gemini.py`.

The ledger's Pacific day is hand-coded (no `tzdata` on Windows), so the US rule is pinned
at both 2026 transitions. Its local count binds the day start in the column's own
`+00:00` form, pinned at the boundary second. The probe's scrub is pinned against the
`AQ.`-shaped key the deployment uses, which the app's own patterns miss (SEC-13).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest


def load(repo_root: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, repo_root / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ledger(repo_root: Path) -> ModuleType:
    return load(repo_root, "quota_ledger")


@pytest.fixture
def probe(repo_root: Path) -> ModuleType:
    return load(repo_root, "probe_gemini")


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "day_start"),
    [
        # PST before the spring change; 2026-03-08 is its second Sunday.
        ("2026-03-08T07:59:00", "2026-03-07T08:00:00"),
        ("2026-03-08T08:00:00", "2026-03-08T08:00:00"),
        # 10:00 UTC is 02:00 PST: PDT from here, but that day's midnight was PST.
        ("2026-03-08T12:00:00", "2026-03-08T08:00:00"),
        ("2026-03-09T06:59:00", "2026-03-08T08:00:00"),
        ("2026-03-09T07:00:00", "2026-03-09T07:00:00"),
        # Today, under PDT: 12:30 IST.
        ("2026-10-01T05:00:00", "2026-09-30T07:00:00"),
        ("2026-10-01T07:00:00", "2026-10-01T07:00:00"),
        # 2026-11-01 is the first Sunday of November; its midnight is still PDT.
        ("2026-11-01T07:30:00", "2026-11-01T07:00:00"),
        # 09:00 UTC is 02:00 PDT -> 01:00 PST; the same Pacific day continues.
        ("2026-11-01T09:30:00", "2026-11-01T07:00:00"),
        ("2026-11-02T07:59:00", "2026-11-01T07:00:00"),
        # From here the day starts at 08:00 UTC: 13:30 IST.
        ("2026-11-02T08:00:00", "2026-11-02T08:00:00"),
    ],
)
def test_the_pacific_day_starts_at_local_midnight(
    ledger: ModuleType, now: str, day_start: str
) -> None:
    assert ledger.pacific_day_start(utc(now)) == utc(day_start)


def make_db(path: Path, repo_root: Path, spans: list[tuple[str, str, str | None, str]]) -> None:
    sql = (repo_root / "src/harness/migrations/001_init.sql").read_text(encoding="utf-8")
    with sqlite3.connect(path) as conn:
        conn.executescript(sql)
        for index, (started_at, status, error_type, schema) in enumerate(spans):
            conn.execute(
                "INSERT INTO trace_span (span_id, run_id, name, component, status, started_at,"
                " attributes_json, error_json) VALUES (?, ?, 'llm.attempt', 'llm', ?, ?, ?, ?)",
                (
                    f"span_{index}", "run_a" if index < 3 else "run_b", status, started_at,
                    json.dumps({"schema": schema}),
                    json.dumps({"type": error_type}) if error_type else None,
                ),
            )
        conn.execute(
            "INSERT INTO trace_span (span_id, run_id, name, component, status, started_at)"
            " VALUES ('not_an_attempt', 'run_a', 'agent.run', 'agent', 'ok',"
            " '2026-09-17T09:00:00.000000+00:00')"
        )


def test_the_local_count_includes_the_boundary_second_and_counts_every_status(
    ledger: ModuleType, repo_root: Path, tmp_path: Path
) -> None:
    db = tmp_path / "ledger.db"
    make_db(db, repo_root, [
        ("2026-09-17T06:59:59.999999+00:00", "ok", None, "InvestigationNotes"),  # yesterday
        ("2026-09-17T07:00:00+00:00", "error", "LlmUpstreamError", "InvestigationNotes"),
        ("2026-09-17T07:00:00.000001+00:00", "ok", None, "InvestigationNotes"),
        ("2026-09-17T08:00:00.5+00:00", "error", "invalid_output", "Diagnosis"),
        ("2026-09-17T08:00:01.5+00:00", "error", "LlmRateLimited", "Diagnosis"),
    ])
    tally = ledger.Tally()

    ledger.count_db(db, utc("2026-09-17T07:00:00"), tally)

    assert ledger.bind_form(utc("2026-09-17T07:00:00")) == "2026-09-17T07:00:00+00:00"
    assert tally.total == 4
    assert tally.ok == 1
    assert dict(tally.errors) == {
        "LlmUpstreamError": 1, "invalid_output": 1, "LlmRateLimited": 1,
    }
    assert dict(tally.schemas) == {"InvestigationNotes": 2, "Diagnosis": 2}


def test_the_space_count_pages_to_the_day_start_and_names_who_asked(
    ledger: ModuleType,
) -> None:
    start = utc("2026-09-17T07:00:00")
    runs = [
        {"run_id": "run_3", "status": "completed", "created_at": "2026-09-17T09:00:00+00:00"},
        {"run_id": "run_2", "status": "escalated", "created_at": "2026-09-17T08:00:00+00:00"},
        {"run_id": "run_1", "status": "completed", "created_at": "2026-09-16T20:00:00+00:00"},
    ]
    traces = {
        "run_3": [
            {"name": "run", "status": "ok", "started_at": "2026-09-17T09:00:00Z",
             "attributes": {"requested_by": "webhook:github:guid"}},
            {"name": "llm.attempt", "status": "ok", "started_at": "2026-09-17T09:00:01Z",
             "attributes": {"schema": "InvestigationNotes"}},
        ],
        "run_2": [
            {"name": "run", "status": "ok", "started_at": "2026-09-17T08:00:00Z",
             "attributes": {"requested_by": "replay:anonymous"}},
            {"name": "llm.attempt", "status": "error", "started_at": "2026-09-17T08:00:01Z",
             "attributes": {"schema": "InvestigationNotes"},
             "error": {"type": "LlmUpstreamError"}},
        ],
    }
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url.copy_with(query=None).path))
        if request.url.path == "/v1/runs":
            cursor = request.url.params.get("cursor")
            page = runs[:2] if cursor is None else runs[2:]
            return httpx.Response(200, json={
                "items": page, "next_cursor": "run_2" if cursor is None else None,
            })
        run_id = request.url.path.split("/")[3]
        body: dict[str, Any] = {"run_id": run_id, "spans": traces[run_id]}
        return httpx.Response(200, json=body)

    tally = ledger.Tally()
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://space") as client:
        lines = ledger.count_space(client, start, tally)

    assert tally.total == 2 and tally.ok == 1
    assert dict(tally.errors) == {"LlmUpstreamError": 1}
    assert "/v1/runs/run_1/trace" not in requested  # older than the day: never read
    assert any("replay:anonymous" in line for line in lines)
    assert any("webhook:github:guid" in line for line in lines)


def test_the_probe_scrubs_the_stripped_key_and_both_key_shapes(probe: ModuleType) -> None:
    deployed = "AQ." + "Ab1_-" * 10  # 53 characters, the deployed key's shape
    registered = f"  {deployed}\n"  # as a pasted Space secret might hold it
    other = "AIza" + "B" * 35
    message = f"403 key {deployed} rejected; also saw {other} and AQ.{'c' * 30}"

    scrubbed = probe.scrub(message, registered)

    assert deployed not in scrubbed
    assert other not in scrubbed
    assert "AQ.ccc" not in scrubbed
    assert scrubbed.startswith("403 key *** rejected")
