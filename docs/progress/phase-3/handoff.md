# Handoff — start of Phase 3 (Memory)

Written 2026-09-11, at `phase-2-green`. Phase 2 is closed: gate green (492 passed, 2 skipped),
one independent audit with six findings all closed, the closing fix round coordinator-verified
at the user's direction (`docs/progress/phase-2/backlog.md`, "Audit provenance").

**Read this file first and in full.** It is the index. It does not restate `PLAN.md` or the
Phase 2 documents — it tells you which parts are load-bearing for Phase 3 and records what
is true about this tree but written down nowhere else.

---

## 1. Deploy state

The Space at <https://shakti-agent-harness.hf.space> serves the `phase-2-green` tree, pushed
as a fast-forward of the Phase 2 commits (`85a168b..d7e4cb9`, plus this docs commit).
Verified live after the push, not assumed:

| Check | Result |
|---|---|
| `GET /healthz` | `{"status":"ok","db":"ok","version":"0.1.0"}` |
| `GET /readyz` | `policy_loaded: true` (was `false` on every earlier build) |
| `POST /v1/replay/flaky_test` | `escalated` / `policy_denied`; `flaky_test` at 0.85; plan `retry_job`; decision `<default>` / `deny`; 46 s, one attempt per stage |
| `GET /v1/escalations` | the run above, `reason: policy_denied` |
| `GET /v1/runs/{id}/trace` | 12 spans, including `policy.decide` and `remediation.plan` (component `guardrails`) |
| Served body | 7 559 bytes, zero credential-shaped matches |
| `POST /v1/approvals/apr_nope` | `404 application/problem+json`; a malformed body `422` |
| `POST /v1/runs` with `mode: live` | `501 Live mode not available`, naming `HARNESS_GATEWAY` |

One thing to know about `readyz` on a redeploy: the *old* container keeps answering while
the Space rebuilds, so for about a minute after a push `readyz` reports the previous build.
Poll for the field you changed, not for `200`.

**The exposure story is unchanged in kind and slightly wider in surface.** Still no
authentication on any endpoint. Two write endpoints are new: `POST /v1/approvals/{id}`
(anyone who learns an approval id can approve or reject it) and `POST /v1/runs` in live
mode. Both are inert on the Space: `HARNESS_GATEWAY=replay` and `HARNESS_DRY_RUN=true` are
the defaults, the replay gateway synthesises every write, and no repository is allowlisted.
An approval on the Space therefore executes nothing real. Ask before changing any of that.

**Quota is the demo's binding constraint.** A replay is now *three* model calls, more when
Recovery retries (the day's worst run spent seven). The free tier's 20/day buys about six
replays. The error-shape checks and `GET` routes cost nothing; prefer them while iterating.

---

## 2. What Phase 3 is

`PLAN.md` §"Phase 3 — Memory": *the fourth time a test flakes, the system says so instead of
re-deriving it.* Built: `harness/memory.py` (`SqliteMemoryStore` + migrations),
`integrations/cicd/fingerprint.py`, the Investigator populating `PriorHistory` from the
store, the Remediator writing an `observation` row, `memory.retries_for_signature_24h`
becoming a real count, the `cold_start` fixture, the `sqlite_locked` fault injection.

Read `PLAN.md`'s Phase 3 section end to end, plus A.5 (memory models and the `MemoryStore`
Protocol), B.3 (the SQLite failure matrix — the fail-closed 999 rule is already enforced one
layer up, see §4), and Appendix C (the claim protocol: `run` table, heartbeat, takeover).

---

## 3. Where the tree stands

