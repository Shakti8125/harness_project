# Handoff — start of Phase 5 (Trace view + real GitHub webhook)

Written 2026-09-14, at `f7cfc36` on `master`. Phase 4 is built, audited and fixed; it is
**not yet tagged `phase-4-green`**, because two live Verify steps are pending the provider's
quota reset (§1). Gate green (744 passed, 2 skipped), one independent audit with five
findings — all five closed in a same-day fix round, the fix round coordinator-verified per
the standing one-audit-per-phase rule (`docs/progress/phase-4/backlog.md`, "Audit
provenance").

**Read this file first and in full.** It is the index. It does not restate `PLAN.md` or the
Phase 4 documents — it tells you which parts are load-bearing for Phase 5 and records what
is true about this tree but written down nowhere else.

---

## 1. What is unfinished, and what it takes to finish

Three things, all live-quota-bound, in this order:

1. **Verify steps 3a and 3c** (`docs/progress/phase-4/verify.md`): `HARNESS_FAULT_INJECT=
   llm_bad_json:2` and `llm_429:3` on `flaky_test`, six model calls in total (the third
   attempt of each agent, then the fourth). Run under uvicorn per fault value, on the
   post-fix tree; expected `{"status":"completed","attempts":3}` and `"completed"`. The
   scratchpad helper that started/stopped uvicorn per fault is not in the repo; the recipe
   is: set the variable, `uv run uvicorn src.api.main:app --host 127.0.0.1 --port 8000`,
   poll `/readyz` for `migrations_applied`, `curl`, kill the process that holds the port.
   Then update `verify.md` and `test-verifier.md`'s verdict line, and **tag
   `phase-4-green`** on that tree.
2. **The README accuracy number**: `uv run python scripts/eval.py --runs 1 --llm gemini`
   — five scenarios, ≤ 15 calls, a day's quota. Record the report in `verify.md` §4 and quote
   *that* run, not the stub's 25/25, anywhere accuracy is claimed.
3. **Deploy** — only when asked (§8). The Space still serves `phase-3-green`; a pending
   approval created there before the deploy is judged under `skipped` and denied by name
   after it (backlog).

**Quota.** The free tier's ~20 calls/day resets at midnight Pacific (~12:30 IST), not local
midnight; Phase 3's 15 verify calls, the Space check's 3 and Phase 4's 3 fell in one Pacific
day and the fourth live call of the Phase 4 block met a real 429. Count the day on that clock
(`data/harness.db`, `llm.attempt` spans with `tokens.total > 0`). Every `llm_bad_json` run
and every `--llm stub` run costs nothing.

---

## 2. Deploy state

Unchanged from the Phase 4 handoff §1: the Space serves `phase-3-green`; no persistent
volume; migrations by hand-call from `app.py:main()`; `HARNESS_GATEWAY=replay`,
`HARNESS_DRY_RUN=true`, no allowlisted repo, no authentication. Two Phase 4 additions
matter for the next deploy:

- **`docker-compose.yml` now forwards `HARNESS_FAULT_INJECT` from the shell**, and thereby
  overrides a value in `.env` with the empty string when the shell does not export it
  (backlog). The `env != dev` refusal is what keeps the variable harmless on a
  `HARNESS_ENV=prod` target; the Space's `.env` says `dev`.
- **`HARNESS_ESCALATION_WEBHOOK_URL`**, when set, adds `webhook` to every run's channels
  and the notifier posts to it (B.4). Not set anywhere today. It is a `SecretStr`: registered
  with the `Redactor`, scrubbed from httpx's own request log by a filter the notifier
  installs, never in `delivery_error`. Not validated at boot (backlog).

---

## 3. What Phase 5 is

`PLAN.md` §"Phase 5 — Observability trace view + real GitHub webhook": *the decision trail
is visible to someone who is not reading the JSON, and a real repo can drive it.* Built:
the Jinja2 trace view at `GET /runs/{id}/view`, `POST /webhooks/github` (HMAC, allowlist,
`202` then background, full idempotency), the PR-writing tools behind the existing
`require_approval` rule, `scripts/record_fixture.py`, `scripts/scrub_fixtures.py`,
`seed_demo_repo.sh`, and `tests/test_no_secret_leak.py`. Read that section end to end, its
Verify block (step 1 expects `evaluator` among the span components — it is there since
Phase 4), the "Secrets never reach the trace" list (the regex set is wider than
`deps.SECRET_PATTERNS` today: the private-key and `api_key=` shapes are missing), A.9, A.12,
B.2 (the write tools' idempotency), and Appendix C.

---

## 4. Where the tree stands

`472120b..HEAD` on `master`:

| Commit | What |
|---|---|
| `dbc63ab` | the build: the `evaluate` stage, `claim_checkers.py`, `agents/evaluator.py`, `harness/escalation.py`, `harness/faults.py`, `OutputBudget`, `DiffSummary.commit_shas`, `EscalationRecord.delivery_error`, `policy.yaml` `[pass, warn]`, the Diagnostician prompt v3, `dependency_break`, `scripts/eval.py`, 8 new test files |
| `e43b99c` | Verify results (steps 1, 2, 3b, 4), the gate verdict, PLAN.md items 1–15 |
| `e580aad` | the audit's fix round (findings 1–5, 8 tests), A.8/A.2 drift |
| `f7cfc36` | the fix round recorded, `backlog.md`, PLAN.md items 16–20 |

Gate on HEAD: `ruff` clean; `mypy --strict src/harness` clean (17 files); `mypy src` 8
errors, all pre-existing from Phase 2; **744 passed, 2 skipped**. `uv.lock` unchanged this
phase (httpx was already a dependency).

Documents: `docs/progress/phase-4/{dispatch,verify,test-verifier,review,backlog}.md`.

---

## 5. What Phase 4 built that Phase 5 depends on

- **Every evaluator check is already a span.** `evaluation.claim` (component `evaluator`)
  per claim, with `claim_id`, `kind`, `locator`, `result`, `detail`, `matched_locator`;
  the evaluate stage's `agent.run` span carries `verdict`, the three counts,
  `confidence_delta` and `final_confidence`. The trace view's "citations with their verify
  verdicts" card reads these; nothing new needs recording. `policy.decide` spans carry
  `downgraded_from` since this phase — render it.
- **`final.evaluation` is served** on every run past the diagnose stage
  (`EvaluationReport`: `verdicts`, `verified`, `refuted`, `unverifiable`, `verdict`,
  `confidence_delta`, `reason`), and `final.diagnosis` is the *re-calibrated* diagnosis:
  `confidence_adjustments` includes the evaluator's row. The Diagnostician's own
  `agent.run` span is pre-penalty; the served body is not.
- **The `evaluate` stage's `StageRecord`** is `{stage: "evaluate", agent: "evaluator",
  attempts: 1, tokens: 0}`; a gated remediate stage is `{agent: null, status: "gated"}`.
  `RunOutcome.stages` is four long on a completed run — the waterfall should expect it.
- **Escalations carry `channels` and `delivery_error`.** `EscalationRecord.delivered_at`
  means the webhook's acceptance when one is configured, else the log line's; the
  `escalation` row has both columns. The view should show `delivery_error` when set.
- **`EscalationRecord.reason` has no member for "unverifiable"** (backlog; audit S2): a run
  that fails the share rule with nothing refuted escalates `evidence_refuted` with a
  message naming the share. If Phase 5 adds `evidence_unverifiable`, it is an A.1 change:
  `contracts.py`, `orchestrator.EscalationReason`, and the drift-guard test
  (`tests/unit/test_escalation_reason_drift_guard.py`) all move together.
- **The write tools are wired but unimplemented** (`GitHubToolGateway` raises for
  `create_branch`/`create_or_update_file`/`open_pull_request`); the approval route already
  executes them, records the action, and escalates `tool_failure` on the first failure
  (`test_approvals_e2e.py::test_approve_re_evaluates_and_executes_through_the_gateway` pins
  the current "not implemented" shape and will need to change). `remediation.
  canonical_tool_calls` fixes the argument shapes; `content_b64` is what the file tool
  receives; the `Redactor`'s base64 pass scrubs it at rest (Phase 3 fix round).
- **The claim protocol, `idempotency_key_for(webhook)`, `_fresh_key`, and `_execute` are the
  webhook route's building blocks** (`main.py`); `POST /v1/runs` already claims, dedupes,
  takes over stale runs and heartbeats the semaphore wait. The webhook route is signature
  verification and event filtering in front of the same `_execute`.
- **`Settings.github_webhook_secret` is a `SecretStr`** already registered with the
  redactor; nothing reads it yet.

---

## 6. Design decisions Phase 4 made that Phase 5 will bump into

1. **The verdict rule is literal** (dispatch decision 4; audit S2 ruled for it): `fail` on
   any `refuted` *or* `verified/total < 0.5` with unverifiable claims in `total`. A run
   whose only citations name a missing artifact fails and escalates `evidence_refuted`.
   The trace view should render the report's `reason`, which says "only 0 of 1 claim(s)
   verified", next to the reason enum — do not let the card say "refuted" when the
   verdicts say `unverifiable`.
2. **Memory is written by the evaluate stage** and never tallies a verdict the evidence
   gate refused (fix-round finding 1). A signature whose runs all escalated
   `evidence_refuted` has `occurrences` and observations but no `verdict_counts`. A view
   of a signature's history should expect that shape.
3. **Fault injection is parsed once in `deps.build_fault`**; `KNOWN_FAULTS` is the union of
   the harness's names and `wiring.FAULT_FABRICATE_CITATION`. A Phase 5 fault (a webhook
   that fails signature, a GitHub 5xx on a write) registers there, not in the store or the
   client.
4. **`EvidenceAvailability`** is how the checkers tell "not in the diff" from "no diff":
   computed from `bundle` plus `state.degraded` (`"logs"`, `"diff"`). If the Investigator
   gains new degraded components (a failed write-tool probe, say), they do not affect it;
   if it renames those two, the checkers' `unverifiable` path silently disappears.
5. **`scripts/eval.py` uses one temporary database per run** and imports `tests/stubs.py`
   only under `--llm stub`. `record_fixture.py` should write scenarios the eval can score:
   a `scenario.yaml` with only the keys the scenario determines.

---

## 7. Residuals worth knowing (full text in `docs/progress/phase-4/backlog.md`)

- Verify 3a/3c and the live eval number are pending quota (§1).
- `WebhookNotifier` worst case ~46 s (per-phase httpx timeout), outside `run_budget_s`.
- `_SHA` treats a nine-digit job id as a sha candidate; `test_in_log` matches anchor lines
  only; an empty `quote` is refuted, not unverifiable.
- Phase 3 finding 10 (the check-then-act cap) is open; `eval.py --shared-db` exercises it.
- `annotate_run` is recorded, never executed.

## 8. How to work on this codebase

Unchanged from the Phase 4 handoff §7 (inline by the coordinator; one independent audit;
fix rounds coordinator-verified and recorded as such; write patch scripts to the scratchpad
and run them with `python <file>` — Bash heredocs mangled a multi-line patch again this
phase; never `git checkout -- <file>` on a dirty file; prove each fix's test against the
unfixed tree — this phase did it by ordering, tests first on a clean tree, and said so),
plus:

- **Count live calls on the Pacific day** before spending them (§1).
- **One uvicorn per fault value**: settings are read once at boot, so a Verify step that
  changes `HARNESS_FAULT_INJECT` restarts the server. `uv run uvicorn` re-executes python;
  kill the process that holds the port, not the wrapper's PID.
- The `phase-reviewer` audit cost ~236 k tokens this phase; the brief named ten suspicions
  and the report answered each — worth the cost, once.

## 9. Constraints that outlive Phase 4

Unchanged: `git add` new files explicitly; a mounted sub-app gets no lifespan events
(`app.py:main()` hand-calls); `app.py` must not read `os.environ`; the Space is pinned to
`zero-a10g`; never print the HF token; `uv.lock` is the pin of record; ask before changing
exposure or deploying. New: **never quote a stub-mode eval number as accuracy**; the report's
`llm` field exists so a reader cannot mistake one.

## 10. Definition of done for Phase 5

`PLAN.md`'s Phase 5 Verify block. Step 5 is live against the Space and the demo repo and
needs the deploy the user has not yet been asked about. A phase is done when
`test-verifier.md` says PASS and `review.md` says SHIP (or, after a fix round, records the
coordinator's SHIP with its basis); tag `phase-5-green` on the tree that is actually
finished — and `phase-4-green` first, once §1's live steps are recorded.

## 11. Read order for a fresh session

1. This file.
2. `docs/progress/phase-4/verify.md` §3 (what is pending and why) and `backlog.md`.
3. `PLAN.md` Phase 5, A.9, A.12, B.2, Appendix C, the Phase 4 amendment (items 1–20).
4. `docs/progress/phase-4/dispatch.md` — the decisions the Phase 4 build was made against.
5. `src/integrations/cicd/agents/evaluator.py`, `src/integrations/cicd/claim_checkers.py`,
   `src/harness/escalation.py`, `src/harness/faults.py`, `src/api/deps.py`
   (`build_fault`, `build_notifier`, `build_orchestrator_for`), `src/api/main.py`
   (`_execute`, `_execute_approved`, `_settle_run`) — what exists.
6. `docs/progress/phase-4/review.md` only if a finding number comes up.
