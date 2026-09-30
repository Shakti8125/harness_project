# Phase 5 — carried backlog

Written 2026-09-14 at the close of the phase's build and fix round. What is known and
deliberately not fixed, what was verified by whom, and the decisions the next phase must
not reopen without reading the reasoning. The authorities are `review.md` (the audit) and
`dispatch.md` (the decisions the build was made against, one of them amended in place);
this file indexes what survived.

## Open — by design, carried from Phase 3

**Phase 3 finding 10 (low): the retry cap is check-then-act.** Unchanged since Phase 4's
backlog; `scripts/eval.py --shared-db --concurrency N` still records the resulting
`policy_denied` escalations rather than treating them as a bug. A reservation protocol is
still the fix, still not built, still the next phase's call.

## Audit provenance — read this before trusting the tag

- `phase-4-green..deef51e` (the build `66e2448`, the records, the seed-script retry) was
  **independently audited** by `phase-reviewer` on 2026-09-14 (the first dispatch died on
  the session's rate limit before writing anything; the second, after the reset, wrote
  `review.md`): FIX FIRST, nine findings (2 high, 2 medium, 5 low), S1–S16 answered, four
  contract-diff items, three unrecorded drifts. `review.md` is that report verbatim.
- `deef51e..9e3b4df` (the fix round) is **coordinator-verified, not independently
  audited**, per the standing rule of one audit per phase. What that round contains, and
  why each item is bigger than a patch: the `Redactor`'s two-tier pattern set and the
  `execute_plan` guard (finding 1 — a change to *what gets scrubbed where* and a new
  refusal path on the approval route); Appendix D's baseline chain in the Investigator,
  a `get_commit` fixture, a fixture slug rename (finding 2 — new read calls on every
  cold-start path); a process-wide log record factory (finding 4 — every logger in the
  process now passes through the `Redactor`); the seed script's `--force` rewrite
  (finding 3 — it runs against a real account); and findings 5–9. Each carries a test
  that failed on the unfixed tree first (`test-verifier.md`, "Ordering").
- The four-phase rule stands: every fix round has been where an independent pass earned
  its cost. **If a drafted file ever reaches a repository with `***REDACTED***` in it, a
  branch-push failure on the demo repository ever replays as `cold_start=true`, a token
  ever shows in a Space log line, or `seed_demo_repo.sh --force` ever loses a commit on
  `main`, an independent read of `deef51e..9e3b4df` is the first thing to spend on.**
- **The fix round shipped a defect the suite could not see, and the first live run
  caught it** (`2c79035`, 2026-09-15 21:10 IST). The log record factory (amendment 13)
  pre-formatted every record and cleared `record.args`; uvicorn's `AccessFormatter`
  unpacks `record.args` positionally, so every request printed a `--- Logging error ---
  ValueError: not enough values to unpack (expected 5, got 0)` traceback and lost its
  access line -- on the Space too, for the eleven minutes between `3216a41` and
  `2c79035`. No test drove a formatter that reads `args`; the leak test and the unit
  tests all read `getMessage()`. The fix scrubs `msg` and each argument in place
  (exceptions become their scrubbed `str()`, numbers pass through) and folds a record
  only when a credential straddles `msg` and `args`; three tests, and a real uvicorn
  serving an access line with `?token=…` redacted. This is the coordinator-verified
  round's first known miss, found by the Verify block doing its job, and it is exactly
  the kind of defect the tripwires above exist for -- record it against the rule, not
  as an exception to it.
- `verify.md` is recorded at `66e2448`, before the fix round, with a "Fix round" section
  saying what changed about each step (nothing about the expected values). The live
  steps have not run; `phase-5-green` is **not tagged** — the tag goes on the tree that
  passes them (`handoff.md` for Phase 6, §1).

## Residuals and hazards — real, not findings against a stated contract

- **A placeholder webhook secret is honoured** (audit S2). `.env.example`'s
  `replace-me-with-a-random-string` verifies signatures like any other value; PLAN sets
  no entropy rule. A blank or whitespace-only secret refuses everything with one warning.
  Deploy note: the Space's secret must be the one GitHub was given, and not the example.
- **The heuristic tier does not look through base64** (amendment 11). A log line a model
  pasted *into a drafted file* with `api_key=…` on it is stored under `content_b64` with
  only the registry and the vendor shapes applied. That is the trade the fix makes: a
  drafted file is an execution input and an assignment-shaped line is ordinary source.
  The vendor shapes (`ghp_`, `AIza`, PEM, `bearer`) and every registered value still apply
  through the encoding, and a plan they altered is refused at execution, never committed.
- **`execute_plan`'s refusal is `invalid_args`**, escalated as `tool_failure`. A.4 has no
  `altered_at_rest` kind and no caller branches on one; the message says what happened.
  A run whose approved plan carried a credential therefore ends `escalated
  (tool_failure)` with the branch already created (the calls before the altered one ran).
  Loud and correct; not tidy.
