# Handoff — start of Phase 2 (Remediator + Guardrails)

Written 2026-09-11, at `phase-1-green` (`d2f7c6e`). Phase 1 is closed: gate green, three
independent audits, zero open findings.

**Read this file first and in full.** It is the index. It does not restate `PLAN.md` or the
audit documents — it tells you which parts of them are load-bearing for Phase 2, and it
records the things that are true about this codebase but written down nowhere else. Several
of them cost the last session hours to discover.

---

## 1. Deploy state — held, and this is the first thing to decide

The Hugging Face Space is **still serving pre-fix Phase 1 code**. It has never been
redeployed since before the fix rounds, deliberately.

- The live URL is serving an **unredacted ~40 KB log**. No fixture carries a credential
  today, so this is exposure-in-waiting rather than an active leak — but it is the reason
  the hold exists.
- Every fix that closes it is committed and tagged. Redeploying is now safe from the code's
  side.
- **Do not redeploy without asking.** The hold was the user's call, and `phase-1-green`
  being tagged does not lift it on its own. Ask; do not infer.

---

## 2. What Phase 2 is

From `PLAN.md` §"Phase 2 — Remediator (retry only) + Guardrails": *the system takes its
first real action, and cannot take a dangerous one.*

Built in this slice: `harness/guardrails.py` (the engine), the orchestrator's third stage
plus the approval-pending suspend path, `integrations/cicd/agents/remediator.py`,
`gateway_github.py` (read tools plus `rerun_failed_jobs`), `POST /v1/approvals/{id}`,
`GET /v1/escalations`, and fixtures 2 and 3 (`flaky_test`, `infra_timeout`).

Read `PLAN.md:384-525` end to end before dispatching anything. Appendix A.7 is the frozen
model transcription; Appendix B.2 is the GitHub failure matrix.

---

## 3. Where the tree actually stands — more is built than the plan used to imply

`PLAN.md` described Phase 2 as if guardrails start from nothing. They do not; Phase 0 scaffolded the
contracts. The plan now says so (Phase 2 amendment 1), and the real starting surface is:

| File | State |
|---|---|
| `src/harness/guardrails.py` | 86 lines. `Condition`, `Rule`, `PolicySpec`, `ActionContext`, `PolicyDecision` **fully transcribed**, plus `MAX_SIDE_EFFECTING_ACTIONS_PER_RUN = 1`. Only `PolicyEngine.__init__` and `PolicyEngine.decide` raise `NotImplementedError`. |
| `src/integrations/cicd/policy.yaml` | 45 lines, **already committed verbatim** from the plan. Read §4 before you touch it — one rule is wrong. |
| `src/harness/memory.py` | 112 lines. All models plus the `MemoryStore` Protocol. No implementation — that is Phase 3. |
| `src/harness/evaluator.py` | 69 lines, two `NotImplementedError`s. Phase 4. |
| `src/integrations/cicd/agents/remediator.py` | 3-line placeholder. |
| `src/integrations/cicd/gateway_github.py` | 4-line placeholder. |

So the first real work is `PolicyEngine.decide` and the two placeholders — not model
transcription. Check the file before you brief an agent to write something that exists.

Gate at the tag: **392 passed, 1 skipped**; `ruff check` clean; `mypy --strict src/harness`
clean on both 1.14.1 and the pinned 2.3.1. `ruff format` is *not* part of the gate (41 of 71
files would be reformatted; do not "fix" this).

---

## 4. Two defects that were in Phase 2's plan — now amended, one still needs a code sync

Both were found while closing Phase 1, and neither is in any audit document — the audits checked
code against the contract, not the contract against itself. **`PLAN.md` has been amended for both**
(Phase 2 amendments 2 and 3, plus a corrected Deferred paragraph). What follows is why, because the
amendment tells you the answer and not the reasoning.

One of them leaves a code change owed: `src/integrations/cicd/policy.yaml` still carries the old
clause. **Syncing it is Phase 2's first task** — it was left undone deliberately, because editing
it is Phase 2 work and Phase 1's closeout stopped at the plan.

### 4.1 `open-fix-pr` could never match, so Verify step 2 could not pass

`policy.yaml:33` gates `open-fix-pr` on `evaluation.verdict: {eq: pass}`. Phase 2's own
**Deferred** paragraph says the Evaluator is not built and `evaluation.verdict` is the
literal `"skipped"`. `"skipped" != "pass"`, so the rule never matches; no other rule matches
a write tool; `default_effect: deny` answers instead.

