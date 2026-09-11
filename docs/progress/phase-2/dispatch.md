# Phase 2 — dispatch decisions (written BEFORE the build)

Written 2026-09-11 at `85a168b`, the tree after `phase-1-green`. Normative for this phase.
Where this file and an implementer's instinct disagree, this file wins. Settled here so the
audit can check the build against a stated intent rather than reverse-engineer one.

## How this phase is being run

**Inline by the coordinator, not by parallel specialist dispatch.** `handoff.md` §9 and the
user's standing instruction both say subagent dispatch is the dominant token cost and is
reserved for two cases: work inside another agent's exclusive territory *while that agent is
running*, and the phase-closing independent audit. Nothing is running in parallel this phase,
so the first case does not arise. The coordinator has read the whole tree and builds
directly; `phase-reviewer` is dispatched for the audit, and again after any fix round
(`docs/progress/phase-1/backlog.md` — every fix round in Phase 1 introduced a defect only an
independent pass caught).

The gate verdict (`test-verifier.md`) is therefore **coordinator-run, not agent-run**, and
is recorded as such. The audit verdict (`review.md`) is independent.

## Decisions

### 1. `policy.yaml` is synced first, exactly as amended
`open-fix-pr` gates on `evaluation.verdict: {in: [pass, skipped]}`. One line, plus the
comment naming Phase 4. Nothing else in the file changes.

### 2. The retry-count fact reads `bundle.prior_history.retries_in_24h` — no separate stub
`PriorHistory(unavailable=True)` already forces `retries_in_24h == 999`
(`FAIL_CLOSED_RETRIES_IN_24H`, enforced structurally in `schemas.py`), and the Investigator
constructs exactly that in this phase. So `memory.retries_for_signature_24h` is read off the
bundle rather than from a second constant: one source of truth, already fail-closed by
construction, and Phase 3 changes what the Investigator puts in the bundle rather than
changing the Remediator.

