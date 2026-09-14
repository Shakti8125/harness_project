# Handoff — start of Phase 5 (Trace view + real GitHub webhook)

Written 2026-09-14 on `master`, at `phase-4-green` (`5abe6ad`) plus the deploy record.
Phase 4 is closed: gate green (744 passed, 2 skipped), one independent audit with five
findings — all five closed in a same-day fix round, the fix round coordinator-verified per
the standing one-audit-per-phase rule (`docs/progress/phase-4/backlog.md`, "Audit
provenance") — every Verify step run live, the tag on the tree that passed, and the Space
serving it (§1, §2).

**Read this file first and in full.** It is the index. It does not restate `PLAN.md` or the
Phase 4 documents — it tells you which parts are load-bearing for Phase 5 and records what
is true about this tree but written down nowhere else.

---

## 1. What is unfinished

One thing, live-quota-bound: **the eval's two remaining scenarios**. The README's model
number is 3/3 (`real_regression`, `flaky_test`, `infra_timeout`, 2026-09-14) because the
day's quota was spent on Verify 3a/3c (6), the Space check (3) and those three (9).
`cold_start` and a *scored* `dependency_break` row are six calls:

```
uv run python scripts/eval.py --runs 1 --llm gemini --scenario cold_start --scenario dependency_break
```

Record the report in `docs/progress/phase-4/verify.md` §4 beside the first, and update the
README's table to five scenarios. (`dependency_break` already has one live data point: the
Space's post-deploy replay, hand-scored against its label in `verify.md`.)

**Quota.** The free tier's ~20 calls/day resets at midnight Pacific (~12:30 IST), not local
midnight; Phase 3's 15 verify calls, the Space check's 3 and Phase 4's first 3 fell in one
Pacific day and the fourth live call of the Phase 4 block met a real 429. Count the day on
that clock (`data/harness.db`, `llm.attempt` spans with `tokens.total > 0`). Every
`llm_bad_json` run and every `--llm stub` run costs nothing. **2026-09-14's Pacific day is
spent (18 calls) as of 13:30 IST.**

---

## 2. Deploy state

The Space at <https://shakti-agent-harness.hf.space> serves `phase-4-green` (`5abe6ad`),
pushed 2026-09-14 13:16 IST on the user's instruction as a fast-forward `472120b..5abe6ad`
of `master` to the Space's `main`, followed by the docs commit that records it. The new
build answered about 2 m 40 s after the push; the old container kept answering meanwhile
(poll for something only the new build can do — `POST /v1/replay/dependency_break` was
`404` on the old image — not for `200`). Verified live, not assumed: one `dependency_break`
replay through the real model (three calls; `awaiting_approval`, three citations all
verified, the fix PR held), then `healthz`, `readyz`, the run list, the 19-span trace with
`evaluation.claim` spans, `GET /v1/escalations`, three `problem+json` 404s, and a
credential-shape scan over 25.7 kB of served bodies — all in `docs/progress/phase-4/
verify.md` §"Deploy". Unchanged: no persistent volume (the Space's memory is one waking
period long; `GET /v1/runs` is empty most mornings), migrations by hand-call from
`app.py:main()`, `HARNESS_GATEWAY=replay`, `HARNESS_DRY_RUN=true`, no allowlisted repo, no
authentication. Two Phase 4 additions matter for the next deploy:

- **`docker-compose.yml` now forwards `HARNESS_FAULT_INJECT` from the shell**, and thereby
  overrides a value in `.env` with the empty string when the shell does not export it
  (backlog). The `env != dev` refusal is what keeps the variable harmless on a
  `HARNESS_ENV=prod` target; the Space's `.env` says `dev`.
- **`HARNESS_ESCALATION_WEBHOOK_URL`**, when set, adds `webhook` to every run's channels
  and the notifier posts to it (B.4). Not set anywhere today, the Space included. It is a
  `SecretStr`: registered with the `Redactor`, scrubbed from httpx's own request log by a
  filter the notifier installs, never in `delivery_error`. Not validated at boot (backlog).
- **A pending approval created on the Space before this deploy** would be judged under
  `skipped` and denied by name — moot this time, since the rebuild discarded the old
  container's state, but true of any persistent deployment.

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

- The eval's `cold_start` and scored `dependency_break` rows are pending quota (§1).
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

`PLAN.md`'s Phase 5 Verify block. Step 5 is live against the Space and the demo repo; ask
before deploying, as before. A phase is done when `test-verifier.md` says PASS and
`review.md` says SHIP (or, after a fix round, records the coordinator's SHIP with its
basis); tag `phase-5-green` on the tree that is actually finished.

## 11. Read order for a fresh session

1. This file.
2. `docs/progress/phase-4/verify.md` §4 and §"Deploy" (the live numbers, what is queued)
   and `backlog.md`.
3. `PLAN.md` Phase 5, A.9, A.12, B.2, Appendix C, the Phase 4 amendment (items 1–20).
4. `docs/progress/phase-4/dispatch.md` — the decisions the Phase 4 build was made against.
5. `src/integrations/cicd/agents/evaluator.py`, `src/integrations/cicd/claim_checkers.py`,
   `src/harness/escalation.py`, `src/harness/faults.py`, `src/api/deps.py`
   (`build_fault`, `build_notifier`, `build_orchestrator_for`), `src/api/main.py`
   (`_execute`, `_execute_approved`, `_settle_run`) — what exists.
6. `docs/progress/phase-4/review.md` only if a finding number comes up.
