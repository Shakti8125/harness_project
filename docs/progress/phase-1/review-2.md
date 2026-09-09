# Phase 1 — Wave 3 fix-round re-audit

Run 2026-09-09 by `phase-reviewer` against the **working tree** (`HEAD` = `ea1178f` + uncommitted changes). Gates re-run independently with the project's own toolchain (`.venv`, mypy 2.3.1): **316 passed**, `ruff check src tests app.py` clean, `mypy --strict src/harness` clean. New-test arithmetic confirmed: 69 new tests, 247 + 69 = 316, and 253 − 247 = 6 = 2 deleted modules × 3 `rglob("*.py")` scans. `git status` confirms test-verifier wrote only under `tests/**` and `docs/progress/**`.

## VERDICT — FIX ROUND    FIX FIRST
Findings 1, 2 and 10 are correctly and completely fixed. Finding 3 is substantially fixed but its stated threat model is still reachable through a second path I reproduced. Two new defects landed with the fixes, one of which I would not deploy without closing. The fix list is small and none of it argues for reverting any hunk.

## VERDICT — PHASE 1    FIX FIRST
Unchanged and explicitly restated: **Phase 1 remains FIX FIRST.** Findings 4, 5, 6, 7, 8, 9, 11 and 12 from `review.md` are open and untouched, and this round adds three more. **No `phase-1-green` tag should be cut.** A green fix round is not a green phase, and I do not think the phase verdict should be anything else — finding 5 (no RFC 9457 on validation errors) and finding 11 (the 999 fail-closed default) both become materially more expensive once Phase 2's policy gate and Phase 3's memory store are wired to them.

---

## Findings

**1. [high] `src/harness/llm.py:357` + `src/harness/recovery.py:237-242` + `src/api/main.py:290-294` — honouring the retry delay is now unbounded at the run level, and every timeout that looks like it would bound it is inert.**

Worst case is exactly computable. `Investigator.run` (`investigator.py:550`) hardcodes `status="ok"` regardless of the LLM result, so a rate-limited Investigator does **not** end the run — the Diagnostician then runs and is rate-limited too. Two agents × `transient_max_attempts=4` = 8 provider calls and **6 sleeps**, each up to `MAX_RETRY_AFTER_S = 60.0` → **≈360 s**. Before this round the same request finished in ≤7 s (`U[0,0.5]+U[0,1]+U[0,2]` per agent).

I checked every candidate bound rather than assuming:
- **No `asyncio.wait_for` anywhere in `src/`** — confirmed by grep.
- **uvicorn** has no request-processing timeout (`timeout_keep_alive` is idle-connection only).
- **`app.py:50`'s `timeout=300.0` is a no-op.** `httpx.ASGITransport.handle_async_request` in the installed httpx contains **zero** occurrences of the string `timeout`; it awaits `self.app(scope, receive, send)` directly. The Gradio path is unbounded too.
- **The HF proxy** is the only remaining candidate and is outside the repo's control. `verify.md` records a live 57 s request returning 200, so it tolerates ≥57 s; its upper bound is unverified and I could not measure it without either a slow endpoint (I cannot add one) or live quota.

Failure scenario, and it is the *normal daily end state* given a 20-request/day free tier and a public unauthenticated URL: `POST /v1/replay/real_regression` after quota exhaustion holds a connection for up to ~6 minutes instead of ~4 s. `max_concurrent_runs = 4` and the semaphore is acquired **inside** `_execute`, so four such requests occupy every slot and a fifth queues on the semaphore with no timeout of its own. Four clicks from one bored visitor wedge the public API for minutes. If the HF proxy does cut the connection, the caller gets nothing and never learns the `run_id`, so the completed run in `RunRegistry` is unreachable.

Is the fix net-positive as shipped? The *extraction* is; the *unbounded honouring* is not. My concrete answer to the held redeploy: **do not redeploy without a run-level bound.** Minimum sufficient change, cheapest first — (a) lower `MAX_RETRY_AFTER_S` to 20 s, which caps the run at ~120 s and still honours the 5–20 s delays that are the common and useful case; and (b) add a cumulative per-run delay budget in `retry_structured` (stop honouring provider delays once total slept exceeds it, fall through to exhaustion and the same `rate_limited` escalation). A route-level `asyncio.wait_for` is the blunter alternative but leaves the run with no escalation record. Doing only (a) is defensible for the redeploy; doing only (b) is better long-term.
Owner: harness-core (bound) / api-surface (route-level ceiling, if chosen)

