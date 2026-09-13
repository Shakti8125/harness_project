# Phase 3 — carried backlog

Written 2026-09-13 at the close of the phase. What is known and deliberately not fixed, what
was verified by whom, and the decisions the next phase must not reopen without reading the
reasoning. The authorities are `review.md` (the audit) and `dispatch.md` (the decisions the
build was made against); this file indexes what survived.

## Open — one, by design

**Finding 10 (low): the retry cap is check-then-act.** The Investigator reads
`retries_in_24h` from memory, the policy decides on that number, and the Remediator acts
~30 s later; three deliveries of the same flaky signature inside one minute (three PRs, one
bad test) each read `0` and three reruns fire against a `lt: 2` cap. Closing it needs a
reservation -- the Investigator (or the policy step) writing a provisional action row that
the Remediator confirms or releases -- which is a new protocol between three agents and the
store, and a new failure mode (a reservation that is never released). Not built this phase.
The bound today: `max_concurrent_runs` (4) is the worst case per window, the retries are
dry-run on the demo target, and every one of them is on the trace. **Phase 4 or 5 should
decide whether the reservation is worth its failure mode; do not narrow the window with a
re-read at execution time and call it fixed** -- that changes the odds, not the property.

## Audit provenance — read this before trusting the tag

- `afc621b..de1365d` (the build, the gate docs, and the pre-audit redaction fix) was
  **independently audited** by `phase-reviewer`: FIX FIRST, ten findings (1 high, 2 medium,
  7 low), S1–S10 answered, no contract diffs. `review.md` is that report verbatim.
- `de1365d..HEAD` (the fix round for findings 1–9, the PLAN.md amendment items 9–17, and
  these records) is **coordinator-verified, not independently audited**, per the standing
  rule of one audit per phase. What that round contains, so a later reader can weigh it:
  a Python-side chain filter in `claim_run` (`is_chain_member`); a module-level
  `heartbeating()` context manager and the API's use of it around the semaphore wait;
  `last_retry_outcome` in `history.py`, the `PriorHistory.last_retry_outcome` field, the
  bonus veto in `memory_agrees`, and a rendered sentence; `upsert_signature(verdict=None)`
  plus a `verdict_threshold` on the Diagnostician wired from `escalation_threshold`; a
  `total_count` check in `_probe_rerun` and `per_page=100` on the GitHub jobs request; a
  section-header anchor and `_section_for` in `fingerprint.py`; `action_observation` shared
  by the Remediator and a new `_record_approved_action` in `main.py`; a base64 pass in
  `Redactor._scrub_str`; guarded store calls in `scripts/replay.py`. Each carries a test
  that fails on `de1365d` and passes on HEAD -- run against the stashed pre-fix tree during
  the round (`tests/unit/test_memory_store.py` ×4, `tests/unit/test_fingerprint.py` ×5,
  `tests/unit/test_history.py` (new, 9), `tests/integration/test_memory_e2e.py` ×4,
  `tests/integration/test_replay_script.py` (new, 1), `tests/integration/
  test_remediation_stage.py` ×1, `tests/unit/test_gateway_github.py` +1 assertion).
- The Phase 1 rule stands: every fix round there introduced a defect only an independent
  pass caught. This round touches the claim protocol, the confidence path and the redactor
  -- three places a mistake is expensive. **If a redelivery ever runs twice, a bonus fires
  on a prior that should not exist, or a served body carries something base64-shaped that
  should have been scrubbed, an independent read of `de1365d..HEAD` is the first thing to
  spend on.**
- `verify.md` is recorded at `66208d0`, before the fix round; the 15 live calls it spent
  were the day's quota. Steps 2–4 are pinned in-process by the e2e suite on HEAD (603
  tests), and the fix round changes none of the block's expected values: step 2's counts
  are all confident verdicts (unaffected by item 12), the section-header anchor does not
  change the fixtures' extracted anchors (`test_flaky_fixture_keys_on_the_first_failed_test`
  and the excerpt-equals-raw property still pass), and the approval-path recording only
  adds rows the block's runs never produce. **Not re-run live.**

