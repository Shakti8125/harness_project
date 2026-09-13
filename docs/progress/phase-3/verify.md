# Phase 3 — Verify block, literal expected vs actual

Run 2026-09-13 against `66208d0` (the build commit; not re-run after the audit's fix round --
`backlog.md`, "Audit provenance", says why the values stand), locally: `uv run uvicorn
src.api.main:app` on `127.0.0.1:8000` over a **fresh** `./data/harness.db` (the previous
file was backed up first), with the real Gemini key from `.env`. The Phase 2 Docker
container that was still listening on `0.0.0.0:8000` from two days earlier was stopped
first — `localhost:8000` had been resolving to it, which is why the first `readyz` read
showed the Phase 2 body. Live quota spent: **15 model calls** (five replays, one attempt
per stage, no Recovery retries).

The Verify block as amended by this phase's PLAN.md note (item 7): step 3 is recorded from
the same four runs as step 2, and step 4's expectation is the escalated/degraded shape.

## Step 1 — fingerprint stability

```
uv run pytest tests/unit/test_fingerprint.py -q
25 passed in 0.61s
```

`test_stable_across_noise` (timestamps, temp paths, durations, xdist worker ids, addresses,
UUIDs, shas differ → same `signature_id`) and `test_distinguishes_real_difference`
(`AssertionError` vs `TimeoutError` → different ids) both pass, plus the excerpt-equals-raw
property on all three fixture logs under a budget that trims hundreds of lines.

## Step 2 — repeat flakiness is recognized across runs

Four `POST /v1/replay/flaky_test?fresh=1`, sequential:

| run | status | escalation | category | conf | occ | hint | retries_24h | effect | remediation | ms | attempts |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `run_01M2CXY2WY…` | completed | — | flaky_test | 0.95 | 0 | unknown | 0 | allow | executed | 32 859 | 1,1,1 |
| `run_01M2CXZ387…` | completed | — | flaky_test | 0.95 | 1 | unknown | 1 | allow | executed | 34 016 | 1,1,1 |
| `run_01M2CY0ED4…` | escalated | policy_denied | flaky_test | 0.95 | 2 | unknown | 2 | deny | denied | 33 329 | 1,1,1 |
| `run_01M2CY1F4M…` | escalated | policy_denied | flaky_test | **0.99** | **3** | **likely_flaky** | 2 | deny | denied | 35 609 | 1,1,1 |

The block's own `jq` on the fourth run:

```
{"occ":3,"hint":"likely_flaky","adj":[0.1]}        # EXPECT {"occ":3,"hint":"likely_flaky","adj":[0.10]}   ✓
```

`confidence_adjustments` on the fourth run: `[{"name":"memory_agreement","delta":0.1,"reason":"3
prior sightings, 100% judged flaky_test"}]` — the model's 0.85 self-confidence calibrated to 0.95 on
runs 1–3 (no adjustment) and to 0.99 (the ceiling) on run 4.

```
sqlite3 ./data/harness.db "select occurrences, last_verdict, verdict_counts from failure_signature;"
4|flaky_test|{"flaky_test":4}                     # EXPECT 4|flaky_test|{"flaky_test":4}   ✓
```