**2. [medium] `src/api/main.py:77-137` — finding 3's threat model is still live through `final.diagnosis.citations[].quote` and `final.bundle.notes.observations[]`, which the fix explicitly and deliberately leaves verbatim.**

The two named fields are genuinely gone (verified below). But both surviving model-authored fields are *verbatim copies of the same log*, on the same public route, and the prompt instructs the model to produce them that way: `prompts/diagnostician.md:16` — "Every citation must quote text that appears **verbatim** in the evidence you were given… Do not paraphrase inside `quote`."

Reproduced offline, no quota: I planted `ghp_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8` beside the anchored assertion line in a private copy of the `real_regression` log — the *same* injection point `tests/integration/test_replay_response_scrubbing.py` uses — and had the stub model behave the way the prompt asks (quote the line it was told to quote). Result:

```
TOKEN IN SERVED RESPONSE: True
citation quote: leaked credential in CI output: ghp_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8
observation   : the log contains: leaked credential in CI output: ghp_...
TOKEN after Redactor.scrub: False
```

The existing test cannot see this because its `StubLlm` returns a fixed citation. Note `Citation.quote` is `max_length=500` and `InvestigationNotes.observations` is `Field(max_length=8)` — a cap on *item count*, not on item length, so an observation is an unbounded string.

The api-surface report records the field-name-walk limitation carefully, but it frames `citations[].quote` as "the harness's actual designed evidence surface… untouched" (`api-surface.md:155-156`) without recording that a citation is a verbatim copy of the very content the fix exists to withhold. So this residual is unfixed *and* unrecorded.

The last line of the reproduction is the answer to your question (d): **the structural mechanism already exists.** `Redactor` (`observability.py:197-223`) is a general recursive JSON scrubber, `SECRET_PATTERNS` (`deps.py:49-55`) already covers `gh[pousr]_`, `github_pat_`, `AIza`, `xox[baprs]-` and bearer tokens, and it is reachable at `get_app_context().recorder.redactor` with no new wiring. One line — `body = redactor.scrub(body)` at the end of `_serialize_run_outcome` — closes this residual, closes the "next raw-content field reopens it" class, and closes the *whole* response body rather than two hand-listed keys.
Owner: api-surface

**3. [medium] `src/integrations/cicd/rendering.py:60-84` — the prompt is now a runtime file read with no startup validation and no exception handling, and the only test that guards shipping covers the Docker image, which is not how the live deployment is built.**

`README.md`'s front matter is `sdk: gradio`, not `sdk: docker`. `tests/unit/test_prompt_templates_ship_with_the_image.py` asserts on `Dockerfile`'s `COPY src ./src` and on `.dockerignore` — both correct, both irrelevant to the Space, which is built from the pushed repo contents plus `requirements.txt`. The `.md` files are **untracked right now** (`??` in `git status`), and `git commit -a` stages the *deletion* of `prompts/*.py` without adding them.

Reproduced by pointing `rendering._PROMPTS_DIR` at an empty directory and driving the real route:
```
healthz: 200 {'status': 'ok', 'db': 'ok', 'version': '0.1.0'}
replay : 500 | content-type: text/plain; charset=utf-8 | body: Internal Server Error
```
`load_prompt_template` is called lazily inside `build_prompt`; `Orchestrator.run` has no `try`/`except` (grep: three `raise`s, zero `except`s), and `main.py` has no exception handler outside the two health routes. So the container starts clean, `/healthz` is green, and the **first** replay 500s in `text/plain` with no RFC 9457 body, no `run_id` and nothing in the trace. Under the pre-round Python-constant design this failure was structurally impossible — a missing prompt was an `ImportError` at startup.