Verify step 2 expects `{"status":"awaiting_approval","effect":"require_approval",...}` and
will get a deny. Note that `retry-suspected-flaky` was written `{in: [pass, skipped]}` and is
fine — so this reads as an oversight in one rule, not a deliberate gate.

**Resolved in `PLAN.md` by widening the rule to `{in: [pass, skipped]}`**, with a comment naming
Phase 4 as when `skipped` comes back out. The two options were not equivalent: widening means an
unevaluated diagnosis can reach `require_approval`, which is defensible only because a human still
approves before anything executes. The alternative — having the deferred Evaluator return `"pass"`
— is worse, because it fabricates a verdict nobody computed, and `"skipped"` is load-bearing
precisely because it is honest.

**Owed:** apply the same one-line change to `src/integrations/cicd/policy.yaml:33`.

### 4.2 The retry-count stub would re-open a fail-open Phase 1 just closed

`policy.yaml:24` gates `retry-suspected-flaky` on
`memory.retries_for_signature_24h: {lt: 2}`, and Phase 2's Deferred paragraph says the
retry-count guard "reads a stub returning 0". A stub returning `0` makes that clause
**always true**, so the retry cap does not exist in Phase 2 — it only looks like it does,
including in the trace, which will quote the rule as if the clause bit.

This is the same defect Phase 1 closed twice at the layer below. `review.md` finding 11
closed it in `PriorHistory`; final-audit finding 2 then found that fix had an escape hatch
which fell open on exactly its motivating shape, and closed it again unconditionally
(`schemas.py`, `FAIL_CLOSED_RETRIES_IN_24H`). Wiring a 0-returning stub into the policy
namespace hands the same fail-open back one layer up.

**Resolved in `PLAN.md`: the stub returns `FAIL_CLOSED_RETRIES_IN_24H` (999), not 0.** Phase 2 then
demonstrates "the cap denies" rather than "the cap is unreachable", which is the honest thing to
show and matches `default_effect: deny`; Phase 3 replaces a constant with a real query rather than
changing a behaviour.

**This costs you Verify step 1, and that is a real trade, not a technicality.** With the cap failing
closed, `retry-suspected-flaky` denies, so "flaky scenario auto-retries" is not demonstrable in this
phase. Either inject a memory stub returning a real count under 2 for that one scenario, or assert
the deny here and move the auto-retry demonstration to Phase 3 where memory is real. The plan's
phase-summary table records the same choice. Pick one and write down which — the 90-case deny test
is unaffected either way and remains the load-bearing half of this phase's gate.

### 4.3 `PLAN.md` was reconciled with the built tree at the close of Phase 1

The plan had drifted in ways no audit caught, because every audit checked *code against the
contract* and none checked the contract against itself. Amendments made, all doc-only:

- **Deployment target.** `PLAN.md` named Fly.io in 22 places and Hugging Face in none. Fly began
  requiring payment information before it would create an app, so Phase 1 substituted a Gradio
  Space and recorded it only in `docs/deploy-huggingface.md`. Every `fly deploy` /
  `<app>.fly.dev` line in a Verify block pointed at a URL that has never existed. `fly.toml` and
  the `Dockerfile` are kept — Fly is still *a* target, just not the live one.
- **Appendix D risk 2 was backwards.** It ended "Render's free tier has no persistent disk, which
  is why Fly is the recommended first target" — written when Fly *was* the target. The Space has
  no persistent disk either, so `data/harness.db` resets on every restart or sleep. **This is a
  Phase 3 design input, not a footnote:** cross-run memory is that slice's entire point, and on
  the live target it would be amnesiac, making its Verify step 2 unreachable there.
- **Risk 11** assumed Fly's ~2–5 s cold start against GitHub's 10 s webhook timeout. A sleeping
  Space wakes considerably slower, so for Phase 5 exceeding that timeout is the expected case
  rather than a near miss — which is what makes Phase 5's idempotency work load-bearing.
- **Recovery was mis-filed.** Phase 1 listed it as deferred and Phase 4 claimed to build it. It
  shipped in Phase 1, wired into every agent call at `src/harness/agent.py:234`, and three audit
  rounds went into its failure classification. Both lists corrected; what Phase 4 still owes is
  Recovery's *Verify* block, whose `HARNESS_FAULT_INJECT` paths have no coverage yet.
