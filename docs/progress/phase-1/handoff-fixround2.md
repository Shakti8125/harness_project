# Handoff — Phase 1, after the Wave 3 fix round

> **Superseded (2026-09-11). Historical record — do not work from it.** Every finding it
> lists as open is closed, and §1's deploy hold is lifted: the Space serves `82d79de`, the
> `phase-1-green` tree. It is kept unedited because it is the record of a state that
> existed, in the same spirit as the miscounted verdict line in `review-2.md`. The current
> index is `docs/progress/phase-1/backlog.md`; the entry point for the next phase is
> `docs/progress/phase-2/handoff.md`.

Written 2026-09-09 at `87cb276`, working tree clean. Read this with
`docs/progress/phase-1/review.md` (the 12-finding Wave 3 audit) and
`docs/progress/phase-1/review-2.md` (the fix-round re-audit) open; this note is the
index over both, not a replacement for either.

**Nothing has been dispatched for any open finding.** The plan in §4 is agreed but
unstarted. No agent is running.

---

## 1. Deploy state — held, deliberately

The Space at <https://shakti-agent-harness.hf.space> **still serves pre-fix code**. It has
not been redeployed since before the fix round.

That is a decision, not an oversight. Pushing the current tree would fix the log exposure
and simultaneously ship the high finding below — a rate-limited run that holds a connection
for minutes where it used to fail in seconds. The re-audit's answer was explicit: *do not
redeploy without a run-level bound.* One deploy, after the urgent bundle lands.

Consequence while held: the live Space serves an **unredacted** `RunOutcome.final` — the
whole raw job log, ~40 KB, on a public unauthenticated URL. No fixture contains a
credential today (`grep -rE "gh[pousr]_|AIza|xox" fixtures/` is empty), so this is
exposure-in-waiting rather than a live leak, but it is the reason the redeploy should not
drift.

## 2. Fixed and committed

| Commit | What |
|---|---|
| `0ffeef1` | Standing instruction added to all six agent definitions: when an implementation diverges from an explicit PLAN.md decision, surface it as a finding for the coordinator's call rather than building to the agent's own judgment. |
| `e4f7f44` | The fix round — `review.md` findings 1, 2, 3, 10, plus the user's `llm_upstream` ruling. |
| `cff5ede` | `review-2.md`, the re-audit, verbatim. |
| `87cb276` | Doc corrections: the 429 claim and the stale span count. |

**Closed outright:**

- **Finding 1** — `gateway_degraded`'s −0.10 fires only on a failed *required* read.
  Optional model-requested calls and policy refusals accumulate separately.
  `Adjustment.reason` no longer asserts something false. The 0.95 → 0.85 regression is
  gone, verified in both directions (a genuinely failed required read still fires it).
- **Finding 2** — `Retry-After` is extracted and honoured; `recovery.py:237-241`'s
  preference branch is live. The delay is in the 429 **body** as `google.rpc.RetryInfo`
  `retryDelay`, a protobuf duration string — *not* an HTTP header. Both SDK error shapes
  (envelope and inner) are handled. **This fix is also the source of the high finding
  below.**
- **Finding 10** — prompts are versioned `prompts/*.md` with `version:` front matter, per
  PLAN.md:202-206 and repo layout line 118.
- **`llm_upstream`** — added to `EscalationRecord.reason`. Four-way agreement confirmed
  across `contracts.py`, `orchestrator.py`'s parallel `Literal`,
  `_OUTCOME_FOR_ERROR_KIND`, and PLAN.md Appendix A. The PLAN.md edit was the single
  authorised one: two lines, entirely inside the `Literal`, B.1:1576 untouched.

**Partially closed:**

- **Finding 3** — the two fields the audit named (`logs[].excerpt`, `diff.files[].patch`)
  are gone from the served response, replaced by `_length` + `_sha256`, 47,346 → 5,698
  bytes, gated against a real built container. **The threat model it describes is still
  reachable** by a second path — see `review-2.md` finding 2 below.

**Gate:** Wave 2 returned PASS — 316 tests (247 + 69 new), `ruff` clean,
`mypy --strict src/harness` clean. The re-audit re-ran all three itself rather than
trusting the report, and re-derived the test arithmetic independently.

## 3. Open

Both verdicts from the re-audit are **FIX FIRST**. No `phase-1-green` tag has been cut and
none should be until this list is empty.

### New, from `review-2.md` (5)

1. **[high] Unbounded retry honouring + the `status="ok"` hardcode.** `src/harness/llm.py`,
   `recovery.py`, `src/integrations/cicd/agents/investigator.py:550`. Two compounding
   causes: `Investigator.run` returns `status="ok"` regardless of the LLM result, so a
   rate-limited Investigator does not end the run and the Diagnostician then runs and is
   rate-limited too; and each of the resulting 6 sleeps can be up to
   `MAX_RETRY_AFTER_S = 60.0`. Worst case **≈360 s**, against ~7 s before the fix.
   Every timeout that looks like it would bound this is inert — verified, not assumed: no
   `asyncio.wait_for` in `src/`, uvicorn has no request-processing timeout, and
   `app.py:50`'s `timeout=300.0` is a **no-op** (`httpx.ASGITransport.handle_async_request`
   contains zero occurrences of the string `timeout`). `max_concurrent_runs = 4` with the
   semaphore acquired *inside* `_execute`, so four such requests wedge the public API.
   On a public unauthenticated URL with a 20-request/day quota, this path is the **normal
   daily end state**, not an edge case.