Two cheap closures: load both templates in `main.py`'s lifespan (or at `rendering` import) so a missing template fails loudly at startup rather than silently at first request; and, before the redeploy, `git add src/integrations/cicd/prompts/*.md` and confirm they are in the pushed Space tree (`.gitattributes` has no LFS rule for `*.md` and `.gitignore` does not exclude them, so a plain add is sufficient).
Owner: cicd-integration

**4. [low] `src/integrations/cicd/agents/investigator.py:504-533` — the round removed the last place a model-requested optional tool call's outcome was observable, and its result data was already discarded.**

`executed` is populated at line 521 and never read (grep: lines 504 and 521 only). `result.data` is discarded. Before this round an optional call's failure at least appeared in `bundle.gateway_errors` in the served `RunOutcome`; now `additional_errors` is a local that is only `logger.info`'d, and the refusal branch logs and `continue`s. So the up-to-three "extensibility point" calls now have **zero** observable effect on the outcome — you cannot tell from a `RunOutcome` whether evidence the model asked for was obtained, refused, or 404'd.

This is pre-existing dead-fetch made strictly less observable by this hunk, which is why I am reporting it rather than filing it under the out-of-scope eight. Failure scenario lands in Phase 2: `GitHubToolGateway` replaces `ReplayToolGateway` with the same interface, and each run then makes up to three real GitHub API calls — against B.2's primary rate limit — whose responses are thrown away and whose failures appear only in a log line. Narrow fix: carry `additional_errors` and `executed` onto the bundle as their own fields (not `gateway_errors`), so the confidence row stays correctly scoped while the calls stay visible.
Owner: cicd-integration

**5. [low] `src/integrations/cicd/prompts/diagnostician.md:64` — the port to `*.md` changed the rendered prompt, and the changed prompt is declared `version: 1`.**

I rendered the deleted `prompts/*.py` modules and the new templates over identical inputs and diffed. The Investigator differs by one blank line (2040 → 2039 chars). The Diagnostician differs by two blank lines **and one substantive character pair**: the old f-string rendered `` each `{"claim_kind", "locator", "quote", "note"}` `` (backticked, from the doubled braces); the `.md` renders it unbackticked as `each {"claim_kind", ...}`.

Failure scenario: `version: 1` now names two different prompts — the one that produced the results recorded in `verify.md` from the live Space, and the one in the tree. Finding 10's entire purpose was to give Phase 4 a key to group eval runs by; a version that is not stable across the change that introduced it undermines that on day one. Either restore the backticks (making version 1 byte-faithful to what the live Space last ran) or bump the Diagnostician to `version: 2`. Whitespace-only differences are not worth acting on.
Owner: cicd-integration

**Not findings, recorded so they are not re-discovered:** three of the new integration tests pass `db_path=Path("unused.db")` while driving the full orchestrator, which *does* bind a run id via `run_scope` — so `_persist` runs, creates a zero-byte `unused.db` in the repo root (gitignored) and emits ~7 `sqlite3.OperationalError: no such table: spans` tracebacks per test into the run log. Harmless; noted only because that noise could mask a real trace-persistence failure later. Also: `mypy --strict src/harness` reports one `arg-type` error at `orchestrator.py:312` under mypy **1.14.1** and none under the project's pinned **2.3.1** — a tool-version artifact of `Mapping.get` narrowing, not a code defect, and CI uses the lockfile.

---

## Verification of the four in-scope findings

**Finding 1 — fixed, both directions.** `bundle.gateway_errors` is now fed only from `collected.gateway_errors`, which `build_prompt` populates from the four required calls (`investigator.py:313, 347, 360, 388`). Optional calls accumulate into a separate `additional_errors` list; the write-tool refusal no longer synthesises a `ToolError` at all. `Diagnostician.signals` is unchanged, so `Adjustment.reason` — "a required read tool returned an error (…)" — is now true whenever it appears. The 0.95 → 0.85 regression is gone and the negative case is genuinely covered: `test_a_genuinely_failed_required_read_still_fires_gateway_degraded` deletes the job-log fixture from a `tmp_path` copy and asserts `-0.10`, the exact reason string, and `0.85`. The optional-404 test asserts its own premise (the fixture file really is absent), so it cannot pass vacuously.

