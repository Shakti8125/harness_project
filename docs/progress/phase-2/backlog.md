# Phase 2 — carried backlog

Written 2026-09-11 at the close of the phase. The record of what is known and deliberately
not fixed, what was verified by whom, and the decisions the next phase must not reopen
without reading the reasoning. The authorities are `review.md` (the audit) and
`dispatch.md` (the decisions the build was made against); this file indexes what survived.

## Open — none

All six audit findings are closed, each with a reproducing test where one was possible
(`review.md`). Every contract deviation the audit listed is reconciled into PLAN.md or
accepted in writing there.

## Audit provenance — read this before trusting the tag

- `85a168b..17fd4b5` (the build, both live-exposed fixes, and the two pre-audit fixes) was
  **independently audited** by `phase-reviewer`: FIX FIRST, six findings, all closed.
- `17fd4b5..HEAD` (the fix round for those six, plus the PLAN.md reconciliation) is
  **coordinator-verified, not independently audited.** The user directed that the review
  not be restarted after the fixes. What that round contains, so a later reader can weigh
  it: two `except` clauses in `gateway_github.py`, a tool-set filter and a forbidden-call
  carry-through in `normalize_plan`, an `_escalation_after_approval` helper in `main.py`, a
  pre-transition `404` check, `RemediationResult.status` gaining `rejected`, two span
  attributes, and `build_facts` losing a default. Each code change carries a test that
  reproduces the reviewer's confirming input (findings 1, 2, 4, 6) or extends an existing
  end-to-end assertion (finding 3's `rejected` path). Finding 3's `policy_denied`-on-approve
  branch itself is unreachable in this phase and is **not** pinned — the first thing to test
  when Phase 3 makes facts movable.
- Phase 1's rule stands: every fix round there introduced a defect only an independent pass
  caught. This round is smaller than any of those, and it is still the author checking
  their own work. **If anything in the approval route or the log download path misbehaves,
  an independent read of `17fd4b5..HEAD` is the first thing to spend on.**

## Residuals and hazards — real, not findings against a stated contract

- **The live model is slow to a degree the stubs cannot show.** Clean three-stage replays
  took 40 s (`flaky_test`) and 51 s (`real_regression`); a run with seven model attempts
  across stages (503s, a rate limit, a `MAX_TOKENS` retry) took 110 s. Re-measured per the
  Phase 2 handoff's request: `DEFAULT_RUN_BUDGET_S = 240` still holds with margin and is
  unchanged. The free tier's 20 requests/day now buys roughly **six replays**, fewer when
  Recovery retries — the day's live verification used about fourteen.
- **The Remediator's output budget is a guess that worked twice.** 8 192 with dynamic
  thinking left ~4 000 tokens for output on the re-run. A fix touching a large file, or a
  multi-file revert, could hit `MAX_TOKENS` again, and Recovery's "answer more briefly" nudge
  is the wrong medicine for a plan whose length *is* the content. The plan's own remedy
  (`max_output_tokens × 1.5` on retry) needs `retry_structured` to reach the request budget,
  which it cannot today — a harness change, deferred to the phase that next touches Recovery.
- **The rerun "already in progress" match is unverified against the real endpoint.** Four
  phrasings are matched; a fifth degrades to `tool_failure`, loudly. Check on the first live
  run.
- **`find_last_successful_run` drops runs on the failing head sha, not runs created after
  the failing run.** Exact for the push-to-branch case; a later successful push on a
  different commit would be taken as the baseline. Documented in the gateway; the fix needs
  the failing run's `created_at`, which the tool's arguments do not carry.
- **Both registries are in-process.** A restart loses pending approvals and the escalation
  list (the trace survives). Phase 3's `approval` and `escalation` tables replace them; until
  then `GET /v1/escalations` on the Space is a view of the current process only. The Space
  sleeps, so it is empty most mornings.
- **Approving a fix PR always ends in `tool_failure` this phase.** The PR tools are
  registered, decided, and answer `ToolError(kind="unknown")` naming the phase that
  implements them; the run then escalates. Honest, and worth knowing before a demo.
- **`AppContext.engine` has a default factory** so hand-built contexts (the test suite)
  load the same policy. A test wanting a *different* policy passes one explicitly; nothing
  does yet.
- **`scripts/replay.py --json` prints raw content** (log excerpts, patches, drafted files) to
  the operator's terminal. Documented; not a served surface.
- Carried from Phase 1, unchanged: a rate-limited run serves `final == {}`; one test skips
  because `gradio` is deliberately absent; the 20 s wall-clock test.

## Settled — do not reopen

- **Verify step 1 is recorded against the deny** until memory is real. The allow path is
  pinned offline. `dispatch.md` decision 2.
- **The action cap counts executed plans, not tool calls**, and fails closed on a missing
  count. `dispatch.md` decision 5.
- **Tool calls are derived from the drafts whenever the drafts determine them**; the model's
  calls are a fallback restricted to the action's tool set; a forbidden proposal is always
  judged. `dispatch.md` decision 7 (revised) and `review.md` finding 2.
- **A denied plan escalates `policy_denied`; a failed execution escalates `tool_failure`; a
  person's rejection completes the run as `rejected`.** Three different things, three
  different words.
- **Unimplemented write tools answer a `ToolError`, never raise.** `dispatch.md` decision 10.
- **`GET /v1/escalations` items carry `run_id`; the approval response carries `decisions`.**
  A.12 amended.

## Deploy state

See `handoff.md` for Phase 3 — the Space is pushed there, after the tag.
