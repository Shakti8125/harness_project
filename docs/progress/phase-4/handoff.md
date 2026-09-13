# Handoff — start of Phase 4 (Evaluator + Recovery)

Written 2026-09-13, at `phase-3-green`. Phase 3 is closed: gate green (603 passed, 2
skipped), one independent audit with ten findings — nine closed in a same-day fix round,
one carried open by design — the fix round coordinator-verified per the standing
one-audit-per-phase rule (`docs/progress/phase-3/backlog.md`, "Audit provenance").

**Read this file first and in full.** It is the index. It does not restate `PLAN.md` or the
Phase 3 documents — it tells you which parts are load-bearing for Phase 4 and records what
is true about this tree but written down nowhere else.

---

## 1. Deploy state

The Space at <https://shakti-agent-harness.hf.space> serves the `phase-3-green` tree, pushed
2026-09-13 on the user's instruction as a fast-forward of the Phase 3 commits
(`afc621b..a644c90`, plus this docs commit). The new build answered `readyz` about 45 s after
the push (the old container kept answering `policy_loaded` without `migrations_applied` for
three polls; poll for the field you changed, not for `200`). Verified live after the push,
not assumed -- one replay, three model calls, everything else free:

| Check | Result |
|---|---|
| `GET /healthz` | `{"status":"ok","db":"ok","version":"0.1.0"}` |
| `GET /readyz` | `migrations_applied: true` alongside `db_writable`, `gemini_key_present`, `policy_loaded` |
| `GET /v1/runs`, `GET /v1/escalations` on the fresh container | `{"items":[],"next_cursor":null}` and `[]` -- no volume, so nothing survived the rebuild |
| `POST /v1/replay/flaky_test?fresh=false` | `completed` in 30 s; `prior_history` empty (`occurrences: 0`, hint `unknown`, first sighting on this container); `flaky_test` at 0.92; plan `retry_job`, `retry-suspected-flaky` allowed it (`retries_in_24h: 0`), executed dry-run (`rerun_requested: false, dry_run: true`) -- Phase 2's same replay was `policy_denied`, which is the memory rule working |
| Same `POST` again, same key | `deduplicated` in 2 s, `original_run_id` = the run above, zero model calls -- the Appendix C claim on the live store |
| `GET /v1/runs` | one row, `completed` |
| `GET /v1/runs/{id}/trace` | 13 spans (`run`, `agent.run`, `llm.attempt`, `prompt.render`, `policy.decide`, `remediation.plan`, `remediation.execute`); the local Verify's denied run had 12 -- the one more is the executed retry |
| Served bodies (both replays, run, trace, list, escalations) | 29 kB, zero credential-shaped matches |
| `POST /v1/approvals/apr_nope`, `GET /v1/runs/run_01J8NOPE…` | `404 application/problem+json` |

The local `docs/progress/phase-3/verify.md` remains the record for steps 2--4 (the cap
sequence, the outage step, the fault-injected recovery); the Space has one run of memory in
it until its next restart. Three things to know about the deployment:

- **The Space has no persistent volume** (Phase 3 dispatch decision 1, the user's call).
  Memory is real within one waking period and gone across restarts; `GET /v1/escalations`
  and `GET /v1/runs` are empty most mornings, as before. Cross-run demonstrations are local.
- **Migrations run from `app.py:main()` by hand-call** (`asyncio.run(get_app_context()
  .initialize())`) because a mounted sub-app gets no lifespan events. A second migration
  file lands in `src/harness/migrations/` and needs nothing else — the runner is
  idempotent, tracked in `schema_version`, and `readyz` fails until it has applied.
- **`docker-compose.yml` does not forward `HARNESS_FAULT_INJECT`.** Fault-injected Verify
  steps run under uvicorn. Phase 4 adds LLM faults (§2); either forward the variable or keep
  the same split.

The exposure story is unchanged: no authentication anywhere, writes inert on the Space
(`HARNESS_GATEWAY=replay`, `HARNESS_DRY_RUN=true`, no allowlisted repo) -- the executed
`rerun_failed_jobs` above touched nothing real. Ask before changing any of that or before
deploying.

**Quota is the demo's binding constraint.** Three model calls per replay; the free tier's
20/day is about six replays. Phase 3's live Verify spent 15 in one day. Everything
fault-injected, every `GET`, and every error-shape check is free; prefer them.

---

## 2. What Phase 4 is

`PLAN.md` §"Phase 4 — Evaluator + Recovery": *the Remediator can no longer act on a claim the
log does not support, and a malformed model response no longer kills a run.* Built:
`harness/evaluator.py` + `integrations/cicd/claim_checkers.py` (five deterministic claim
kinds, the `fail`/`warn`/`pass` verdict, `evidence_refuted`), the outbound escalation
webhook (`HARNESS_ESCALATION_WEBHOOK_URL`, `delivery_error`), `scripts/eval.py`, and the
`dependency_break` fixture. Per PLAN's own amendment, **Recovery shipped in Phase 1**; what
Phase 4 owes is its Verify block — the `llm_bad_json:N` / `llm_429:N` /
`diagnostician_fabricate_citation` fault injections have no coverage yet.

Read `PLAN.md`'s Phase 4 section end to end, plus A.6 (Evaluator models), A.7 (Recovery),
the Phase 2 amendment 2 (`open-fix-pr`'s `skipped` verdict comes back out here), and the
Phase 3 amendment items 9–17 (the fix round; item 12 in particular, see §5).

---

## 3. Where the tree stands

`afc621b..HEAD` on `master`, tagged `phase-3-green`:

| Commit | What |
|---|---|
| `66208d0` | the build: `storage.py`, `001_init.sql`, `SqliteMemoryStore`, fingerprints, `history.py`, priors in all three agents, the claim protocol in the API, registries deleted, `cold_start`, `sqlite_locked` |
| `4404dbb` | Verify results (15 live calls) and the coordinator-run gate verdict |
| `de1365d` | the store redacts every JSON column at write time (pre-audit) |
| HEAD | the audit's fix round (findings 1–9, 25 tests), PLAN.md items 9–17, the records |

Gate on HEAD: `ruff` clean; `mypy --strict src/harness` clean (15 files); `mypy src` 8
errors, all pre-existing from Phase 2 (none in files Phase 3 added); **603 passed, 2
skipped** (the `gradio` test and one Phase 2 skip). `uv.lock` unchanged this phase.

Documents: `docs/progress/phase-3/{dispatch,verify,test-verifier,review,backlog}.md`.

---

## 4. What Phase 3 built that Phase 4 depends on

- **`evaluation.verdict` is spelled `"skipped"` in exactly two places**, deliberately:
  `Remediator(evaluation_verdict=EVALUATION_SKIPPED)` (`agents/remediator.py:138`) and
  `_execute_approved` (`main.py:1282`), both feeding `build_facts(evaluation_verdict=...)`.
  The constant is `remediation.EVALUATION_SKIPPED`. When the Evaluator exists, both callers
  read the run's real verdict — the second from the stored outcome's `final["evaluation"]`,
  which is where the orchestrator will file it once `build_stages` gains the stage.
- **The stage seam is `wiring.build_stages`.** `StageSpec(gate=..., suspend=...)` — a gate
  runs before its stage, a suspend hook after. The Evaluator is a fourth stage between
  `diagnose` and `remediate`; `evidence_refuted` on `fail` is a gate on `remediate` (or a
  suspend on `evaluate`), not a branch in the orchestrator. `ARTIFACT_KEYS` maps stage
  names to `RunState.artifacts` keys; `final` is those artifacts dumped.
- **`warn` downgrades effects one step.** `PolicyDecision.downgraded_from` already exists
  (`guardrails.py:99`) and nothing sets it. `remediation.decide_plan` is where a downgrade
  belongs; `plan_verdict` already turns `require_approval` into a suspension.
- **Every citation has a checkable shape.** `Citation{claim_kind, locator, quote, note}`
  with the five `claim_kind`s PLAN names; `FailureBundle.dependency_changes`,
  `DiffSummary.files[].path`, `LogExcerpt.excerpt`, and `fingerprint.anchor_lines()` (the
  `test_in_log` check should use exactly that function — it is the anchor set the
  Investigator, the Context Manager and the fingerprint all agree on, section headers
  included since the fix round).
- **The `escalation` table and the `db` channel are built.** `SqliteMemoryStore.save_run`
  files the outcome's `EscalationRecord` under channel `db`; `ESCALATION_CHANNELS =
  ("log", "db")` in `deps.py`. Phase 4 adds `webhook`: `Settings.escalation_webhook_url`
  exists (a `SecretStr`, so it is redacted), nothing reads it. `EscalationRecord.channels`
  is the list the orchestrator records; `delivered_at`/`delivery_error` are the webhook's.
- **Fault injection has one entry point and one fault.** `Settings.fault_inject` (dev-only,
  refused otherwise at construction — `deps.build_memory_store`), spelled `sqlite_locked`.
  Phase 4's faults live in the LLM client, not the store: the guard and the "unknown
  fault" refusal need to move up to `AppContext` (or a `faults.py`) so one variable can
  name a store fault *or* an LLM fault. `llm_bad_json:N` should replace the first N
  responses *without calling Gemini* — fault-injected runs must not spend quota.
- **The confidence path and memory are coupled at the Diagnostician.** `Diagnostician.run`
  calibrates, then `_remember` writes `upsert_signature` + `record_observation` with the
  calibrated confidence — *before* any Evaluator has run. See §5, first item.
- **`Orchestrator(run_budget_s=240)`.** Phase 2 measured 40–110 s per live replay. A fourth
  stage with no model call adds nothing; the pending-retry probes (up to three gateway
  reads per run, each with the gateway's own timeouts) already sit inside the budget.

---

## 5. Design decisions Phase 3 made that Phase 4 will bump into

1. **The verdict is tallied in memory before it is evaluated.** The Diagnostician's
   `_remember` runs inside the `diagnose` stage. An Evaluator that refutes the diagnosis
   (`fail`, −0.15) will find the refuted verdict already counted in `verdict_counts` and the
   observation already written at its pre-penalty confidence. `upsert_signature(verdict=None)`
   (item 12) cannot un-count. Options, in order of preference: move the memory write to
   after evaluation (the orchestrator, or an `evaluate` suspend hook that writes; the
   Diagnostician keeps `verdict_threshold` and passes `None` for a refuted verdict too);
   or have the Evaluator record a correcting observation. **Decide this before building
   the stage** — it is the one place Phase 4 changes Phase 3's semantics.
2. **`verdict_threshold` is the remediation gate's number.** `build_agents(
   escalation_threshold=...)` hands `settings.escalation_threshold` (0.70) to the
   Diagnostician so a verdict the gate would refuse is a sighting, not a verdict. If the
   Evaluator's penalty is applied before the gate, the threshold comparison must be made on
   the post-penalty confidence — another reason for option 1 above.
3. **The prior's words are in `history.py`, its numbers in `memory.py`.** `likely_flaky`
   requires the *most recent resolved* retry to have passed (item 11); `memory_agreement`
   is withheld while the last rerun `failed_again`. The Evaluator does not touch these, but
   an eval harness that seeds histories must seed `action_outcome` rows, not just counts.
4. **The base64 scrub is a second line, not a policy** (backlog). A drafted file that
   reproduces a credential is rewritten at rest and on the wire. The right remedy is an
   Evaluator-side or plan-time guardrail that *refuses* the plan with a named reason; PLAN's
   Evaluator checks citations, not drafts, so this is an addition to argue for in the
   dispatch, not a given.
5. **Recovery's `max_output_tokens × 1.5` on a `MAX_TOKENS` retry needs `retry_structured`
   to reach the request budget**, which it cannot today (`agent.py:228` fixes it per
   agent; the Remediator's 8 192 is a guess that has worked twice). Phase 2's backlog
   deferred this to "the phase that next touches Recovery" — that is this one.
6. **Finding 10 is open on purpose.** The retry cap is check-then-act across runs (three
   deliveries in a minute can each read `0`). Closing it is a reservation protocol; the
   backlog says do not narrow the window and call it fixed. Phase 4's eval harness with
   `--concurrency > 1` will *exercise* it — expect the count to overshoot the cap under
   concurrency and report that, rather than treat it as a Phase 4 bug.

---

## 6. Residuals worth knowing (full text in `docs/progress/phase-3/backlog.md`)

- Verify step 3 needs a fresh database; step 4's expectation is the amended
  escalated/degraded shape.
- `annotate_run` is recorded, never executed.
- `infra_timeout`'s pending retries never resolve (no attempt-2 fixture, by design); the
  probe's worst case is three gateway reads inside the run budget.
- Dry-run retries count against the cap; a `HARNESS_DRY_RUN=false` flip inherits ≤24 h.
- `list_runs?status=failed` excludes superseded rows; the superseded row's escalation names
  no successor.
- A run saved after a failed claim carries `unclaimed:<id>`; dedup for that key is gone.
- Runtime corruption degrades, never quarantines; startup quarantine logs, no escalation.
- The `fresh` nonce is 32 bits; the exact chain rule keeps a collision off the real key.
- Stale Docker container on `0.0.0.0:8000`: `docker compose down` first; use `127.0.0.1`.

---

## 7. How to work on this codebase

Unchanged from the Phase 3 handoff §7, with two additions:

- **Never `git checkout -- <file>` to undo an experiment on a file with uncommitted edits.**
  The Phase 3 fix round lost every edit to `memory.py` that way and re-applied them from a
  scratchpad script. Copy the file to the scratchpad, experiment, copy it back. For "does
  this test fail on the old code", `git stash push -- src scripts`, run the tests, `git
  stash pop` — it worked cleanly five times.
- **Prove each fix's test against the pre-fix tree** and say so in `test-verifier.md`. The
  audit's value is the confirming input; a test that also passes on the old code pins
  nothing.
- The standing rules: one independent audit per phase, the fix round coordinator-verified
  and recorded as such; subagents are the dominant token cost (the Phase 3 audit alone was
  ~230 k), build inline; budget every live call; Bash heredocs mangle backslash escapes (a
  doubled backslash collapses, an escaped newline becomes a real one — the fix round hit it
  again while writing this very paragraph) — write patch scripts to the scratchpad with the
  Write tool and run them with `python <file>`.

## 8. Constraints that outlive Phase 3

Unchanged: `git add` new files explicitly; a mounted sub-app gets no lifespan events
(`app.py:main()` hand-calls, now three: prompt validation, `initialize()`, the app);
`app.py` must not read `os.environ`; the Space is pinned to `zero-a10g`; never print the HF
token; `uv.lock` is the pin of record; ask before changing exposure or deploying.

## 9. Definition of done for Phase 4

`PLAN.md`'s Phase 4 Verify block. Steps 2 and 3 are fault-injected replays — free if the
faults are injected at the client and never reach Gemini; step 4 (`scripts/eval.py --runs
5`) is **twenty replays, sixty model calls: three days of free-tier quota** as written.
Plan for it: either a `--llm stub` mode that the README number is honest about, or a paid
key for the one run, and say which in `verify.md`. A phase is done when `test-verifier.md`
says PASS and `review.md` says SHIP (or, after a fix round, records the coordinator's SHIP
with its basis); tag `phase-4-green` on the tree that is actually finished.

## 10. Read order for a fresh session

1. This file.
2. `PLAN.md` Phase 4, A.6, A.7, the Phase 2 amendment 2, the Phase 3 amendment (all 17).
3. `docs/progress/phase-3/backlog.md` — residuals, the open finding, audit provenance.
4. `docs/progress/phase-3/dispatch.md` — the decisions the Phase 3 build was made against.
5. `src/integrations/cicd/wiring.py` (`build_stages`, `build_agents`),
   `src/integrations/cicd/agents/diagnostician.py` (`_calibrate`, `_remember`),
   `src/integrations/cicd/remediation.py` (`build_facts`, `decide_plan`),
   `src/harness/recovery.py`, `src/api/deps.py` (`build_memory_store`) — what exists.
6. `docs/progress/phase-3/review.md` only if a finding number comes up.