**Finding 2 — fixed; the shape claim is accurate and the limits are stated honestly.** I verified the SDK shape independently rather than taking the report's word: `google.genai.errors.APIError.__init__` sets `self.details = response_json` and `self.response = <httpx.Response>`, and `raise_for_response` passes the **full envelope** `{"error": {...}}` for the httpx/requests transports and the **inner error object** for the replay transport — so the report's claim that both shapes occur, and that a key-name walk rather than a positional or `@type`-based lookup is the right response, is correct. The delay is in the **body** as `google.rpc.RetryInfo.retryDelay`, a protobuf duration string, not an HTTP header, exactly as described. `recovery.py:237-241` is now reachable and `test_a_429_with_a_stated_retry_delay_is_slept_verbatim_every_time` pins `slept == [41.0, 41.0, 41.0]` with a control case pinning the jittered fallback. `LlmUpstreamError` carries the delay too, and the `if delay is None or isinstance(exc, LlmTimeout)` branch keeps timeouts on backoff.

The stated limits are honest and, with one exception, right. Not honouring `retry-after-ms` is correct: sub-second values would reproduce the defect. Not traversing proto/proto-plus objects is correct: proto3 JSON serialises `Duration` as `"41s"`, so the wire form is always the string. The one unstated gap: a `retryDelay` expressed as `{"seconds": 41, "nanos": 0}` is silently missed — `_duration_to_seconds` rejects the dict, and the recursive descent then looks for `retryDelay` *inside* it and finds nothing. That form does not appear in JSON-over-HTTP, so it is not a defect today; it is worth one line in the docstring alongside the other limits.

**Finding 3 — the two named fields are gone; see finding 2 above for what is not.** Verified end to end offline through the real orchestrator: `"excerpt" in served == False`, `"patch" in served == False`, log entry keys are `['job_id','total_lines','included_lines','anchor_line_numbers','truncation','excerpt_length','excerpt_sha256']`, file entry keys are `['path','status','additions','deletions','patch_length','patch_sha256']`, digests are 64 hex. `patch=None` stays `None` with no `patch_length`/`patch_sha256` siblings (pinned by two tests, one unit and one end-to-end). Both `POST /v1/replay/{scenario}` and `GET /v1/runs/{run_id}` route through `_serialize_run_outcome`; `POST /v1/runs` returns only a 202 envelope and `GET /v1/runs` carries no `final`. `app.py`'s "Raw RunOutcome" tab renders `_call_api`'s result, which goes through the real HTTP route, so it now shows the scrubbed body. `_serialize_run_outcome` mutates only the fresh dict from `model_dump`, never the frozen `RunOutcome`. Nothing else *typed* on that path carries raw content — `LogExcerpt.excerpt` and `FileChange.patch` are the only two raw fields in the CI schemas; the residual is the model-authored copies.

**Finding 10 — fixed.** Both templates are real `.md` files with `version: 1` front matter and a `---` separator, parsed by `load_prompt_template` with explicit `ValueError`s for a missing line or separator, resolved `Path(__file__)`-relative (tested from a relocated cwd with the cache cleared). `prompts/__init__.py`'s docstring is now accurate — the false "(see prompts/*.md)" is replaced by a description of what is actually there. `Dockerfile`'s `COPY src ./src` and `.dockerignore`'s single `README.md` pattern do ship them in the *image* (Docker matches `README.md` against the whole relative path, so `src/**/README.md` would be unaffected anyway). See finding 3 above for the Space.

**`llm_upstream` — three-way agreement confirmed, plus a fourth.** `contracts.py:99-101`, `orchestrator.py:55-59`, `_OUTCOME_FOR_ERROR_KIND` at `orchestrator.py:79`, and `PLAN.md:1017-1019` all carry the member. Your measurement of the PLAN edit is correct: `git diff --numstat` is `2 2 PLAN.md`, the hunk is entirely inside the `Literal`, and B.1's `escalate llm_upstream` row at **PLAN.md:1576** is untouched. The only enumerations of `EscalationRecord.reason` outside `src/harness/**` are `wiring.py:119` (`escalate_as="unknown_category"`, still valid) and PLAN.md:1020 — nothing is broken by the widening. `test_escalation_reason_drift_guard.py` reads both `Literal`s back out of the type system and asserts set equality; note it does **not** guard PLAN.md's copy, which remains a manual third source.