- **The repo layout** listed `fly.toml` but neither `app.py` nor `requirements.txt`.

If you find more drift, amend `PLAN.md` rather than building against it — that is the standing rule
here, set in `0ffeef1`, and it is why the plan is still worth trusting.

---

## 5. What Phase 1 built that Phase 2 depends on

- **`diagnosis.final_confidence` is trustworthy now.** `review.md` finding 4 was closed by
  `_diagnosis_schema()` stripping `final_confidence` and `confidence_adjustments` from the
  wire schema, so the model is never asked for the two fields A.11 marks harness-added. Both
  `open-fix-pr` and `retry-suspected-flaky` threshold on that field; before the fix they
  would have been reading a value the model could influence.
- **A required-`forbidden` gateway is the established pattern.** `review.md` finding 12 made
  `gateway_replay.py`'s `forbidden` a required keyword argument, so the authoritative safety
  re-check cannot be skipped by omission. **`GitHubToolGateway` must follow it** — that is
  the gateway where the re-check actually stops something, and PLAN.md's
  `test_gateway_refuses_forbidden_even_with_forged_allow_decision` is the test that proves
  it. A default-empty `forbidden` there would make that test pass vacuously.
- **`AdditionalToolCallOutcome`** records optional tool calls per call
  (`obtained` / `refused` / `failed`). Phase 2 makes those calls real; the shape is already
  in Appendix A.
- **Upstream LLM failure ends the run.** The Investigator returns `output=None` on
  `_UPSTREAM_ERROR_KINDS` and `orchestrator.py:299-318` escalates and breaks. A run that
  cannot investigate no longer proceeds to diagnose, or to remediate against nothing.

---

## 6. Two design decisions that are the user's, not yours

Neither was built in Phase 1, deliberately. Both get sharper in Phase 2, because Phase 2 is
the first phase that takes an action.

1. **There is no run-level wall-clock bound.** `RETRY_DELAY_BUDGET_S` bounds cumulative
   *sleep*, not call duration. There is no `asyncio.wait_for` anywhere in `src/`, uvicorn has
   no request timeout configured, and `app.py`'s `timeout=300.0` is inert. A provider that
   fails *slowly* rather than fast can still hold a request for minutes.
2. **`POST /v1/runs`'s fire-and-forget `_execute` is unsupervised.** It fails invisibly after
   the 202 and leaves the registry row `in_progress` forever. No HTTP handler can catch it —
   the response is already sent. Phase 2 adds a suspend-for-approval path, which makes a
   stuck `in_progress` harder to tell apart from a legitimate wait.

Raise both; do not build either unilaterally.

---

## 7. Residuals carried in from Phase 1

Full text in `docs/progress/phase-1/backlog.md` §"Residuals and hazards". The ones that touch
Phase 2:

- **A rate-limited run serves `final == {}`.** Ending the run on upstream failure is correct
  and is what the audit asked for, but the collected `FailureBundle` and the degraded
  `investigator_notes` no longer reach the served `RunOutcome` (`AgentResult.evidence` is read
  by nothing downstream). With a 20-request/day quota **this is the demo's normal daily
  output.** If Phase 2's demo needs to show anything on a quota-exhausted day, this is the
  thing to fix, and the fix is a bundle field rather than a confidence adjustment.
- **One test skips because `gradio` is deliberately absent from the lockfile.**
  `test_app_py_main_hand_calls_validate_prompt_templates_before_launch` is the only thing
  pinning the Space's startup hand-call. It was verified non-vacuous under an ephemeral
  `uv run --with gradio` overlay that touched neither the venv nor the lockfile. Running it
  unconditionally means adding `gradio`/`spaces` to the dev group, trading away the isolation
  `requirements.txt` argues for. A real trade, owned by whoever owns `pyproject.toml`.
- **The 20-second wall-clock test** in `tests/unit/test_retry_delay_budget.py` takes the suite
  from ~6 s to ~27 s. It buys real-time proof against the shipped constants, which the mocked
  tests beside it cannot. Revisit the first time someone skips the suite because of it.

---

## 8. Audit provenance — what is owed

Two spans of Phase 1 are **coordinator-verified, not independently audited**, and are recorded
that way rather than left to look audited:

- `dcf480f..830332e` (the RFC 9457 error-path round). The reviewer hit a session limit; the
  coordinator ran the five checks it had been briefed to run.
- `862e8e0..d2f7c6e` (the final fix round itself). Better evidenced than the above — each of
  the six fixes carries a test shown non-vacuous by reproducing the defect under the *old*
  code, not merely by passing under the new — but still the author checking their own work.

An independent pass over either is cheap, and is the first thing to spend on if anything in
the error paths misbehaves.

---

## 9. How to work on this codebase

Read this section even if you think you know how you work. Every item cost the last session
real time.

- **Every fix round in Phase 1 introduced a new defect, and every one was caught only by an
  independent audit** — never by the builder, never by the test gate, never by reading the
  diff. The citation-leak fix introduced an `Allow`-header regression. The eager-validation
  fix was dead on the Space. The finding-11 fix shipped an escape hatch that fell open on its
  own motivating case, pinned by a test that read as thoughtful. **A green suite is not
  evidence when the assertion itself encodes the defect.** Do not close a phase on a tree
  whose last fix round has had no independent read-only pass.
- **Subagents are the dominant token cost.** The user asked explicitly for them to be
  minimised. Reserve dispatch for two cases: work inside another agent's exclusive write
  territory, and the phase-closing audit. Do the rest inline. When you do dispatch, name
  specific suspicions rather than briefing an open-ended sweep, and specify a terse report
  format up front — a mid-flight `SendMessage` can tighten a running agent, and is far
  cheaper than killing and re-dispatching.
- **Agents will not write outside their territory, including report files.** `harness-core`
  correctly declined to write into `docs/progress/`. The coordinator records; the agent
  reports. Brief accordingly.
- **Live Gemini quota is 20 requests/day, free tier. One replay = 2 requests.** All three
  Phase 1 audits reached their verdicts spending none. Reason from code; run one targeted
  test rather than a suite.

---

## 10. Constraints that outlive Phase 1

- **`git add` new files explicitly, and run `git diff --cached --name-status` before
  committing** anything that turns a module into a data file. A `git commit -a` nearly shipped
  a broken Space when the `prompts/*.md` files sat untracked beside deleted `.py` ones with
  nothing staging the replacements.
- **A mounted sub-app gets no lifespan events.** `app.py:228`'s `demo.app.mount("/", api)`
  means anything that must happen at Space startup needs a hand-call in `app.py`'s `main()`,
  not just a FastAPI lifespan hook. This has already caught two separate pieces of startup
  work. Assume it will catch the third.
- **`app.py` must not read `os.environ`** — `tests/unit/test_no_env_access.py` scans it.
- **The Space is pinned to `zero-a10g`** and cannot leave it. See
  `docs/deploy-huggingface.md` §2.
- **Never print or pass the HF token on a command line.**
- **`uv.lock` is the pin of record** (mypy 2.3.1), the `Dockerfile` uses `uv sync --frozen`,
  and `pyproject.toml` carries a `mypy>=1.18.2` floor. A system mypy will disagree with the
  gate; trust `uv run mypy`.

---

## 11. Definition of done for Phase 2

`PLAN.md:481-521` is the Verify block — five steps, of which step 2 cannot pass until §4.1 is
resolved, and step 5 spends live GitHub calls. The house rule from `docs/progress/README.md`:
a phase is done when `test-verifier.md` says PASS and `review.md` says SHIP, then tag
`phase-2-green`.

One addition to that rule, learned the hard way this phase: **the tag goes on the tree that is
actually finished.** `phase-1-green` sat on a tree with seven known-open findings for three
commits, which is a misleading thing for a green tag to name. Move it, or cut it late.

---

## 12. Read order for a fresh session

1. This file.
2. `PLAN.md:384-525` (Phase 2) and Appendix A.7 (`PLAN.md`, guardrails models).
3. `docs/progress/phase-1/backlog.md` — the closed record, residuals, settled decisions.
4. `src/harness/guardrails.py` and `src/integrations/cicd/policy.yaml` — what already exists.
5. `docs/progress/phase-1/review.md` and `review-2.md` only if a specific finding number
   comes up. They are history; the backlog is the index.

`docs/progress/phase-1/handoff-fixround2.md` is superseded by the backlog and by this file.
It describes a mid-phase state that no longer exists — do not work from its open list.