2. **[medium] The finding-3 leak is still live** through `final.diagnosis.citations[].quote`
   and `final.bundle.notes.observations[]` — model-authored *verbatim copies of the same
   log*, because `prompts/diagnostician.md:16` instructs the model to quote verbatim and
   not paraphrase. Reproduced offline with a planted `ghp_` token at the same injection
   point the new test uses. The existing test cannot see it (its stub returns a fixed
   citation). `Citation.quote` is capped at 500 chars; `observations` is
   `Field(max_length=8)` — a cap on item *count*, not length, so an observation is an
   unbounded string.
3. **[medium] The prompt port turned a startup failure into a first-request failure.**
   `load_prompt_template` is called lazily inside `build_prompt`; `Orchestrator.run` has no
   `except`; `main.py` has no handler outside the health routes. A missing template gives a
   green `/healthz` and then `500 text/plain "Internal Server Error"` on the first replay —
   no RFC 9457 body, no `run_id`, nothing in the trace. Under the old Python-constant
   design this was structurally impossible (an `ImportError` at startup).
4. **[low] Optional tool calls are now unobservable.** `executed` is populated and never
   read; `result.data` is discarded; `additional_errors` is only logged. You cannot tell
   from a `RunOutcome` whether evidence the model asked for was obtained, refused, or 404'd.
   Bites in Phase 2, when `GitHubToolGateway` makes those up-to-three calls real.
5. **[low] `version: 1` names two different Diagnostician prompts.** The port dropped a
   backtick pair (`each {"claim_kind", ...}` vs the old `` each `{"claim_kind", ...}` ``),
   so the version that shipped to the live Space and the version in the tree differ.
   Undercuts finding 10's whole purpose on day one. Either restore the backticks or bump
   to `version: 2`.

> Counting note for whoever reads `review-2.md` directly: its PHASE 1 verdict line says the
> round "adds three more" findings, but the document lists five. Five is right — the
> verdict line appears to count only the two defects newly *introduced* by the fixes
> (1 and 3). The operative total is **13 open**: these 5 plus the 8 below.

### Untouched, from `review.md` (8)

Findings **4, 5, 6, 7, 8, 9, 11, 12** — deliberately not dispatched in the last round and
not since. Summarised, with the audit as the authority:

| # | Sev | One line |
|---|---|---|
| 4 | medium | `Diagnosis` schema asks the model for `final_confidence` / `confidence_adjustments`, which A.11 marks harness-added. Overwritten today; wrong one consumer away. |
| 5 | medium | No `RequestValidationError` handler — 422s return FastAPI's default JSON with the request body echoed, not RFC 9457. |
| 6 | medium | `problem()`'s `detail` does not pass through the `Redactor`, which A.12 requires. |
| 7 | low | `GET /v1/runs/{id}` and `.../trace` report different `degraded_components` for the same run. |
| 8 | low | `_ALLOWED_SCHEMA_KEYS` drops `maxLength`/`minLength`, so `Field(max_length=…)` is invisible to the model but enforced by Pydantic — burns repair attempts. |
| 9 | low | B.1's "empty candidates" half of the safety row is not terminal; retried the full 3 attempts. |
| 11 | low | `PriorHistory` degraded path leaves `retries_in_24h` at `0` instead of B.3's fail-closed `999`. Phase 2 wires the retry cap to it. |
| 12 | low | `gateway_replay.py`'s `forbidden: tuple = ()` makes the authoritative safety re-check opt-in at construction. |

Note findings 5 and 6 interact with new finding 3: the missing-template 500 is a fresh
instance of exactly the gap finding 5 describes.

## 4. The agreed dispatch plan — urgent bundle, not yet started

One wave, four parallel builders over disjoint write territories, then Wave 2, then a
re-audit. Scoped to what unblocks the redeploy; the 8 older findings stay out of it.