One consequence worth recording: because `classify_provider_error` maps 5xx to `LlmUpstreamError`, an exhausted 503/504 now escalates `llm_upstream` rather than `tool_failure`. B.1's 503/504 row says "same as 429", i.e. `rate_limited`, so it matches neither — this is the PLAN inconsistency already recorded in `review.md`'s failure-path table, moved but not worsened.

---

## The five items raised for judgement

**(a) The retry delay and wall clock.** See finding 1. Net-positive in principle, not shippable as-is on a public unauthenticated endpoint. No client-side or server-side timeout bounds it: uvicorn has none, there is no `asyncio.wait_for`, and `app.py:50`'s `timeout=300.0` is provably inert. The HF proxy tolerates ≥57 s; its ceiling is unverified and is not yours to rely on. My recommendation: lower `MAX_RETRY_AFTER_S` to 20 s **and** add a cumulative per-run delay budget. Lowering the cap alone is enough to unblock the redeploy.

**(b) The `prompt.render` span.** Acceptable to ship; do not rework now. I verified it behaves correctly: it parents under `agent.run` (the ambient `_current_span_id` is set for the whole `async with` body), `"agent"` is a valid `Component`, and the trace read path is unaffected — `test_trace_is_persisted_and_readable`'s token totals still come out at exactly 3000 because they aggregate `llm` spans only. The count is 7, confirmed by measurement (below). The pattern's real cost is that the span name describes work that happens *outside* it — the prompt is rendered on the line before — so its `duration_ms` is meaningless. That is a docstring's worth of harm. cicd-integration is right that `AgentPrompt.prompt_version: str | None` is harness-core's call; queue it behind the Phase 2 harness work rather than opening `src/harness/agent.py` for it now.

**(c) The dormant prompt clauses.** Agree with cicd-integration: land them with Phase 3, worded around a real prior. Phase 1 has no memory store, both `prior_history_summary` strings are stand-ins built in the agent rather than in the template, and adding a clause about "if this run's evidence contradicts the prior" to a prompt that can never receive a prior is speculative text that will be rewritten anyway. It does interact with my finding 11 — but finding 11 is about `retries_in_24h` defaulting to `0` instead of `999`, which is a *code* fail-open the prompt cannot compensate for; fixing the prompt would not close it, and fixing finding 11 does not require the prompt. They should land together in Phase 3, not separately now.

One thing I would move earlier, because it is a one-string change and not new prompt substance: the Investigator's stand-in currently says **"Treat this failure as a first sighting"** (`investigator.py:456`), which instructs the model to do exactly what the Diagnostician's stand-in forbids two agents later ("Do not treat the absence of history as evidence of either flakiness or novelty"). Aligning the Investigator's string to the Diagnostician's costs nothing and removes a contradiction between two prompts in the same run.

**(d) The scrub is a field-name walk.** Not acceptable to close the phase on, but the remedy is much cheaper than a schema marker. As finding 2 shows, the structural mechanism already exists and is already wired: `Redactor.scrub` over the whole serialised body, one line in `_serialize_run_outcome`, using `recorder.redactor`. That closes the residual, closes the "next raw-content field reopens it" class, and is testable today (plant a token, assert absence from `response.text` — the fixture is already written). Keep the field-name walk as well: it is doing a different job (bulk removal, not credential removal) and the digest capability is worth keeping. A schema-level marker in `src/integrations/cicd/schemas.py` is the right long-term shape but belongs to whoever adds the next artifact field, not to this round. Whose tree, which phase: **api-surface, now** — it is one line in a file this round already owns, and it removes the reason the limitation note exists.

**(e) The A.12 deviation.** Yes, PLAN.md needs the amendment; without it a future reviewer diffing served JSON against `RunOutcome.model_dump()` finds an undocumented divergence, and the natural conclusion is that the API is broken. A.12's error paragraph at **PLAN.md:1555-1556** is already the place where response-body transformations are stated, so the exception belongs immediately after it, as a second paragraph:

> `200 RunOutcome` responses are the model dump with one documented exception: raw external content carried in `final` is replaced by its length and sha256 digest at the HTTP boundary — today `final.<artifact>.logs[].excerpt` → `excerpt_length` + `excerpt_sha256` and `final.<artifact>.diff.files[].patch` → `patch_length` + `patch_sha256`. `final` is opaque to the harness, so this substitution lives in the API layer and must be extended by hand when an integration adds a raw-content field. The whole body also passes through the `Redactor`.

That last sentence only becomes true once (d) is done; if you close (d) in the same commit, write it as above. If you do not, drop it and note the residual instead.

**The four harness-core judgement calls.** Header-before-body precedence: correct as chosen, and B.1 names `Retry-After` explicitly, so the header is the specified instruction and the body is the inference. It is also the safer direction on this codebase, since the header path is the one that can be short. Silent clamp: add the `logger.info`. `classify_provider_error` being a pure function is not a reason — it already calls `logger.debug` two frames down in `retry_after_seconds`, so the precedent is set, and "we ignored the provider's stated hour" is precisely the line you want in a postmortem for finding 1. `retry_after_seconds` public as a test seam: fine, and the tests do use it directly; it is provider-coupled, not domain-coupled, and lives in the module that already owns `to_gemini_schema`, so it creates no layering debt. Duplicated `EscalationReason` lists: agree with the reasoning — deriving the alias at runtime launders the `Literal` through `Any` and loses the mypy check on `_OUTCOME_FOR_ERROR_KIND`, which is the check that matters. The drift guard is the right trade. It does leave PLAN.md as an unguarded third copy; that is acceptable while a human diffs Appendix A each phase.

**cicd-integration's "no confidence signal for a refused write tool".** Correct, and correctly reasoned. PLAN's adjustment table has no row for "the model asked for something it wasn't allowed to have", and inventing one would put a number on the confidence scale that no `calibrate()` row justifies — the exact failure mode finding 4 is about from the other direction. Refusing, logging, and moving on is right. The visibility gap it creates is finding 4 above, and the fix there is a bundle field, not an adjustment.

---

## Doc corrections — your numbers checked

**The 429 claim is wrong as written and your correction is right.** `docs/progress/phase-1/verify.md:258-259` reads "Incidentally this verified Appendix B.1's 429 row for free: the run made 4 attempts with exponential backoff and then escalated as `rate_limited`, exactly as specified." The run verified the attempt count, the backoff, and the escalation reason. It could not have verified honouring, because at that commit `retry_after_s` was hardcoded `None`. Suggested replacement: "…verified three of the four things Appendix B.1's 429 row specifies: four attempts, exponential backoff, and escalation as `rate_limited`. It did **not** verify the fourth — 'honour `Retry-After`' was not implemented at this commit (Wave-3 finding 2) and landed in the follow-up round."

**7 is right; 5 appears in two files.** Measured directly against the real orchestrator over the real fixture with a stub LLM: `SPAN COUNT: 7` — `run` (orchestrator) + `agent.run`×2 + `llm.attempt`×2 + `prompt.render`×2, with the new spans carrying `{"agent": "investigator"|"diagnostician", "prompt_version": "1"}`. This matches test-verifier's independent measurement. Stale places:
- **`docs/deploy-huggingface.md:214`** — `# 5   (0 would mean the explicit recorder.initialize() was lost)`. This is the actively misleading one: the surrounding text calls it "the one assertion that catches a mounted sub-app silently losing its lifespan", so anyone following the instruction after redeploy sees 7, reads it as a failure, and starts debugging a lifespan that is fine. Correct to `7`, and consider making the parenthetical carry the real invariant — the assertion that matters is `> 0`, not `== 5`.
- **`docs/progress/phase-1/verify.md:172`** (recorded output `5`) and **`:175`** ("Five spans means the explicit `recorder.initialize()` ran…"). This is a historical record of a real run, so the honest edit is to leave the `5` and annotate that the count became 7 after the fix round added `prompt.render`.

Nothing under `tests/**` pins a span count, so no test needs changing — confirmed by grep; the only count assertion is `len(agent_spans) == 2`, which is still correct.