| File | State |
|---|---|
| `src/harness/memory.py` | 112 lines: every A.5 model plus the `MemoryStore` Protocol. No implementation. No `migrations/` directory exists. |
| `src/harness/observability.py` | Owns the only SQLite schema today (`spans`), created by `TraceRecorder.initialize()`. PLAN's `001_init.sql` names a `trace_span` table with different columns. **Decide whether the recorder migrates to the shared schema or keeps its own table** — do not leave two span tables. |
| `src/api/run_registry.py`, `src/api/approval_registry.py` | In-process, both docstrings say "replaced in the memory phase". `RunRegistry` holds `RunOutcome`s; `ApprovalRegistry` holds `ApprovalRequest` + a `RunContext` (mode, repo, scenario dir) needed to rebuild the gateway on approve. PLAN's `approval` table has `plan_json` but nothing for the context — **add columns or a sidecar; the approve route cannot execute without knowing which gateway to build.** |
| `src/api/main.py` `_background_runs` / `_supervised` | The in-process stand-in for the claim/heartbeat machinery (Phase 2 handoff §6.2). Replace with `MemoryStore.claim_run` + `heartbeat`, and keep the strong-reference set until then — `asyncio` holds tasks weakly. |
| `src/integrations/cicd/agents/investigator.py` | Constructs `PriorHistory(signature_id=None, unavailable=True)` unconditionally, with a comment naming this phase. Both prompts carry a "no prior history is available" paragraph to replace. |
| `src/integrations/cicd/remediation.py` `build_facts` | Reads `memory.retries_for_signature_24h` off `bundle.prior_history.retries_in_24h` and `memory.unavailable` off `.unavailable`. **Nothing in the Remediator changes when memory arrives**: put a real count in the bundle and the rule matches. |
| `src/integrations/cicd/fingerprint.py`, `claim_checkers.py` | 5-line placeholders. |
| `fixtures/scenarios/` | `real_regression`, `flaky_test`, `infra_timeout`. `cold_start` is a copy of `flaky_test` with an empty `find_last_successful_run` body (PLAN Appendix D). `scripts/gen_fixture_log.py` regenerates logs deterministically. |

Gate at the tag: **492 passed, 2 skipped**; `ruff check .` clean; `mypy --strict src/harness`
clean on 2.3.1. `ruff format` is still not part of the gate.

---

## 4. What Phase 2 built that Phase 3 depends on

- **The policy engine is real, and the retry rule is one fact away from matching.**
  `retry-suspected-flaky` needs `memory.retries_for_signature_24h < 2`; today it reads 999
  from the fail-closed `PriorHistory`. The Verify-step-1 demonstration Phase 2 deferred is
  simply: put a real count under 2 in the bundle for `flaky_test`, and the live replay goes
  from `escalated`/`policy_denied` to `completed` with `executed[0].tool == "rerun_failed_jobs"`.
  `tests/integration/test_remediation_stage.py::test_retry_executes_through_the_gateway_when_the_history_is_readable`
  shows exactly the bundle shape that does it.
- **Obligations are recorded, not executed.** `PolicyDecision.obligations` (`record_observation`,
  `annotate_run`) travel on the decision and the `policy.decide` span. Nothing acts on them.
  `record_observation` is Phase 3's — the Remediator (or `execute_plan`) should write the
  `observation` row when a side-effecting plan executes, with `action_outcome="pending"`
  until the re-run's result is known.
- **B.3's fail-closed rule is already structural.** `PriorHistory(unavailable=True)` implies
  `retries_in_24h == 999` on construction, `model_validate` and `model_copy`. A degraded
  memory read should produce exactly that object; do not re-implement the rule at the call
  site, and do not weaken it there.
- **`evaluation_verdict` is passed explicitly in two places** (`Remediator.__init__` and
  `main._execute_approved`), both `"skipped"`. Phase 4 changes both; Phase 3 should not touch
  either.
- **The approval route re-evaluates policy from the run's own `final`** (`Diagnosis` and
  `FailureBundle` re-validated from the stored `RunOutcome`). When memory is real, the count
  at approval time should be *fresh*, which means `_execute_approved` must re-query memory
  rather than reuse the bundle's stored `prior_history`. `review.md` finding 3's
  `policy_denied`-on-approve branch exists for this and is unpinned — pin it the moment it
  becomes reachable.