| Owner | Territory | Work |
|---|---|---|
| **harness-core** | `src/harness/**` | Bound the retry. Lower `MAX_RETRY_AFTER_S` 60 → **20 s** (caps the run at ~120 s, still honours the 5–20 s delays that are the common case) **and** add a cumulative per-run delay budget in `retry_structured` — once total slept exceeds it, stop honouring provider delays, fall through to exhaustion and the same `rate_limited` escalation. Also: add the `logger.info` on clamp (the re-audit overruled the "pure function on the hot path" argument — `retry_after_seconds` already calls `logger.debug` two frames down, and "we ignored the provider's stated hour" is the line you want in a postmortem for this exact finding). Also: one docstring line recording that a `retryDelay` in `{"seconds":…,"nanos":…}` object form is not parsed (not a JSON-over-HTTP wire form, so not a defect today). |
| **cicd-integration** | `src/integrations/**` | The `status="ok"` hardcode at `investigator.py:550` — a rate-limited Investigator must end the run rather than letting the Diagnostician burn four more attempts. This is half of the high finding and is the half that multiplies it. Also: startup validation for the templates, if it lands at `rendering` import rather than in the lifespan (coordinate with api-surface — one of you, not both). Also new finding 5, the `version: 1` divergence. Also the free win the re-audit surfaced: the Investigator's `prior_history_summary` says *"Treat this failure as a first sighting"*, instructing the model to do exactly what the Diagnostician's stand-in forbids two agents later — align the strings. |
| **api-surface** | `src/api/**`, `app.py`, packaging | The `Redactor` one-liner: `body = redactor.scrub(body)` at the end of `_serialize_run_outcome`, using `get_app_context().recorder.redactor`. Verified reachable — `Redactor` is already a recursive JSON scrubber (`observability.py:197-223`), `SECRET_PATTERNS` (`deps.py:49-55`) already covers `gh[pousr]_`, `github_pat_`, `AIza`, `xox[baprs]-` and bearer tokens, and `recorder.redactor` is public at `observability.py:276`. No new wiring. This closes new finding 2 *and* the whole "next raw-content field reopens it" class. **Keep the existing field-name walk** — it does a different job (bulk removal + digests, not credential removal). Also: template loading in `main.py`'s lifespan, if that is where startup validation lands. |
| **coordinator (me)** | `PLAN.md`, `docs/**` | The A.12 amendment. The re-audit wrote the paragraph and named the insertion point — immediately after PLAN.md:1555-1556, where response-body transformations are already stated. Text is in `review-2.md` under item (e), including the conditional final sentence about the `Redactor` (which only becomes true once api-surface's one-liner lands — if it does, keep it; if not, drop it and record the residual instead). |

**Tests go to test-verifier, not to the builders.** `tests/**` is test-verifier's exclusive
territory per both the skill's table and the agent definitions. Builders reproduce and hand
over recipes; test-verifier writes the durable tests in Wave 2. This split has already
caused one deviation from a literal instruction ("reproduce X as a test") and is worth
stating up front in each brief.

Specifically wanted from Wave 2 on this bundle: a test that the citation path cannot carry
a planted token (the fixture already exists in
`tests/integration/test_replay_response_scrubbing.py`, but its stub returns a *fixed*
citation — it needs a stub that quotes the log, which is what the prompt actually asks the
model to do), and a wall-clock assertion that a fully rate-limited run terminates inside
the new budget.

Then: Wave 2 gate → re-audit → **redeploy once** → re-verify against the live Space.

## 5. Constraints a fresh session will otherwise rediscover

- **Live Gemini quota is 20 requests/day, free tier.** One replay = 2. The re-audit spent
  none and reached both verdicts offline; assume the same is possible before spending. The
  quota resets daily — check what today has already cost before budgeting.
- **The redeploy sequence has a trap that already nearly fired.** The `prompts/*.md` files
  were untracked when the fix round was committed; `git commit -a` would have staged the
  *deletion* of the `.py` prompts without adding their replacements, shipping a Space that
  boots green and 500s on every request. They are committed now, but the shape of the trap
  recurs: `git add` new files explicitly and check `git diff --cached --name-status` before
  committing anything that moves a module to a data file.
- **`app.py` must not read `os.environ`** — `tests/unit/test_no_env_access.py` scans it.
  Port 7860 is a literal there for that reason.
- **The Space is pinned to `zero-a10g`** and cannot leave it (402 on downgrade,
  `isPro: false`, no free CPU tier on this account). That is why `app.py` carries a
  never-called `@spaces.GPU` stub and calls `demo.launch()` rather than driving uvicorn.
  See `docs/deploy-huggingface.md` §2.
- **Never print or pass the HF token on a command line** — use `build_hf_headers()` or the
  stored git credential.
- **Verify step 4** (escalation-threshold override) was substituted with offline coverage
  this round. The re-audit judged that sound and not worth live quota; it does not need
  re-running before the phase closes.

## 6. Decisions settled by the re-audit, recorded so they are not reopened

- **The `prompt.render` span ships as-is.** Don't rework it now. `AgentPrompt.prompt_version`
  is the cleaner mechanism and is harness-core's call, queued behind Phase 2 harness work.
  It changes the clean-run span count from 5 to 7 — both docs that said 5 are corrected in
  `87cb276`.
- **The dormant prompt clauses land with Phase 3**, worded around a real prior, together
  with `review.md` finding 11. Not speculatively now.
- **A refused write-tool request carries no confidence signal**, in either direction. No
  PLAN.md adjustment row covers it; inventing one would be scope creep. The visibility gap
  this leaves is new finding 4, and the fix there is a bundle field, not an adjustment.
- **Header-before-body precedence** for the retry delay is correct as built; the duplicated
  `EscalationReason` lists stay duplicated with the drift-guard test (deriving the alias at
  runtime would launder the `Literal` through `Any` and lose the mypy check that matters);
  `retry_after_seconds` stays public as a test seam.
