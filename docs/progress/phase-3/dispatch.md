# Phase 3 — dispatch decisions (written BEFORE the build)

Written 2026-09-11 at `afc621b`, the tree after `phase-2-green`. Normative for this phase.
Where this file and an implementer's instinct disagree, this file wins. Settled here so the
audit can check the build against a stated intent rather than reverse-engineer one.

## How this phase is being run

**Inline by the coordinator**, as Phase 2 was (`docs/progress/phase-2/dispatch.md`, "How this
phase is being run"). Nothing runs in parallel, so no agent needs another's territory while
it is busy; `phase-reviewer` is dispatched once, after the build, with named suspicions. The
gate verdict (`test-verifier.md`) is coordinator-run and recorded as such.

## The two things the plan says to settle first

### 1. The live target has no persistent disk — the demo of cross-run memory runs locally

`PLAN.md` Phase 3's amendment and Open Risk 2 both say: decide before building. Decided:
**`SqliteMemoryStore` is built exactly as planned, and Verify step 2 is demonstrated locally
and under Docker.** On the Space, memory is real but amnesiac across restarts and sleeps,
which the handoff already documents; four replays *within one waking period* still show the
fourth sighting reading `occurrences=3`, and that is what the Space can honestly show.

Not chosen: a persistent volume (a paid tier) or `PostgresMemoryStore` (Supabase). Both are
the user's call and cost money or a second storage backend; the `MemoryStore` protocol keeps
either to a one-class change, and this phase does nothing that makes that harder.

### 2. This phase is where the retry cap becomes real

`memory.retries_for_signature_24h` is read off `bundle.prior_history.retries_in_24h`
(Phase 2 decision 2). Nothing in the Remediator changes: the Investigator now puts a real
count in the bundle, computed from the store's `actions_in_window["rerun_failed_jobs"]`
over 24 h. An unreadable history still yields `PriorHistory(unavailable=True)` and therefore
`999` — the structural rule in `schemas.py` is not touched.

## Decisions

### 3. One schema, one owner: `src/harness/storage.py` + `migrations/001_init.sql`

The recorder owned the only schema (`spans`, created by `TraceRecorder.initialize()`), and
PLAN's `001_init.sql` names `trace_span` with different columns. The handoff says: do not
leave two span tables. **The recorder migrates to `trace_span`**, and the whole schema is
owned by the migration files, applied by `storage.apply_migrations()` and tracked in
`schema_version`. `TraceRecorder.initialize()` and `SqliteMemoryStore.initialize()` both call
the same runner (idempotent), so every existing caller keeps working. A legacy `spans` table
found at migration time has its rows copied into `trace_span` and is dropped — a one-off
step in the runner, in Python, because SQL alone cannot say "if this table exists".

`storage.py` is a new harness module (not in PLAN's layout): the SQLite plumbing shared by
`observability.py` and `memory.py` — pragmas (`journal_mode=WAL` persisted at migration,
`busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON` per connection), the
migration runner, and the B.3 "file is not a database" recovery (rename to
`harness.db.corrupt.<ts>`, recreate). It exists so `observability` need not import `memory`
(which imports `orchestrator`, which imports `observability` — a cycle). Recorded as a
layout addition.

### 4. `MemoryStore` gains the read side and the approval side — A.5 amended additively

A.5 lists the write and claim methods. The `run` and `approval` tables exist to serve
`GET /v1/runs/{id}`, `GET /v1/runs`, `GET /v1/escalations` and `POST /v1/approvals/{id}`, and
`run_registry.py` / `approval_registry.py` say in their own docstrings that they are replaced
in this phase. So the protocol gains, additively: `get_run`, `list_runs` (keyset cursor on
`run_id`, which is ULID-shaped), `list_escalations`, `save_approval`, `get_approval`,
`decide_approval`, and a harness model `ApprovalRecord` whose `plan` and `context` are
opaque JSON — the store never learns what a `RemediationPlan` is. `approval` gains a
`context_json` column (the handoff's "columns or a sidecar"): the API layer's `RunContext`
(mode, repo, scenario dir) is what the approve route needs to rebuild the gateway, and it is
an API-layer notion, so it rides as opaque JSON on the approval rather than as columns the
harness would have to name.

Both in-process registries are deleted. `_background_runs` / `_supervised` / `_spawn_run`
stay: they hold the *task* (asyncio holds tasks weakly), which no store can do; `_supervised`
now records a failure through `save_run`.

### 5. The claim protocol is wired, and `fresh` means what it says

`POST /v1/runs` claims `RunRequest.idempotency_key` per Appendix C's table: a completed /
escalated / failed original answers `200` with `status="deduplicated"` and
`original_run_id`; an `awaiting_approval` original answers `200` with its outcome (same
approval id); a fresh `in_progress` original answers `202`; a stale one (heartbeat older than
120 s) is taken over. The dedup response keeps the original's `run_id` — no new run exists.

`POST /v1/replay/{scenario}` keeps `fresh=true` as its default and gives it a meaning: the
claim is made under `<key>#fresh:<nonce>` so the demo path always re-runs (the Verify block's
four consecutive replays are four real runs). `fresh=false` claims the webhook's real key and
dedupes like a redelivery. The store mints the run id (`RunClaim.run_id`) and the route hands
it to the orchestrator, as `mint_run_id` did.

The **heartbeat is the orchestrator's** (Appendix C: "the orchestrator writes `heartbeat_at`
every 15 s"): `Orchestrator` takes an optional `memory` and runs a heartbeat task for the
duration of `run()`. A heartbeat failure is logged, never raised.

A superseded run read back through `get_run` is served as `failed` with a `run_timeout`
escalation naming the run that took over — `RunOutcome.status` (A.1, frozen) has no
`superseded` member and the heartbeat going stale *is* the run timing out from the store's
point of view.

### 6. Fingerprints are computed from anchor lines only

PLAN's step 1 says "from the anchor lines". Taken literally, and load-bearing: the Context
Manager never trims an anchor line, so a fingerprint computed from the raw log and one
computed from the budgeted excerpt agree whenever no anchor was dropped. The Investigator
computes the key from the raw log; the key travels on the bundle (decision 8) so nothing
downstream has to recompute it.

For a pytest log the subject is the **first** `FAILED <nodeid>` line and the exception is the
first `E   Type: message` line (pytest prints failure sections in summary order); for
anything else, the **last** `Type: message` line, falling back to the last anchor line. ANSI
escapes and the Actions timestamp prefix are stripped first. Normalisation follows PLAN step
3 with one ordering choice stated: durations are replaced **before** integers, because
`1.207s` → `1.<N>s` would leave nothing for the `<DUR>` rule to match; and `[gwN]`
(pytest-xdist worker ids) → `<WORKER>`, which PLAN's stability test names but its rule list
does not.

### 7. The flakiness prior: the number lives in the harness, the words in the integration

PLAN says `prior_hint` is "computed in `memory.py`, not by the model", and the rule names
`flaky_test`, `real_regression` and `passed_on_retry`. The harness must not name verdicts, so
the split is: `memory.dominant_verdict(record, min_occurrences, min_share)` — the numeric
rule, over the constants already in `memory.py` — and `memory.has_outcome(recent, outcome)`;
the integration (`history.py`, new) maps a dominant `flaky_test` plus a `passed_on_retry`
observation to `likely_flaky`, a dominant `real_regression` to `likely_real`, else `unknown`.
The Diagnostician's `memory_agreement` (+0.10) fires when the prior's dominant verdict equals
the category and the history is readable; it is never applied when `unavailable`.

### 8. `PriorHistory.key` — A.11 amended additively

`PriorHistory` gains `key: SignatureKey | None = None`. The Investigator always computes it
(pure; no store needed), so the Diagnostician can `upsert_signature` and the Remediator can
`record_observation` without recomputing, and the approval route can **re-query memory for a
fresh count** rather than reuse the stored `prior_history` (handoff §4). A bundle whose
`key` is `None` (a run investigated before this phase) re-evaluates as `unavailable` → 999.

### 9. Who writes what to memory, and when

- **Investigator**: `lookup`, then resolves pending outcomes (decision 10), then populates
  `PriorHistory`. Any failure → `PriorHistory(key=..., unavailable=True)`,
  `degraded += ["memory"]`, and the prompts say the history was unavailable.
- **Diagnostician**: after calibration, `upsert_signature(key, category, run_id)` and
  `record_observation` with the verdict and confidence and no action. Every diagnosed run
  counts, gated or not — `confidence` is on the row so a reader can weigh it. Failure →
  `degraded += ["memory"]`, the diagnosis stands.
- **Remediator**: after a side-effecting plan executes (dry-run included — the harness
  *decided* to act, and the cap must be demonstrable without writes), re-records the same
  observation with `action_taken` = the plan's terminal write tool (`rerun_failed_jobs`,
  `create_issue`, `open_pull_request`) and `action_outcome="pending"`. This is the
  `record_observation` obligation on `retry-suspected-flaky` acted upon; it is done for every
  executed write regardless of the YAML obligation, because the retry count must not depend
  on a policy file remembering to ask for it. `annotate_run` remains recorded, not executed.

`observation_id` is deterministic — `sha256(signature_id|run_id)` — so the two writes are one
row and a retry inside a run cannot double-insert.

### 10. `pending` outcomes are resolved by asking about the rerun

"`action_outcome='pending'` until the re-run's result is known." Phase 5's success webhook
is one way to learn it; this phase adds another that needs no new input: at the next sighting
of a signature, for each recent observation still `pending` with `action_taken ==
"rerun_failed_jobs"`, the Investigator reads the original bundle back from the `run` table,
and probes `list_workflow_run_jobs(run_id, attempt + 1)`. All jobs `success` →
`passed_on_retry`; any `failure` → `failed_again`; anything else (404, still running) →
still pending. At most three probes per run; a probe failure is data, never degradation.

This is what makes Verify step 2's `likely_flaky` reachable honestly: the `flaky_test`
fixture gains `api/GET_…-runs-501234890-attempts-2-jobs.json` recording that the rerun
went green — the scenario's own ground truth ("the right action is to re-run the job").
`infra_timeout` deliberately has no such file, so its retries stay `pending`.

### 11. The Verify block, as this phase can honestly meet it — PLAN amended

- **Step 2** passes as written, via decision 10.
- **Step 3** must start from a fresh database (or run before step 2): step 2's first two
  runs already executed two retries inside the window, so a fifth run denies. The four runs
  of step 2 *are* `allow, allow, deny, deny` and the sequence is recorded from them.
- **Step 4** expects `{"status":"completed","degraded":["memory"]}`. Under Phase 2's
  decision 3 (a denied plan escalates `policy_denied`) and B.3's fail-closed cap, a
  memory outage on `flaky_test` yields `status: "escalated"`, `escalation.reason:
  "policy_denied"` naming `memory.retries_for_signature_24h: 999`, and `degraded:
  ["memory"]`. That *is* "degrades, does not fail": the diagnosis is produced, the outage is
  visible, and the one thing the harness refuses to do is act on a cap it cannot verify.
  The expectation is amended to that.
- `HARNESS_FAULT_INJECT=sqlite_locked` makes every store operation raise `database is
  locked` before the B.3 ladder (100/200/400 ms), so the ladder is exercised and each call
  degrades in ~0.7 s. Migrations are exempt (the process must boot). Refused when
  `HARNESS_ENV != dev`, at composition time.

### 12. Fixture labels flip to what the phase determines

`flaky_test` and `infra_timeout` carry `effect: allow` (a first sighting under an empty
history allows the retry — decision 12 of Phase 2 said this phase flips it). `cold_start` is
a copy of `flaky_test` with an empty `find_last_successful_run` body, labelled
`baseline_kind: none`, `cold_start: true`, `effect: deny`.

### 13. Escalations are durable, on the `db` channel

`save_run` writes an `escalation` row when the outcome carries one (`channel="db"`), and
`GET /v1/escalations` reads that table. Orchestrators built by the composition root declare
`escalation_channels=("log", "db")` so the record says where it went.

## Territory map for this phase (coordinator writes all of it)

| Area | Files |
|---|---|
| harness | `storage.py` (new), `migrations/001_init.sql` (new), `memory.py`, `observability.py`, `orchestrator.py` |
| integration | `fingerprint.py`, `history.py` (new), `schemas.py` (`PriorHistory.key`), `agents/{investigator,diagnostician,remediator}.py`, `prompts/{investigator,diagnostician}.md`, `rendering.py`, `wiring.py` |
| api | `deps.py`, `main.py`; `run_registry.py` and `approval_registry.py` deleted; `app.py` |
| fixtures / scripts | `cold_start/`, `flaky_test/api/...attempts-2-jobs.json`, `scenario.yaml` labels, `scripts/replay.py` |
| tests | `test_fingerprint.py`, `test_memory_store.py`, `test_memory_e2e.py`, `test_storage_migrations.py`, updates to the approvals / supervision / error-body tests |
| docs | this file, `verify.md`, `test-verifier.md`, `review.md` (reviewer), `backlog.md`, PLAN.md amendments |