The `observation` rows, showing dispatch decision 10 at work (the first two runs' pending
retries were resolved to `passed_on_retry` by the next sighting's probe of attempt 2):

```
run_01M2CXY2WY | flaky_test | 0.95 | rerun_failed_jobs | passed_on_retry
run_01M2CXZ387 | flaky_test | 0.95 | rerun_failed_jobs | passed_on_retry
run_01M2CY0ED4 | flaky_test | 0.95 |                   |
run_01M2CY1F4M | flaky_test | 0.99 |                   |
```

## Step 3 — the retry cap actually bites

Recorded from the same four runs (amendment item 7: the sequence needs a fresh database,
and step 2's is one):

```
allow, allow, deny, deny                           # EXPECT allow, allow, deny, deny   ✓
```

Run 3's decision reason, verbatim — rule and clause named:

```
no rule matched, default_effect is 'deny': 'retry-suspected-flaky' did not match
(memory.retries_for_signature_24h: 2 fails {lt: 2.0})
```

Not run live: the sequence from a *second* fresh database (it would spend 12 more calls to
reproduce what `tests/integration/test_memory_e2e.py::test_the_retry_cap_bites_on_the_third_run`
pins in-process, on a fresh file, with the same assertions).

## Step 4 — memory outage degrades, does not fail (as amended)

Server restarted with `HARNESS_FAULT_INJECT=sqlite_locked` (uvicorn, not `docker compose`:
the compose file does not forward that variable, and the Docker image is checked separately
below). `readyz` still `200` with `migrations_applied: true` — migrations are exempt from the
fault, and the readiness probe writes through its own connection.

```
POST /v1/replay/flaky_test  →  200 in 64 s
{"status":"escalated","degraded":["memory"]}       # amended EXPECT: escalated + ["memory"]   ✓
```

| field | actual |
|---|---|
| `escalation.reason` | `policy_denied` |
| `escalation.message` | `policy denied 'rerun_failed_jobs' via <default>: … 'retry-suspected-flaky' did not match (memory.retries_for_signature_24h: 999 fails {lt: 2.0})` |
| `final.diagnosis.category` | `flaky_test` (0.95; no `memory_agreement`) |
| `final.bundle.prior_history.unavailable` / `.retries_in_24h` / `.key` | `true` / `999` / present |
| `GET /v1/runs/{id}` | `503 application/problem+json`, `Retry-After: 5`, title "Memory store unavailable" |
| server log | 52 `database is locked` retry lines: the B.3 ladder ran on every store call |

The 64 s (vs ~34 s) is the ladder: 100/200/400 ms per store call, on the claim, the lookup,
the two Diagnostician writes and `save_run`, plus one more model round trip's worth of
variance.

## Free checks on the same server (no model calls)

| Check | Result |
|---|---|
| `GET /readyz` | `{"db_writable":true,"gemini_key_present":true,"policy_loaded":true,"migrations_applied":true}` |
| `GET /v1/escalations?limit=2` | runs 4 and 3, `reason: policy_denied`, `channels: ["log","db"]`, newest first |
| `GET /v1/runs?limit=2` | 2 items, `next_cursor` = the second item's id |
| `GET /v1/runs/{run 4}` | served from the `run` table: `escalated`, `occurrences: 3` |
| `GET /v1/runs/{run 4}/trace` | 12 spans (`run`, `agent.run`, `llm.attempt`, `prompt.render`, `policy.decide`, `remediation.plan`), 39 980 tokens |
| Served body | 8 009 bytes, zero credential-shaped matches |
| `POST /v1/approvals/apr_nope`, `POST /v1/replay/nope` | `404 application/problem+json` both |

## Docker

`docker compose up -d --build` on the same `./data` volume:

```
GET /healthz  {"status":"ok","db":"ok","version":"0.1.0"}
GET /readyz   {"db_writable":true,"gemini_key_present":true,"policy_loaded":true,"migrations_applied":true}  200
/app/src/harness/migrations  ['001_init.sql']        # the image ships the migration
GET /v1/runs?limit=1   the escalated run 4, read from the file the local server wrote
```

No replay was spent under Docker. The container was brought down afterwards.

## Not demonstrated live, pinned in-process

- `fresh=false` deduplication and `POST /v1/runs` redelivery
  (`test_replay_with_fresh_false_dedupes_a_redelivery`, `test_create_run_dedupes_by_idempotency_key`).
- The approval route re-querying memory and denying (`test_approval_re_evaluates_against_fresh_memory_and_can_deny`).
- The Appendix D `cold_start` replay (`test_cold_start_replay`): `baseline_kind: none`,
  `cold_start: true`, effect `deny` via the `context.cold_start` clause.
- The stale-heartbeat takeover (`tests/unit/test_memory_store.py::test_stale_heartbeat_is_taken_over`).