- **`AppContext` builds the gateway per run and closes it after.** A `MemoryStore` is a
  process-wide singleton like the recorder; wire it in `deps.get_app_context`, pass it into
  `build_orchestrator` like `engine`, and report on it from `readyz` (`db_writable` already
  exists; B.3's "file missing → migrations create it; `readyz` fails until they succeed").

---

## 5. Three design decisions Phase 2 made that Phase 3 will bump into

Full reasoning in `docs/progress/phase-2/dispatch.md`; the short forms:

- **The action cap counts executed plans, not tool calls** (decision 5). The Remediator
  supplies `run.side_effecting_actions_so_far = 0` and the approval route the same. If
  Phase 3 lets a run act twice (it should not), that fact has to become real.
- **Tool calls are derived from the drafts** (decision 7, revised). The `retry_job` call is
  built from `bundle.job.run_id` / `run_attempt`, never from the model. A proposed call
  outside the action's tool set is dropped and recorded on the `remediation.plan` span; a
  proposed *forbidden* call is always judged so the run escalates.
- **Three words for three outcomes:** `denied` (policy), `rejected` (a person),
  `tool_failure` (execution). `RemediationResult.status` gained `rejected` in A.11.

---

## 6. Residuals worth knowing (full text in `docs/progress/phase-2/backlog.md`)

- The Space has no persistent disk: `data/harness.db` resets on restart or sleep. **Phase 3's
  cross-run memory is amnesiac on the live target**, so its Verify step 2 ("the 4th flaky run
  shows `occurrences=3`") is demonstrable only within one waking period, or locally. Design
  input, not a footnote — decide early whether the demo runs locally or the Space gets a
  persistent volume (a paid tier, the user's call).
- `DEFAULT_RUN_BUDGET_S = 240` was re-measured with three stages: 40–51 s clean, 110 s with
  seven model attempts. Unchanged.
- The Remediator's 8 192 output budget worked twice; a large fix could still hit
  `MAX_TOKENS`, and Recovery cannot raise the budget from inside `retry_structured`.
- The rerun 403 "already in progress" match is unverified against the real endpoint; a miss
  is loud (`tool_failure`).
- Approving a fix PR always ends in `tool_failure` until the PR tools exist (Phase 5).

---

## 7. How to work on this codebase

Unchanged from the Phase 2 handoff §9, with one addition:

- **Every fix round needs an independent audit; when the user waives it, say so in the
  backlog.** Phase 2's closing round is recorded as coordinator-verified. If the approval
  route or the log download path misbehaves, an independent read of `17fd4b5..88afccb` is the
  first thing to spend on.
- **Subagents are the dominant token cost.** Phase 2 was built inline with one reviewer
  dispatch (~230 k tokens for the audit alone). Reserve dispatch for the audit.
- **Budget every live command.** Three calls per replay; `GET`s and error-shape checks are
  free.
- **Bash heredocs mangle backticks and `\\n` in this environment.** Write patch scripts to
  the scratchpad and run them with `python <file>`; the Phase 2 session lost two rounds to
  this before noticing.

## 8. Constraints that outlive Phase 2

Unchanged from the Phase 2 handoff §10: `git add` new files explicitly; a mounted sub-app
gets no lifespan events (`app.py:main()` hand-calls — **a migration step at startup needs a
third hand-call there**, the Space will not run a lifespan hook); `app.py` must not read
`os.environ`; the Space is pinned to `zero-a10g`; never print the HF token; `uv.lock` is the
pin of record.

## 9. Definition of done for Phase 3

`PLAN.md`'s Phase 3 Verify block. Note step 2 needs four sequential live replays of
`flaky_test` (twelve model calls, more with retries) — most of a day's quota; plan the day
around it. A phase is done when `test-verifier.md` says PASS and `review.md` says SHIP; tag
`phase-3-green` on the tree that is actually finished.

## 10. Read order for a fresh session

1. This file.
2. `PLAN.md` Phase 3, A.5, B.3, Appendix C, Appendix D.
3. `docs/progress/phase-2/backlog.md` — residuals, settled decisions, audit provenance.
4. `docs/progress/phase-2/dispatch.md` — the decisions the Phase 2 build was made against.
5. `src/harness/memory.py`, `src/integrations/cicd/remediation.py` (`build_facts`),
   `src/api/approval_registry.py` — what exists.
6. `docs/progress/phase-2/review.md` only if a finding number comes up.