## Residuals and hazards — real, not findings against a stated contract

- **The Space is amnesiac across restarts.** No persistent volume (dispatch decision 1):
  memory is real within one waking period, gone the next morning. Every cross-run
  demonstration in this phase was made locally. The first thing a persistent deployment
  should do is run Verify step 2 against the mounted file.
- **Verify step 3 needs a fresh database** (PLAN amendment item 7). On a file that already
  holds today's two retries the sequence is `deny, deny, deny, deny`. `cold_start` and
  `infra_timeout` share nothing with `flaky_test`'s signature and can be replayed freely.
- **`annotate_run` is recorded, never executed.** The `retry-suspected-flaky` obligation
  names two things; `record_observation` is acted on, the annotation waits for the tool.
- **Pending rows never expire, and `infra_timeout`'s stay pending forever.** The fixture
  deliberately ships no attempt-2 recording; on the live target a rerun that never happened
  probes `404` on every sighting (three probes per run, bounded, each a gateway read with
  up to three 30 s timeouts -- up to 270 s inside a 240 s run budget in the worst case; not
  observed, not bounded further). A `pending` older than the 30-day lookback simply falls
  out of `recent`.
- **Dry-run retries count against the cap** (dispatch decision 9, audit S4: consistent).
  A `HARNESS_DRY_RUN=false` flip inherits up to 24 h of dry-run counts, fail-closed.
- **Gated verdicts count as sightings, not verdicts** (item 12). `occurrences` is every
  diagnosed run; `verdict_counts` only those the gate would act on. The two can drift apart
  on a signature the model is consistently unsure about, and `dominant_verdict` will then
  never fire for it -- which is the intended reading, and worth remembering when a prior
  "should" exist and does not.
- **`list_runs?status=failed` excludes superseded rows** (audit S7, accepted); they are
  under `?status=superseded`, and `get_run` serves them as `failed`. The superseded row's
  synthetic escalation names no successor and has no `escalation` table row (audit plan
  drift note): the successor is the row whose `superseded_run_id` points back.
- **A run saved after a claim failure carries `unclaimed:<id>`** (audit S2): dedup for that
  key is lost for good, not only during the outage. Logged; not recoverable without a
  key-rewrite on recovery, which nothing does.
- **`save_approval` failing after `save_run`** leaves a run `awaiting_approval` whose
  approval id answers `404` (amendment 8, audit unhandled-path note). Logged only.
- **Corruption after startup degrades, never quarantines** (audit unhandled-path note): the
  B.3 quarantine runs in the migration step only. And a startup quarantine records no
  `config_error` escalation -- there is no run to file it under; it is a log line.
- **The `fresh` nonce is 32 bits.** A collision claims the earlier replay's row and answers
  `deduplicated`; at demo volumes this is theoretical, and the store's exact chain rule
  (item 9) means it cannot reach the real key.
- **`docker-compose.yml` does not forward `HARNESS_FAULT_INJECT`;** step 4 was run under
  uvicorn. The image is checked separately (`verify.md`, Docker section).
- **The base64 scrub is a second line, not a policy.** A drafted file that reproduces a
  credential is scrubbed at rest and on the wire (`***REDACTED***` inside the file body,
  re-encoded), and executing the approved plan then writes the scrubbed body. That is the
  right outcome for a secret; it is also a silent edit of the model's draft. The proper
  remedy is a plan-time guardrail that refuses such a plan with a named reason -- a Phase 4
  Evaluator concern, noted in the handoff.
- **Stale Docker container on `0.0.0.0:8000`.** `restart: unless-stopped` outlived the
  Phase 2 session by two days and answered `localhost:8000` with the old build. `docker
  compose down` before local verification; address the local server as `127.0.0.1`.
- **Quota.** ~3 model calls per replay, 20/day on the free tier: roughly six replays a day,
  fewer with Recovery retries. The 15 spent on `verify.md` were this phase's entire live
  budget; nothing else was run live.
