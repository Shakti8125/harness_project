# Phase 4 — carried backlog

Written 2026-09-14 at the close of the phase. What is known and deliberately not fixed,
what was verified by whom, and the decisions the next phase must not reopen without
reading the reasoning. The authorities are `review.md` (the audit) and `dispatch.md` (the
decisions the build was made against); this file indexes what survived.

## Open — by design, carried from Phase 3

**Phase 3 finding 10 (low): the retry cap is check-then-act.** Unchanged, and now
*exercisable*: `scripts/eval.py --shared-db --concurrency N` runs N deliveries of the same
signature against one store, and the report records the resulting `policy_denied`
escalations and `effect` label misses rather than treating them as a bug (dispatch
decision 12; handoff §5.6). A reservation protocol is still the fix, still not built, and
still the next phase's call.

## Audit provenance — read this before trusting the tag

- `phase-3-green..e43b99c` (the build `dbc63ab` and the records) was **independently
  audited** by `phase-reviewer`: FIX FIRST, five findings (3 medium, 2 low), S1–S10
  answered, two contract-drift items. `review.md` is that report verbatim.
- `e43b99c..e580aad` (the fix round for findings 1–5, PLAN.md items 16–20, the A.8/A.2
  drift) is **coordinator-verified, not independently audited**, per the standing rule of
  one audit per phase. What that round contains: a `report.verdict == "fail"` check in
  `EvidenceEvaluator._counts_as_verdict`; a `logging.Filter` on the `httpx` logger installed
  by `WebhookNotifier`; `rstrip(".+-")` on version tokens in `claim_checkers._versions_in`;
  the commit list in `rendering.render_diff_summary`; `scripts/eval.py::build_context`
  leaving the store to `AppContext.__post_init__`. Each carries a test that failed on the
  unfixed tree first (`test-verifier.md`, last section).
- The three-phase rule stands: every fix round has been where an independent pass earned
  its cost. This round touches the memory tally, a logging filter on a third-party logger,
  and a parser the Evaluator's verdicts depend on. **If a prior ever appears for a
  signature whose runs all escalated `evidence_refuted`, a webhook URL ever shows in a log
  line, or a true `dependency_bump` citation is refuted on its spelling, an independent read
  of `e43b99c..e580aad` is the first thing to spend on.**
- `verify.md` is recorded at `dbc63ab`, before the fix round; the fix round changes none of
  the block's expected values (step 2's served body does not depend on the tally; the
  commit list only adds prompt text; the eval's stub run does not use the store fault).
  Steps 3a and 3c were **not run live** in this session: the provider's daily quota was
  exhausted mid-block (see below). Both are pinned in-process and must be run live on the
  post-fix tree before `phase-4-green` is tagged.

## Residuals and hazards — real, not findings against a stated contract

- **Verify steps 3a (`llm_bad_json:2`) and 3c (`llm_429:3`) are pending a live re-run**
  (six model calls: the third attempt of each agent, then the fourth). The fault paths
  themselves ran as designed in 3a before Gemini answered a real 429; the daily cap resets
  at midnight Pacific (~12:30 IST), and Phase 3's verify spend counted against the same day.
  The README accuracy number (`eval.py --runs 1 --llm gemini`, ≤ 15 calls) is a separate
  day's quota. Neither is a stub-able gap: the stub proves the pipeline, not the model.
- **A run whose only citations name a missing artifact fails on the share rule and
  escalates `evidence_refuted`** although nothing was refuted (dispatch decision 4, PLAN
  item 4, audit S2). The reason string names the share; the enum does not. The audit
  recommends an additive A.1 reason (`evidence_unverifiable`) in Phase 5 — an A.1 change,
  so it is a recorded amendment, not a quiet edit.
- **`quote_exists` with an empty quote is `refuted` (−0.15), not `unverifiable`**; a quote
  of a rendering artefact the model can see but the artifact lacks (`--- path (status)`
  headers, section titles, elision markers) is refuted too. Both are the model quoting
  something that is not evidence; kept as refutations on purpose, noted because a reader
  of the trace may expect `unverifiable`.
- **`dependency_bump` with no version in the quote verifies on the package alone**
  (`"pydantic"`); a quote naming two changed packages is refuted. Pinned by test as
  intended; the prompt (v3) asks for the rendered line, which carries the versions.
- **`_SHA` treats any 7–40 hex token as a sha candidate**, a nine-digit job id included; a
  `commit_in_range` quote that mentions a job id and no sha is refuted on the job id.
- **`test_in_log` matches anchor lines only.** A test id that appears only on a non-anchor
  line (a `PASSED` line, prose) is refuted — the claim is about the failure, and the anchor
  set is the one every reader shares (handoff §4).
- **`WebhookNotifier`'s worst case is ~46 s, not 3 × 5 s** (audit S5): `httpx.Timeout(5.0)`
  is per phase (connect, write, read), so one attempt can take ~15 s. It runs outside the
  stage's `wait_for` (not charged to `run_budget_s`) but inside the concurrency slot and
  the heartbeat. Bounded, not tuned.
- **`HARNESS_ESCALATION_WEBHOOK_URL` is not validated at boot**; a malformed value surfaces
  as `delivery_error` on the first escalation, never as a failed boot. Every delivery opens
  its own `httpx.AsyncClient` (escalations are rare); a long-lived client would be a
  one-line change if that ever shows in a trace.
- **`docker-compose.yml` forwards `HARNESS_FAULT_INJECT` from the shell and thereby
  overrides a value set in `.env`** with the empty string when the shell does not export
  it. Set it in the shell (`HARNESS_FAULT_INJECT=... docker compose up`), not in `.env`.
- **A pending approval created before this phase's deploy is judged under `skipped` at
  approval time** and denied by name (dispatch decision 7): its stored run has no
  `evaluation` artifact. The Space has no persistent volume, so nothing there survives a
  rebuild anyway; a persistent deployment should expect one such denial per pre-Phase-4
  pending approval. Deploy note.
- **The fault client keys "per agent" on the request schema** (audit S3). Correct for the
  three LLM agents today (three distinct output models, one `generate` site, the schema
  built once per call); a future agent sharing an output model, or a schema-less request,
  would share a counter. `faults.py`'s docstring says so.
- **`scripts/eval.py --llm stub` imports `tests/stubs.py`** lazily, only in stub mode; the
  Docker image ships neither `scripts/` nor `tests/`, so the CI gate runs from a checkout.
  The summary line's "N escalated" under `--shared-db` counts the cap's denials with every
  other escalation; the JSON's per-run `escalation_reason` tells them apart.
- **Stub-mode `estimated_cost_usd_per_run` is 0 and says `unpriced`**; the Gemini client
  reports no cost in `TokenUsage`, so a priced number needs `--price-in/--price-out`.
- **`annotate_run` is recorded, never executed** (unchanged since Phase 2).
- **The Space still serves `phase-3-green`.** This phase is not deployed: the coordinator
  did not change exposure or deploy without being asked (handoff §1, §8).
- **Quota.** The free tier's ~20 calls/day resets at midnight Pacific, not local midnight;
  count the day on that clock (`data/harness.db` `llm.attempt` spans with
  `tokens.total > 0`) before spending. Fault-injected `llm_bad_json` runs and every stub
  run cost nothing.
