# Phase 3 — audit

**VERDICT: FIX FIRST** — independent `phase-reviewer` pass over `de1365d` (read-only; the
agent returned its report and the coordinator recorded it here verbatim, entities decoded).
Ten findings: one high, two medium, seven low. Suspicions S1–S10 from the brief are answered
below.

**Verdict after the fix round: SHIP** — coordinator's call, on the basis below, and not
re-audited (one independent pass per phase; the Phase 2 precedent). Findings 1–9 are closed
in the same-day round, each with a test that reproduces the reviewer's confirming input and
was run against the pre-fix source to confirm it fails there (`test-verifier.md`, "Fix
round"); the contract-level consequences are PLAN.md Phase 3 amendment items 9–17. Finding
10 (low) is carried **open** in `backlog.md`: closing it is a reservation protocol between
three agents and the store, with its own failure mode, and is a decision for the phase that
next touches the retry path rather than a fix to slip in after an audit.

## Findings

1. [high] `src/harness/memory.py:552-556` — `claim_run`'s takeover-chain lookup
   (`idempotency_key = ? OR idempotency_key LIKE '<key>#%'`) also matches the demo path's
   `<key>#fresh:<nonce>` rows (`src/api/main.py:843-849`, `scripts/replay.py:172-174`), so
   replay runs sit in the real key's chain and tie on `attempt = 1`.
   Failure scenario (reproduced against the store): (a) one `POST /v1/replay/flaky_test`
   (default `fresh=1`, completed) → the real webhook arrives, `POST /v1/runs` claims `cicd:K`,
   runs → a redelivery 10 s later with a fresh heartbeat returns `200 status=deduplicated`
   with the *replay's* outcome and `original_run_id` = the replay run, where Appendix C
   requires `202 in_progress` naming the real run. (b) a fresh replay whose row is stuck
   `in_progress` (process killed mid-replay on a persistent volume) + the same webhook twice
   → the redelivery *takes over the dead replay row* (`took_over_from` = replay id, key
   `cicd:K#2`) and starts a second concurrent run for one idempotency key while the first is
   alive → duplicate model calls, duplicate observations, a second `rerun_failed_jobs`.
   `test_replay_with_fresh_false_dedupes_a_redelivery` avoids it only by ordering (`fresh=0`
   before `fresh=1`); `test_create_run_dedupes_by_idempotency_key` uses a key no replay shares.
   Owner: harness-core (chain match should be `#<digits>` only) / api-surface (`_fresh_key`) /
   fixtures-eval (`scripts/replay.py`)

2. [medium] `src/api/main.py:771-773` — the claim row is written in the route, but the
   orchestrator's heartbeat (the only writer of `heartbeat_at`) starts inside
   `orchestrator.run()`, *after* `context.run_semaphore` is acquired; a queued run has no
   liveness signal.
   Failure scenario: 5 webhooks in a burst with `max_concurrent_runs=4` and Gemini throttling
   stretching runs past 2 min (Open Risk 6), or 17+ at nominal 35 s → the queued claim's
   `heartbeat_at` is >120 s old while the run has not started → a redelivery (expected on the
   Space, Open Risk 11) takes it over → both the queued original and the takeover execute: two
   runs, two actions, `retries_in_24h` consumed twice for one failure.
   Owner: api-surface

3. [medium] `src/integrations/cicd/history.py:85-137`, `rendering.py:305-336` —
   `failed_again` is written by `resolve_pending_outcomes` and read by nothing: not
   `prior_hint_for`, not `memory_agrees`, not the rendered prior. PLAN Open Risk 7 states "an
   `action_outcome == 'failed_again'` observation pushes the flaky share below 0.6, so the
   prior self-corrects"; and `prompts/diagnostician.md` tells the model memory says "whether
   an automatic retry of it passed", but the rendered block never says one failed.
   Failure scenario: signature with 3× `flaky_test` and one `passed_on_retry` (`likely_flaky`);
   the test then genuinely breaks with the same fingerprint → run 4: hint `likely_flaky`,
   `memory_agreement` +0.10, retry executed → rerun fails → next sighting records
   `failed_again` → run 5: hint still `likely_flaky` (any `passed_on_retry` in 30 days
   suffices), +0.10 again, retry executed → cap denies the third, and the next day the same
   two retries fire again, indefinitely. The only brake left is the 2/24 h cap; the
   "self-correction" PLAN names does not exist.
   Owner: cicd-integration

4. [low] `src/integrations/cicd/agents/diagnostician.py:272-297` + `src/harness/memory.py:236-254`
   — every diagnosed run increments `occurrences`/`verdict_counts` with full weight; dispatch
   9's "confidence is on the row so a reader can weigh it" has no reader (`dominant_verdict`
   counts rows, ignores confidence).
   Failure scenario: three sightings the gate refused (`flaky_test`, self 0.55 →
   `low_confidence` escalations) → fourth sighting `flaky_test` at self 0.65 → `memory_agrees`
   → 0.75 → passes the 0.70 gate and the rule's `gte: 0.75` → auto-retry executed, on the
   strength of three verdicts the harness itself declined to act on. Without memory: escalated.
   Owner: cicd-integration