**Consequence, chosen deliberately (amendment 3's fork):** the live `flaky_test` and
`infra_timeout` replays **deny** `rerun_failed_jobs` in this phase, and the run escalates as
`policy_denied`. Verify step 1 is therefore recorded against the deny, with the engine's
`<default>` reason naming the clause that failed (`memory.retries_for_signature_24h=999
fails {lt: 2}`), so the trace says *why* rather than merely *that*. The allow path — the
same scenario with a readable history — is pinned by a test that constructs the bundle with
`unavailable=False, retries_in_24h=0` and asserts `rerun_failed_jobs` executes through the
gateway. Both halves are covered; only the live demo of the allow half moves to Phase 3.

### 3. A denied plan escalates the run as `policy_denied`
`EscalationReason` has carried `policy_denied` since Phase 0 and nothing else in the
pipeline can produce it. A plan the policy refuses is precisely "the system will not act;
a person should look", so the run ends `escalated` with the `RemediationResult`
(`status="denied"`, every decision) still in `final`. A run whose stage *failed* would lose
the artifact; this one keeps it, which is the whole point of surfacing the decisions.

### 4. The suspend path is a post-stage hook on `StageSpec` — A.2 amended
`StageSpec.gate` runs *before* a stage. The approval suspend needs the mirror image: a hook
that reads the stage's *output* and can end the run early with a status the stage cannot
express through `AgentResult.status` (A.1 is frozen and has no `awaiting_approval`). So
`StageSpec` gains `suspend: Callable[[RunState], Suspension | None] | None = None`, and
`Suspension{status: "awaiting_approval" | "escalated", reason, escalate_as, payload}` is
a new orchestrator model. The integration supplies the closure, exactly as it supplies the
gate; the orchestrator learns nothing about remediation. Defaulted, so no existing caller
changes. Recorded in PLAN.md A.2 as an amendment, not built silently.

### 5. "Max one side-effecting action per run" counts *actions*, not tool calls
An action is one executed plan. `open_fix_pr` is one action made of three write calls
(`create_branch`, `create_or_update_file`, `open_pull_request`); counting calls would make
a fix PR impossible under a cap the plan's own catalog was designed to fit. The fact
`run.side_effecting_actions_so_far` therefore counts executed plans, every call in one plan
is evaluated against the same count, and a second side-effecting plan in the same run is
denied by the engine with `rule_id="<invariant>"` regardless of what any YAML rule says.
The unit test pins the invariant directly; the live flow never reaches it in this phase
(one plan per run), which is stated rather than hidden.

### 6. A plan executes all-or-nothing
Every proposed call is decided before any executes. Any `deny` → nothing runs
(`status="denied"`). Otherwise any `require_approval` → nothing runs, an `ApprovalRequest`
is built (`status="awaiting_approval"`). Otherwise everything runs in order. Half a plan
(a branch with no PR) is worse than none.

### 7. Tool calls are derived from the drafts whenever the drafts determine them
*(Revised after the first live run — the original wording made derivation a fallback for an
empty `tool_calls` only.)* The model chooses the action and writes the content; the calls
are mechanical. `retry_job` → one `rerun_failed_jobs` from the bundle's own run id and
attempt; `file_ticket` → `create_issue` from `ticket_draft`; the two PR actions → branch /
one file call per draft file / draft PR from `pr_draft`. These replace whatever the model
proposed, and the span records `tool_calls_derived=true`. The model's own calls are used
only when nothing can be derived (a PR action with no draft), so a plan is never silently
emptied. Every call gets a harness-minted `call_id` and every write call Appendix C's
`idempotency_key`. A tool the catalog does not know is evaluated as
`side_effect="destructive"` and falls to `<default>` deny — fail closed, visible in the
trace.

Why the revision: live, with the default 4 096 output budget, the model spent ~3 900 tokens
thinking, hit `MAX_TOKENS`, and after Recovery's "answer more briefly" nudge returned
`create_branch` with empty args and no draft. Raising the Remediator's output budget to
8 192 fixed the truncation; making derivation authoritative removes the class of plan where
the calls and the draft disagree. Re-run: the exact one-line fix, three canonical calls.

### 8. Approvals persist in an in-process registry, by the API layer
Same shape and same caveat as `RunRegistry`: replaced when the memory phase lands the
`approval` table. The Remediator never touches storage — it returns the `ApprovalRequest`
inside its `RemediationResult`, and `src/api` registers it after the run returns, keyed by
`approval_id`, together with what it needs to rebuild the gateway later. `POST
/v1/approvals/{id}` re-evaluates policy against the stored plan (Phase 2: same facts, since
nothing can have changed; the mechanism is what matters), executes on approve, updates the
stored `RunOutcome` so `GET /v1/runs/{id}` reflects it, and answers `409` on a decided
approval and `410` on an expired one. The response carries `decisions` in addition to A.12's
`{state, executed}` — additive, so the re-evaluation is visible when it refuses.

### 9. `GET /v1/escalations` items carry `run_id`
A.12 says `[EscalationRecord]`; the record has no run id and `extra="forbid"`. Each item is
the record's dump plus `run_id`. Additive, recorded in A.12.

### 10. Unimplemented write tools return a `ToolError`, not `NotImplementedError`
The plan says `create_branch` / `open_pull_request` are "registered with a
`NotImplementedError` body". `invoke` must not raise for anything but a programming error,
and a human approving a fix PR through the API is not one — a raise there is a `500` with
nothing in the trace. So the catalog entry exists, the policy decision is real, and
invoking one returns `ToolResult(ok=False, error=ToolError(kind="unknown", retryable=False))`
naming the phase that implements it. Same for `create_issue`. `rerun_failed_jobs` is real.

### 11. Live mode reaches the API, gated three ways
`POST /v1/runs` with `mode="live"` builds a `GitHubToolGateway` only when
`settings.gateway == "github"` (else `501`), and only for a repo in
`settings.allowed_repos` (else `403`). `HARNESS_DRY_RUN` defaults to true, so the first live
run touches nothing. `scripts/replay.py --live` drives the same path from the command line
for Verify step 5. Both are blocked on environment here (placeholder token, no demo repo)
and are recorded that way rather than as passing.

### 12. Fixture labels assert only what the phase determines
`flaky_test` and `infra_timeout` carry `effect: deny` in this phase — the honest label under
decision 2 — with a comment saying `allow` is what Phase 3 flips it to once
`retries_for_signature_24h` is a real count under 2. `infra_timeout`'s diff is empty and
must stay empty.

## Territory map for this phase (coordinator writes all of it)

| Area | Files |
|---|---|
| harness | `guardrails.py` (engine + loader), `orchestrator.py` (suspend hook) |
| integration | `policy.yaml`, `agents/remediator.py`, `prompts/remediator.md`, `remediation.py` (facts, decide, execute, canonical calls), `gateway_replay.py` (write tools), `gateway_github.py`, `wiring.py`, `rendering.py` |
| api | `deps.py` (engine, gateway selection), `main.py` (approvals, escalations, readyz, live mode), `approval_registry.py` |
| fixtures / scripts | `flaky_test/`, `infra_timeout/`, `scripts/replay.py`, `scripts/gen_fixture_log.py` |
| tests | `test_guardrails.py`, `test_remediator.py`, `test_gateway_github.py`, `test_approvals_e2e.py`, additions to `test_replay_e2e.py` |
| docs | this file, `verify.md`, `test-verifier.md`, `review.md` (reviewer), `backlog.md`, PLAN.md amendments |
