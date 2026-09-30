"""How many Gemini requests has today's Pacific day spent? **Costs no request.**

    uv run python scripts/quota_ledger.py [--db ./data/harness.db ...] [--space URL]
                                          [--since-utc 2026-09-17T07:00:00+00:00]

The free tier allows 20 `generate_content` requests per model per Pacific day, and a
request Google answers `503` or `429` counts like a success. Every attempt the harness
makes is an `llm.attempt` span, so the day's spend is the number of those spans started
since midnight Pacific, whatever their status:

- `--db PATH` (repeatable; default `./data/harness.db`) counts a local database, opened
  read-only.
- `--space URL` counts a deployed instance through its public API: every run listed by
  `GET /v1/runs` since the day began, then each run's trace. Runs anyone started count;
  each is listed with who asked for it, so a stranger's replay shows up here.

Prints the total, the `ok` count, the errors by type (`LlmUpstreamError` is a `503`,
`LlmRateLimited` a `429`, `invalid_output` a reply that failed the schema), the split by
agent schema, the attempts per run, and `remaining = 20 - total`, with a warning from 16.

What it cannot see: the probe (`scripts/probe_gemini.py`), an eval's temporary
databases, and anything else on the same key. Add those by hand, and cross-check
against Google AI Studio's usage page when it is available.

No span records a model name, so this is one pool, named by `HARNESS_GEMINI_MODEL`. If a
per-agent `HARNESS_MODEL_*` override names another model, the single count is wrong and
a warning says so.

The day starts at 00:00 America/Los_Angeles: 07:00 UTC under PDT (12:30 IST), 08:00 UTC
under PST (13:30 IST). The US rule is hand-coded -- PDT runs from the second Sunday of
March at 02:00 to the first Sunday of November at 02:00 -- because `zoneinfo` has no
database on Windows and `tzdata` is not a dependency. `--since-utc` overrides it.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.settings import get_settings  # noqa: E402

DAILY_LIMIT = 20
WARN_AT = 16
PST = timedelta(hours=-8)
PDT = timedelta(hours=-7)


def nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def pacific_offset(at: datetime) -> timedelta:
    """UTC offset of US Pacific time at the instant `at` (timezone-aware)."""
    at = at.astimezone(UTC)
    # 02:00 PST is 10:00 UTC; 02:00 PDT is 09:00 UTC.
    pdt_from = datetime.combine(nth_sunday(at.year, 3, 2), time(10), tzinfo=UTC)
    pdt_until = datetime.combine(nth_sunday(at.year, 11, 1), time(9), tzinfo=UTC)
    return PDT if pdt_from <= at < pdt_until else PST


def pacific_day_start(now: datetime) -> datetime:
    """The UTC instant of the most recent midnight in US Pacific time."""
    local_day = (now.astimezone(UTC) + pacific_offset(now)).date()
    # A midnight is never inside a transition (they happen at 02:00), so the offset in
    # force at that midnight decides it: try PST, fall back to PDT.
    as_pst = datetime.combine(local_day, time(8), tzinfo=UTC)
    if pacific_offset(as_pst) == PST:
        return as_pst
    return datetime.combine(local_day, time(7), tzinfo=UTC)


def bind_form(at: datetime) -> str:
    """`at` as the stored `started_at` column spells it, to the second.

    The column holds `2026-09-17T18:10:14.704366+00:00`. A bound value with `+00:00` sorts
    before any fractional second of the same second (`+` < `.`); a `Z` suffix would sort
    after it and drop the rows inside the boundary second.
    """
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse_utc(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"{text!r} names no UTC offset")
    return parsed.astimezone(UTC)


@dataclass
class Tally:
    total: int = 0
    ok: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    schemas: Counter[str] = field(default_factory=Counter)
    runs: Counter[str] = field(default_factory=Counter)

    def add(self, run_id: str, status: str, error_type: str | None, schema: str | None) -> None:
        self.total += 1
        self.runs[run_id] += 1
        self.schemas[schema or "?"] += 1
        if status == "ok":
            self.ok += 1
        else:
            self.errors[error_type or "?"] += 1


def count_db(path: Path, start: datetime, tally: Tally) -> str:
    if not path.is_file():
        return f"WARNING: {path} does not exist; nothing counted from it"
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute(
            "SELECT run_id, status, json_extract(error_json, '$.type'),"
            " json_extract(attributes_json, '$.schema')"
            " FROM trace_span WHERE name = 'llm.attempt' AND started_at >= :start",
            {"start": bind_form(start)},
        ).fetchall()
    for run_id, status, error_type, schema in rows:
        tally.add(f"{path.name}:{run_id}", status, error_type, schema)
    return f"{path}: {len(rows)} attempt(s)"


def count_space(client: httpx.Client, start: datetime, tally: Tally) -> list[str]:
    """Every run the instance lists since `start` (newest first, stop at the first older
    one), then each run's `llm.attempt` spans since `start`."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/v1/runs", params=params)
        response.raise_for_status()
        page = response.json()
        older = False
        for item in page["items"]:
            if parse_utc(item["created_at"]) < start:
                older = True
                break
            items.append(item)
        cursor = page.get("next_cursor")
        if older or not cursor:
            break

    lines: list[str] = []
    for item in items:
        run_id = item["run_id"]
        response = client.get(f"/v1/runs/{run_id}/trace")
        spans: list[dict[str, Any]] = (
            response.json()["spans"] if response.status_code == 200 else []
        )
        requested_by = next(
            (s.get("attributes", {}).get("requested_by") for s in spans if s["name"] == "run"),
            None,
        )
        attempts = 0
        for span in spans:
            if span["name"] != "llm.attempt" or parse_utc(span["started_at"]) < start:
                continue
            attempts += 1
            error = span.get("error") or {}
            tally.add(
                f"space:{run_id}", span["status"], error.get("type"),
                span.get("attributes", {}).get("schema"),
            )
        lines.append(
            f"  {run_id}  {item['created_at']}  {item['status']:<18} "
            f"{attempts:>2} attempt(s)  requested_by={requested_by}"
        )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", action="append", type=Path, default=None)
    parser.add_argument("--space", metavar="URL", default=None)
    parser.add_argument("--since-utc", default=None, help="override the Pacific-day start")
    args = parser.parse_args()

    start = parse_utc(args.since_utc) if args.since_utc else pacific_day_start(datetime.now(UTC))
    settings = get_settings()
    pool = settings.gemini_model
    print(f"day start: {bind_form(start)}   pool: {pool}")
    overrides = {
        name: value
        for name in ("model_investigator", "model_diagnostician", "model_remediator")
        if (value := getattr(settings, name)) and value != pool
    }
    if overrides:
        print(f"WARNING: per-agent model overrides {overrides} differ from {pool}; "
              "the single-pool count below mixes models")

    tally = Tally()
    for path in args.db or [Path("./data/harness.db")]:
        print(count_db(path, start, tally))
    if args.space:
        with httpx.Client(base_url=args.space.rstrip("/"), timeout=60.0) as client:
            lines = count_space(client, start, tally)
        print(f"{args.space}: {len(lines)} run(s) since the day began")
        for line in lines:
            print(line)

    errors = ", ".join(f"{kind} {n}" for kind, n in sorted(tally.errors.items())) or "none"
    schemas = ", ".join(f"{name} {n}" for name, n in sorted(tally.schemas.items())) or "none"
    print(f"total {tally.total}   ok {tally.ok}   errors: {errors}")
    print(f"by schema: {schemas}")
    if tally.runs:
        print("by run: " + ", ".join(f"{run} {n}" for run, n in tally.runs.most_common()))
    remaining = DAILY_LIMIT - tally.total
    print(f"remaining {remaining} of {DAILY_LIMIT} (probe and eval DBs not included)")
    if tally.total >= WARN_AT:
        print(f"WARNING: {tally.total} >= {WARN_AT}: stop, and skip the optional steps")
    heavy = [run for run, n in tally.runs.items() if n >= 7]
    if heavy:
        print(f"WARNING: run(s) spent >= 7 requests (schema retries stacking): {heavy}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