5. [low] `src/integrations/cicd/history.py:235-255` — `_probe_rerun` decides `passed_on_retry`
   from a single unpaginated `GET .../attempts/{n+1}/jobs` (`gateway_github.py:445-449` sends
   no `per_page`, follows no `Link`; GitHub's default page is 30).
   Failure scenario: attempt 2 has 31+ jobs (a matrix where many failed), page 1 all
   `success`, the failing job on page 2 → `conclusions == {"success"}` → `passed_on_retry`
   recorded for a rerun that failed → `likely_flaky` reached on false evidence.
   Owner: cicd-integration

6. [low] `src/integrations/cicd/fingerprint.py:161-182` — `_pytest_anchor`'s fallbacks (first
   `E` line; first `path:line: Type` location line) scan anchor lines from the top of the
   log, and pytest prints the ERRORS section before FAILURES (S8).
   Failure scenario (reproduced with `extract_anchor`): a setup `ERROR at setup of
   test_db_roundtrip` ending `tests/conftest.py:12: ModuleNotFoundError` plus `FAILED
   tests/test_pricing.py::test_discount_applies - assert 91 == 90` → anchor
   `test_id=test_discount_applies, exc_type=ModuleNotFoundError`; the same log without the
   setup error → `AssertionError`. Same failing test, same assertion, two signatures — the
   history splits and the prior/cap reset the day the unrelated fixture is fixed. With a bare
   `FAILED nodeid` line (older pytest / width truncation) both type and message come from the
   other test.
   Owner: cicd-integration

7. [low] `src/api/main.py:1214-1266` — `_execute_approved` executes the plan but never
   `record_observation`s the write; dispatch 9 says the action is recorded "for every
   executed write regardless of the YAML obligation, because the retry count must not depend
   on a policy file".
   Failure scenario: `policy.yaml` flips `retry-suspected-flaky` to `require_approval` → every
   approved `rerun_failed_jobs` executes and `actions_in_window["rerun_failed_jobs"]` stays 0
   → the 2/24 h cap never bites.
   Owner: api-surface

8. [low] `src/harness/memory.py:327-336` — the write-time scrub is regex-only;
   `tool_calls[].args.content_b64` (kept intact because the approval route must re-execute
   it) is base64 and opaque to `SECRET_PATTERNS`, so it lands verbatim in
   `approval.plan_json` and `run.outcome_json`. The HTTP boundary digests it
   (`_digest_content_b64`); the file does not.
   Failure scenario: a diff with a committed `ghp_…` → the model drafts a file reproducing
   the line → `harness.db` bytes hold base64(token); Appendix E's leak test (sentinel string
   search over the file bytes) passes while the token is one `base64 -d` away.
   Owner: harness-core / api-surface

9. [low] `scripts/replay.py:172-179` — `claim_run`/`save_run` are unguarded (no
   `_claim_or_degrade`, no `except MemoryStoreError`), and `save_run` precedes the print.
   Failure scenario: store locked at the end of a replay → three model calls spent, traceback,
   no outcome printed — the "soft dependency" rule does not hold on the CLI path.
   Owner: fixtures-eval

10. [low] `src/integrations/cicd/history.py:114` + `agents/remediator.py:236-271` — the retry
    cap is check-then-act ~30 s apart across runs with no reservation.
    Failure scenario: three deliveries for the same flaky test on three PRs inside a minute
    (≤ `max_concurrent_runs`) → each Investigator reads `retries_in_24h = 0` → three
    `rerun_failed_jobs` in the window against a `lt: 2` cap.
    Owner: cicd-integration