- **The log record factory scrubs `msg` and `args`, not the formatted traceback of an
  `exc_info`** (amendment 13). A `_Failure` message inside an exception chain that a
  handler formats with a traceback is scrubbed only where it was also logged as text.
  `aiosqlite`'s `DEBUG` lines echo whole rows (scrubbed rows, and now scrubbed again on
  the way out); the volume is the cost, not the content.
- **The sync reply of `POST /v1/replay/{scenario}` and the stored run can differ in
  `excerpt_length`** (40777 vs 40751 for `real_regression`) because the store scrubs the
  excerpt at rest and the reply is built from the in-memory outcome; the `view` and
  `GET /v1/runs/{id}` agree with each other. Digest-only; noted so a diff of the two is
  not read as corruption.
- **The recorded `find_last_successful_run` body is the live gateway's post-`before`
  filter, and a recorded log is the tail the gateway kept** (audit finding 8, second
  half). `record_fixture.py` now says so in `scenario.yaml` when a log was tail-capped;
  `fixtures/README.md`'s "raw response" wording is true of hand-written fixtures and
  approximately true of recorded ones. Recording the raw bytes would mean recording
  below the gateway; not built.
- **`create_issue` files a duplicate when `signature_id` is absent**, which is only when
  the run has no idempotency key (never, on the API's paths); and when a person removed
  one of the agent's labels from the marked issue, since the search now filters by
  `labels=` (amendment 15). The marker in the body still says which signature it was.
- **`open_pull_request` reports `labels_applied`** on the pre-check path; the obligation
  `label:agent-generated` is met on the retry, not on the attempt that failed. A label
  call that fails on *every* attempt leaves a draft PR that is never labelled and an
  escalation naming it each time. Bounded by the approval path (one attempt per
  approval), not tuned.
- **`_says_exists` is a phrase match** on `already exists` in `message` and
  `errors[].message` (audit S6c). A different `422` carrying the phrase re-fetches and,
  finding nothing, re-raises the original failure; no misclassification observed, none
  ruled out by GitHub's documentation either.
- **`PUT contents` `422` "sha wasn't supplied"** (a file created between the `GET` `404`
  and the `PUT`) surfaces as `ToolError(kind="unknown", http_status=422)`: loud, not
  clobbering, not retried. Acceptable.
- **A `CancelledError` from outside a stage leaves its span `status=ok`** (audit S9,
  pre-existing): `span()` catches `Exception`, and cancellation is a `BaseException`. A
  `run_timeout` is caught *inside* the scope and does carry `status=timeout`.
- **`WebhookNotifier`'s worst case is ~46 s** (Phase 4 backlog, unchanged).
- **A write without an `idempotency_key` runs uncached in both gateways.** A.4 says the
  key is "required when `side_effect != "read"`"; neither `_invoke_write` refuses one
  without it. Nothing on the API's paths builds a keyless write (`normalize_plan` keys
  every side-effecting call), so it is a contract gap, not an observed failure; Phase 6's
  conformance suite is where it gets decided (handoff §5).
- **`search_workflow_runs` shares the `-branch-<name>` slug rule** and is called by
  nothing today; a scenario that needs it records under the same suffix.
- **`seed_demo_repo.sh` was not run against GitHub.** Syntax-checked, its usage and
  refusal paths exercised, its invariants pinned by reading it
  (`tests/unit/test_seed_script_invariants.py`). The first real run is step 5 and is
  the user's.
- **The Space serves `phase-4-green`** (`5abe6ad`), not this tree. Redeploying is the
  user's call (step 5's fourth part); the Phase 6 handoff carries the deploy note.