**Verify step 4.** The substitution is sound and does not need a live re-run. Step 4 exercises the escalation-threshold comparison in the remediation gate, which no hunk in this round touches — `_serialize_run_outcome` runs after the outcome is built, the `gateway_errors` split changes which adjustments fire but not how the threshold is compared, and the retry-delay work is below the agent layer. `test_low_confidence_escalates` asserts the same behaviour offline and still passes. The only thing a live re-run would add is confirmation that `HARNESS_ESCALATION_THRESHOLD` is plumbed through the *container's* environment, and that plumbing is itself unchanged. Spending 2 of 20 daily requests on it is not justified. The `git stash` substitution is also sound: a control case that fails if the fix is reverted is a stronger non-vacuity argument than a before/after diff, and `test_gateway_error_scoping.py`'s required-read case and `test_recovery_retry_delay.py`'s `retry_after_s=None` case both have that property. I additionally re-derived the pre-fix behaviour from `git show HEAD:` for both, and it matches what the reports claim.

**No live Gemini call was made during this re-audit,** and none is required to reach these verdicts.

---

## Contract diffs

| Model / surface | Field | Plan says | Code has |
|---|---|---|---|
| `EscalationRecord` | `reason` | `Literal[…, "llm_upstream", …]` after the authorised edit | identical in `contracts.py`, `orchestrator.py`'s alias and `_OUTCOME_FOR_ERROR_KIND`. **Agrees — no diff.** |
| A.12 `200 RunOutcome` | `final.<artifact>.logs[].excerpt`, `…diff.files[].patch` | `RunOutcome`; `final` is "integration payload; opaque to the harness", no scrubbing stated | replaced by `_length` + `_sha256` at the HTTP boundary. Deliberate, ruled by the user; **PLAN.md amendment owed — see (e)** |
| A.12 error bodies | unhandled route exception | RFC 9457 `application/problem+json` | a missing prompt template yields `500 text/plain "Internal Server Error"` — finding 3, and a new instance of the open finding 5 |

Everything else re-checked in the changed files matches. `Adjustment` (A.11) and Appendix E vs `src/settings.py` are unchanged from the Wave-3 pass and still agree field for field. No new `extra="forbid"` / `frozen` deviations; `PromptTemplate` is a frozen dataclass, not a contract model, and correctly so.

## Unhandled failure paths

| External call | Missing condition |
|---|---|
| Gemini 429 / 503 / 504 | **Now handled** — header first, decoded body second, clamped, hostile input rejected. Undocumented residual: a `retryDelay` in the `{"seconds":…,"nanos":…}` object form is missed (not a wire form; docstring-only fix) |
| Gemini 429 with a long delay | **New** — honoured without a run-level ceiling; up to ~360 s per request — finding 1 |
| `prompts/*.md` read | **New** — `FileNotFoundError` / `ValueError` from `load_prompt_template` is unhandled by the orchestrator and the route; no startup validation — finding 3 |
| Model-requested optional tool call | error is logged and discarded; result data was already discarded — finding 4 |
| Gemini, no candidates returned | still not terminal — `review.md` finding 9, open, unchanged |
| SQLite | `retries_for_signature_24h = 999` fail-closed default still absent — `review.md` finding 11, open, unchanged |
| GitHub API, escalation webhook | out of scope for Phase 1, unchanged |

## Plan drift

**Built but not planned (new this round)** — nothing. Every hunk maps to a dispatched finding or to the authorised `llm_upstream` change. `MAX_RETRY_AFTER_S`, `_MAX_PAYLOAD_DEPTH`, `PromptTemplate` and `_digest_str_field` are implementation detail below the contract line, all documented in place.

**Planned but missing (closed this round)** — "prompts as versioned `*.md` files" (repo layout line 118, PLAN.md:202-206) is now satisfied.

**Planned but missing (still open, unchanged)** — the Diagnostician's larger thinking budget (PLAN.md:158, not expressible as config), `gateway_replay.py`'s fault injection (`HARNESS_FAULT_INJECT` read by nothing), Fly.io deploy (replaced and documented), `scripts/replay.py` / `scripts/eval.py` (not this phase).

**PLAN.md owed** — the A.12 amendment in (e). One paragraph, one location, no other section affected.