## Suspicions S1–S10

- S1: CLEAR — the late `save_run` replaces the synthetic `run_timeout` outcome with the real
  one; the chain lookup keys on `attempt`, not status, so dedup and `get_run` are unaffected;
  `list_runs` never listed it under `failed` anyway (S7). The double execution is Appendix C's
  accepted takeover cost.
- S2: CLEAR per PLAN amendment 8 — note two things a caller cannot detect: the `202` is
  byte-identical to a claimed run's, and a row saved after the store recovers carries
  `unclaimed:<id>`, so dedup for that key is lost permanently, not only during the outage.
- S3: CONFIRMED (→ finding 4) — "so a reader can weigh it" has no reader.
- S4: CLEAR — consistent with Appendix E's "the whole pipeline is exercisable" and Verify
  step 3 in dry-run; the one consequence (a `HARNESS_DRY_RUN=false` flip inherits dry-run
  counts for ≤24 h) is fail-closed.
- S5: (a) CLEAR — live 404 → `not_found` → pending; `conclusion: null` → `"None"` → pending;
  replay's missing file → `not_found`. (b) CONFIRMED (→ finding 5, pagination). (c) CLEAR —
  3 reads/run; older pendings can starve behind three newer unresolvable ones, which is the
  stated bound working.
- S6: CLEAR — nothing inside the `try` raises on a programming path today (bundle validation
  guarded, gateways return errors as data); the masking is real but lands fail-closed (999)
  and the traceback is in the server log.
- S7: CLEAR — accepted and documented in `list_runs`'s docstring; `?status=superseded`
  reaches those rows.
- S8: CONFIRMED (→ finding 6).
- S9: CLEAR for every served surface (`_serialize_run_outcome`, `problem()`, approval and
  escalation responses all scrub); one at-rest gap → finding 8. (`approval.decision_note` is
  written unscrubbed but is operator-authored and never served.)
- S10: CLEAR — heartbeat cancelled and awaited in `finally` before `run()` returns,
  `save_run` follows; `heartbeat` only touches `in_progress` rows and the upsert never writes
  `heartbeat_at`; A.2's `run(request) -> RunOutcome` unchanged, new kwargs defaulted. The gap
  is *before* `run()` → finding 2.

## Contract diffs

none — A.5 models/Protocol (incl. the Phase 3 additions), `RunClaim`, `ApprovalRecord`,
`dominant_verdict`/`has_outcome`/`signature_id_for`/`observation_id_for`, A.11
`PriorHistory.key`, and `001_init.sql` all match PLAN column-for-column; the one schema
addition (`approval.context_json`) is the recorded one. `Orchestrator(memory=None,
heartbeat_interval_s=15)` as amended.

## Unhandled failure paths

- `list_workflow_run_jobs` (attempt+1 probe) → no missing B.2 condition (timeout / rate
  limit / 5xx / 404 / malformed all come back as data); but no *time* bound: three probes ×
  the gateway's worst case (3 × 30 s read timeouts each) = 270 s, inside a 240 s run budget,
  spent on a courtesy before the model call.
- SQLite `file is not a database` at startup → quarantined and recreated, but no
  `config_error` escalation is recorded (B.3 row says "escalate `config_error`").
- SQLite corruption *after* startup → `MemoryStoreError` → degrade only; no quarantine at
  runtime.
- `save_approval` failing after `save_run` succeeded → run reads `awaiting_approval` forever
  with an approval id that answers `404` (accepted by amendment 8; logged only).

## Plan drift

- built-but-unplanned: none beyond what the Phase 3 amendment records (`storage.py`,
  `context_json`, the probe, `#fresh` keys, superseded-as-failed). Minor: dispatch 5 says the
  superseded run's escalation "names the run that took over" — `_outcome_from` names nothing
  (`payload={"superseded": True}`), and it says `channels=["db"]` while no `escalation` row
  exists for it.
- planned-but-missing: Open Risk 7's `failed_again` self-correction (finding 3); dispatch 9's
  "every executed write is recorded" on the approval path (finding 7); B.3's `config_error`
  escalation on quarantine.