- **A `503` storm burns the day.** Google counts an overloaded (`503`) request against
  the free tier's 20 requests/day, and the client retries a `503` four times with
  sub-second backoff when the provider sends no `Retry-After` (Appendix B.1's row).
  2026-09-15 21:10–21:22 IST: five replays, 18 `503`s, one success, then `429
  RESOURCE_EXHAUSTED … limit: 20` -- the day gone on requests that produced nothing.
  The escalations were right (`llm_upstream`, then `rate_limited`); the *cost* of being
  right was not. Backlog for Phase 6 or a settings change: back off a `503` in seconds,
  not sub-seconds, and cap the per-run attempts on the free tier -- or stop after the
  first `503` when `HARNESS_ENV=dev`. Until then: on an overloaded evening, probe once
  with a tiny request (`scratchpad/probe_gemini.py`'s shape) before spending a replay.
  **Closed 2026-10-01 (step-5 plan, Stage 1)** for the "back off in seconds, cap
  attempts" half. The transient policy is now four `HARNESS_LLM_*` settings passed to
  every agent (`deps.retry_policy_for`; `src/harness/` untouched). The defaults are
  B.1's, and the free-tier profile is 2 attempts with one 5 s sleep; the local `.env`
  carries it. `tests/conftest.py` pins B.1's defaults, so the suite's attempt counts
  never read the operator's `.env`. The probe is now `scripts/probe_gemini.py` (one
  request; key scrubbed by its stripped value and by the `AIza`/`AQ.` shapes).
  `scripts/quota_ledger.py` counts the Pacific day's `llm.attempt` spans in local DBs
  and, with `--space`, on a deployment, listing who started each run. Still open: the
  profile does not cap schema retries, so one agent can still spend 3 + 2 + 2 = 7. The
  step-5 plan's stop rules are the guard. The "stop after the first `503` in dev" idea
  was not built.
- **Quota.** The free tier's ~20 calls/day resets at midnight Pacific (~12:30 IST);
  2026-09-14's day was spent before this phase's build began (18 calls by 13:30 IST).
  The live steps -- Verify 1 and 3 (six calls) and the eval's `cold_start` +
  `dependency_break` rows (six) -- are 2026-09-15's first spend, after counting the day's
  `llm.attempt` spans with `tokens.total > 0` on that clock.

## Security gate before live exposure (2026-10-01, step-5 plan Stage 1e)

A security assessment of `06c87db` (kept out of the repository until its fixes are deployed)
found that the step-5 live configuration was unsafe as the plan was written. Stage 1e is the
fix round. Everything is in `src/api/`, `src/integrations/`, `scripts/` and
`docker-compose.yml`; `git diff 06c87db -- src/harness/` is empty.

- **Closed:**
  - SEC-01: the PEM redaction pattern has a bounded body, so redaction is linear.
  - SEC-08: request bodies are capped at 1 MiB.
  - SEC-02, SEC-04, SEC-06: an operator token guards `/v1/runs` and `/v1/approvals` when
    `HARNESS_GATEWAY=github`. `decided_by` comes from the credential, and `live_allowed` is
    re-checked before a live approval executes.
  - SEC-03, SEC-24: `url_segments` validates every sha, ref and file path in both gateways.
  - SEC-05: `normalize_plan` derives the branch `agent/fix/<signature_id[:8]>` and the base,
    and drops paths under `.github/`. The gateway refuses a file write outside `agent/fix/`.
  - SEC-09: the fingerprint patterns are linear, lines are capped at 2,000 characters, and
    the fingerprint runs in `asyncio.to_thread`.
  - SEC-07: admission control answers `429` beyond `max_concurrent_runs` executing plus
    twice that many waiting. Signed deliveries are counted but never refused.
  - SEC-18: compose binds `127.0.0.1`.
  - SEC-19: the seed script sends the hook body on stdin.
- **Carried:**
  - SEC-10 and SEC-11 must be fixed before anyone sets `dry_run=false`.
  - SEC-12 stays in the backlog because it is in `src/harness/`.
  - SEC-13 to SEC-17, SEC-20 to SEC-23 and SEC-25 are low or info.
- **Changed behaviour worth knowing:**
  - The stub's canned plan names `agent/fix/<head sha[:8]>`, which is now replaced by the
    signature-derived branch. No fixture's expected values pin a branch, so none changed.
  - The Remediator prompt no longer tells the model to name the branch.
  - Placeholder shas in `test_gateway_github.py` became hex-shaped.
- **Tests:** each test fails on the pre-fix tree with the finding's own reproduction, and
  the three timing tests ran past a 60 s time-box there.
- **Audit provenance:**
  - `4e6261c..5602f47` was independently audited by one `phase-reviewer` dispatch
    (`review-security.md`). The verdict was FIX FIRST, with 1 medium and 3 low findings.
  - The medium finding: a signed delivery shared the admission pool with anonymous replays.
  - All four findings were fixed in the next commit. Each has a test that fails on
    `5602f47`. The round is coordinator-verified, not re-audited.
- **Early replay-mode deploy (2026-10-01, step-5 plan Stage 1e item 8):**
  - After the audit fixes and a green gate (976 passed, 2 skipped; stub eval 5/5), the user
    approved `git push space master:main` (`06c87db..a0a64a2`). The Space stayed
    `HARNESS_GATEWAY=replay`.
  - The Hub reported `RUNNING a0a64a2` 63 s after the push. Use the Hub's `runtime.sha`
    (`https://huggingface.co/api/spaces/shakti/agent-harness`) to wait for a deploy.
    `check_space.py --wait-for-new-build` polls for the webhook route's `401`, which every
    image since Phase 5 already answers, so it no longer tells builds apart.
  - `check_space.py`: ALL OK.
  - SEC-01: `POST /v1/runs` with a 120 KB PEM-marker key answers `422` in 1.47 s, the same
    as a benign 120 KB key (1.58-1.67 s, all network). Before the fix it took 14.5 s.
  - SEC-08: a declared 1 MiB + 2 body and a 1.5 MiB chunked body both answer `413`
    problem+json.
  - Replay mode unchanged: `mode=live` still answers `501`.
  - 0 runs listed and 0 model requests spent.
  - The probe smoke earlier the same session spent 1 request on the Pacific day that ended
    12:30 IST 2026-10-01. It answered `OK`, and no database records it.
  - Session B needs no code deploy, only the Space settings change. `space/main` lags
    `master` by the docs-only commit that records this; that commit needs no redeploy.
