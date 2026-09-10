# Phase 1 — Wave 3 audit

> **Superseded — the current verdict for Phase 1 is SHIP, at the bottom of this file.**
> The `FIX FIRST` below was correct on 2026-09-08 and every finding it raised is now
> closed; `docs/progress/phase-1/backlog.md` records each closure. The original text is
> left unedited on purpose. Do not act on the verdict below without reading the
> superseding one first.

Run 2026-09-08 by `phase-reviewer` against `a870925`, after Wave 2 returned PASS
(253 passed, `ruff` clean, `mypy --strict src/harness` clean) and all five Verify steps
passed. Returned verbatim; the reviewer is read-only and has no Write tool.

## VERDICT              FIX FIRST

Findings 1–3 are live wrong behaviour or a live exposure on a public URL. The rest are contract and barrier gaps that are cheap now and expensive in Phase 2/4/5.

The two items checked explicitly both **pass**:

- **Appendix A.11 `Adjustment`.** Carried out correctly and minimally. `PLAN.md:1486-1490` is now the four-line comment plus `from src.harness.confidence import Adjustment   # name: str; delta: float; reason: str`; `git show c064e70 -- PLAN.md` is a 2-line-for-5-line replacement and touches nothing else in A.11 (`Citation` above and `Diagnosis` below are byte-identical). `src/integrations/cicd/schemas.py:21` imports it and re-exports it in `__all__`; `src/harness/confidence.py:70` declares it once with `name: str; delta: float; reason: str` and `extra="forbid", frozen=True`. Three sites agree; one class, one type.
- **Appendix E vs `src/settings.py`.** Field for field identical — all 4 secrets and all 19 non-secret fields, same names, same types, same defaults, same `Field(0.70, ge=0.0, le=1.0)`, same `model_config`. `gemini_model = "gemini-3.6-flash"` on both sides; `.env.example` agrees; PLAN.md:157 and :1742 agree; PLAN.md:159's promotion example is `gemini-pro-latest`. `grep -n "gemini-" PLAN.md` shows no surviving `2.5` reference anywhere in the repo outside `.venv`.

## Findings

**1. [medium] `src/integrations/cicd/agents/diagnostician.py:161-163` — the `gateway_degraded` −0.10 penalty fires on *any* entry in `bundle.gateway_errors`, including errors from optional model-requested tools and from refusals the Investigator manufactures itself.**
PLAN.md's adjustment table conditions this row on "any **required** read tool returned an error". `Investigator.run` (`investigator.py:473`, `485-491`, `493`, `506`) folds two non-required classes into the same list: a `forbidden_by_policy` `ToolError` it synthesises when the model names a write tool, and any error from the up-to-three optional `additional_tool_calls`.
Failure scenario: on `real_regression` the model asks for `get_file_contents(path="src/pricing.py", ref=<head_sha>)` — a natural move, and the prompt offers the catalog. The scenario directory has only three recorded `api/` files, so the replay gateway returns `not_found`, which is correct. The Diagnostician then applies −0.10. Reproduced: `final_confidence` 0.95 → **0.85**, with `Adjustment.reason` reading `"a required read tool returned an error (not_found)"` — a statement that is false; no required read failed. Confidence is the number the escalation gate and Phase 2's `0.75`/`0.85` policy thresholds read, so one optional 404 moves a run across `open-fix-pr`'s bar. A refused `merge_pull_request` request costs the same 0.10.
Owner: cicd-integration

**2. [medium] `src/harness/llm.py:353` — Appendix B.1's "honour `Retry-After`" on a 429 is not implemented; the plumbing that would use it is dead code.**
`classify_provider_error` constructs `LlmRateLimited("provider rate limited the request", retry_after_s=None)` unconditionally and never inspects the provider exception for a delay. `recovery.py:237-241` reads `exc.retry_after_s` and prefers it over backoff — that branch can never be taken.
Failure scenario: the free tier returns 429 `RESOURCE_EXHAUSTED` with a `retryDelay` of tens of seconds. The harness instead draws full jitter from `[0,0.5]`, `[0,1]`, `[0,2]`, completes all four attempts in under ~4 seconds, gets 429 four times, and escalates `rate_limited` — having spent 4 of a 20-request **daily** quota to learn nothing. `verify.md` finding 4 records exactly this run and reads it as "verified Appendix B.1's 429 row for free"; the half of that row that was verified is the attempt count, not the honouring.
Owner: harness-core

**3. [medium] `src/harness/orchestrator.py:336-339` → `src/api/main.py:284` — the run response body carries the raw job log verbatim, unredacted, on a publicly reachable URL, and the leak test PLAN.md specifies would not see it.**
`RunOutcome.final` is `{key: artifact.model_dump(mode="json")}`, so `final.bundle.logs[].excerpt` is the whole budgeted log. Measured on the current fixture: **40,638 characters** of raw log in the JSON response of `POST /v1/replay/real_regression`, which is live at `https://shakti-agent-harness.hf.space`. `app.py:113` also renders it into the Space's "Raw RunOutcome" tab. The `Redactor` covers span attributes only; nothing scrubs this path.
Failure scenario: PLAN.md:862-866 plans a Phase 5 fixture "whose log fixture itself contains a pasted `ghp_…` token, simulating a careless workflow". The moment that fixture lands, an unauthenticated `POST /v1/replay/<that scenario>` returns the token verbatim over the internet — and `test_no_secret_leak.py` as PLAN.md specifies it asserts only over `trace_span`, `escalation`, captured stdout/stderr and the raw bytes of `harness.db`. The gate stays green while the token is served. No fixture contains a token today (`grep -rE "gh[pousr]_…|AIza…|xox…" fixtures/` is empty), so this is exposure-in-waiting, not a live leak.
Owner: api-surface (response path) / fixtures-eval (leak-test scope, Phase 5)

**4. [medium] `src/integrations/cicd/schemas.py:157-158` via `src/harness/llm.py:269-286` — the `Diagnosis` response schema asks the model for `final_confidence` and `confidence_adjustments`, the two fields Appendix A.11 marks "added by the harness after the model returns, **not requested from the model**".**
Verified: `to_gemini_schema(Diagnosis)["propertyOrdering"]` is `[... 'suggested_action', 'final_confidence', 'confidence_adjustments']`, and `confidence_adjustments` is emitted as a full `ARRAY` of `OBJECT{name,delta,reason}` with all three `required`. Under constrained decoding the model will fill them.
Failure scenario: the model returns `final_confidence: 0.90` and a fabricated `Adjustment` list. Today `Diagnostician.run` (`diagnostician.py:196-201`) overwrites both via `model_copy`, so the served number is still the harness's — the current output is correct. The wrong behaviour is one consumer away: any code that reads a `Diagnosis` straight off `retry_structured` rather than off `Diagnostician.run` (Phase 4's Recovery re-validation, or an Evaluator handed a raw attempt) gets a self-graded confidence and invented adjustment rows that no `calibrate()` call produced. It also exposes the calibration mechanism to the model, which PLAN.md's "the model never sees the adjusted number" exists to prevent, and pays output tokens for two discarded fields on every call.
Owner: cicd-integration (contract) / harness-core (`to_gemini_schema` has no way to exclude a field)

**5. [medium] `src/api/main.py:53` — no `RequestValidationError` handler; malformed requests get FastAPI's default 422 `application/json` with the entire request body echoed back, not the RFC 9457 body A.12 requires.**
Reproduced: `POST /v1/runs` with `{"integration":"cicd","not_a_field":"x"}` returns `422`, `content-type: application/json`, body `{"detail":[{"type":"missing","loc":["body","subject"],...,"input":{"integration":"cicd","not_a_field":"x"}}, ...]}`. A.12: "Errors use RFC 9457 `application/problem+json`: `{type, title, status, detail, instance, run_id?}`". The hand-written `problem()` helper is used on every route's own error paths but never on validation, which is the most common error a caller will actually see. `RunRequest.subject` is `dict[str, JsonValue]` — arbitrary caller-supplied JSON — reflected verbatim with no redaction.
Owner: api-surface

**6. [medium] `src/api/main.py:62-85` — `problem()`'s `detail` does not pass through the `Redactor`, which A.12 requires ("`detail` passes through the `Redactor`").**
Today every call site authors a constant string, so nothing leaks; the docstring makes that convention explicit. But the barrier PLAN.md names does not exist — only the convention does, and the convention has no test.
Failure scenario: Phase 2's `POST /v1/approvals/{id}` or Phase 5's `/webhooks/github` interpolates an upstream message or a caught exception into `detail` — the natural thing to write — and it reaches the client unscrubbed, with `test_no_secret_leak.py` (which does not inspect HTTP responses) still green.
Owner: api-surface

**7. [low] `src/harness/orchestrator.py:314-315` + `src/integrations/cicd/agents/investigator.py:475-476` — `GET /v1/runs/{id}` and `GET /v1/runs/{id}/trace` report different `degraded_components` for the same run.**
`LLMAgent.run` publishes `prompt.degraded` under `ATTR_DEGRADED_COMPONENT`, which `read_trace` (`observability.py:457-462`) aggregates. `Investigator.run` appends `"investigator_notes"` to `state.degraded` *after* `super().run()` has closed the `agent.run` span, and the orchestrator writes the run-level list under the attribute name `"degraded"`, which `read_trace` does not read.
Reproduced with a stub LLM that raises `LlmRateLimited`: `RunOutcome.degraded_components == ["investigator_notes"]`, `TraceResponse.degraded_components == []`.
Owner: harness-core / cicd-integration

**8. [low] `src/harness/llm.py:153-168` — `_ALLOWED_SCHEMA_KEYS` keeps `minItems`/`maxItems` but drops `maxLength`/`minLength`, so every `Field(max_length=…)` string constraint in Appendix A.11 is invisible to the model while still enforced by Pydantic.**
PLAN.md's step 3 says to strip "`title`, `default`, `additionalProperties`, `$schema`, `format`". `maxLength` is not on that list, and Gemini's schema dialect accepts it.
Failure scenario: a diagnosis whose natural `reasoning` runs to 1,400 characters generates unconstrained, fails `Diagnosis.model_validate_json` with "String should have at most 1200 characters", and burns a repair attempt — two model calls where one would do, against a 20-request/day quota. Three such in a row escalates `invalid_output` on a diagnosis that was substantively fine. Same for `Citation.quote` (500) and `note` (200), the fields most likely to run long.
Owner: harness-core

**9. [low] `src/harness/recovery.py:68,249` — Appendix B.1's "empty candidates" half of the safety row is not terminal.**
`_TERMINAL_FINISH_REASONS` covers `SAFETY`/`RECITATION`/`BLOCKLIST`. A response with no candidates arrives from `GeminiClient.generate` (`llm.py:429-433`) as `finish_reason="UNKNOWN"`, `text=""`, falls into the validation branch, and is retried the full 3 attempts. B.1 requires "no retry (deterministic)" and `detail.finish_reason` recorded; the terminal `AgentError.detail` instead carries `{"last_error": …}`.
Owner: harness-core

**10. [low] `src/integrations/cicd/prompts/` — prompts are Python string constants, which PLAN.md:202-206 explicitly decides against, and no prompt carries a `version:`.**
Repo layout line 118: `prompts/*.md ← versioned, hash logged into the trace`. The decision's stated rejection of the alternative is verbatim: "prompts as Python string constants — invisible in diffs and impossible to attribute an eval regression to". `prompts/__init__.py`'s docstring still reads "(see prompts/\*.md)", which is now false.
Failure scenario: `prompt_sha256` (`agent.py:215`) hashes the *fully rendered* prompt — template plus the evidence — so it changes with every input. Phase 4's "compare eval runs across prompt edits" has no version key to group by; you cannot tell a hash change caused by an edit from one caused by a different fixture. The Diagnostician's action-guidance edit recorded in `verify.md` finding 3 is already unattributable this way.
Owner: cicd-integration

**11. [low] `src/integrations/cicd/agents/investigator.py:439` — the memory-unavailable path does not fail closed on the retry cap.**
`PriorHistory(signature_id=None, unavailable=True)` leaves `retries_in_24h` at its `0` default. Appendix B.3 requires the degraded path to default `retries_for_signature_24h` to a conservative **999** "so the flaky-retry rule fails closed". Nothing reads the field in Phase 1, so there is no live defect.
Failure scenario, and the reason it is worth writing down now: Phase 2 wires `memory.retries_for_signature_24h` from this field to satisfy `retry-suspected-flaky`'s `{lt: 2}` condition. With memory permanently unavailable, `0 < 2` holds on every run and the retry cap never bites — the exact brake Open Risk 7 lists first.
Owner: cicd-integration (carry-forward)

**12. [low] `src/integrations/cicd/gateway_replay.py:87` — `forbidden: tuple[str, ...] = ()` makes the gateway's authoritative safety re-check opt-in at construction.**
`src/harness/gateway.py:74-119` calls this check "the authoritative one … the last code that runs before an external system is touched", and step 1 says the set "is held by the implementation itself". A default of empty means a caller who forgets `forbidden=` gets a gateway that refuses nothing. `deps.py:98` passes `load_forbidden()` correctly and the unit test passes an explicit tuple, so nothing is wrong today.
Failure scenario: a gateway built without `forbidden=` receives a hand-forged `PolicyDecision(effect="allow")` for `merge_pull_request`. In Phase 1 it still executes nothing — `call.tool not in READ_TOOLS` catches it — but returns `ToolError(kind="invalid_args")` instead of `forbidden_by_policy` and logs no refusal, so Phase 2's mandated `test_gateway_refuses_forbidden_even_with_forged_allow_decision` would pass or fail on how its fixture happens to construct the gateway. When `GitHubToolGateway` lands with the same signature, the default becomes a live fail-open.
Owner: cicd-integration

## Contract diffs

| Model / surface | Field | Plan says | Code has |
|---|---|---|---|
| `Diagnosis` (response schema) | `final_confidence`, `confidence_adjustments` | A.11: "added by the harness after the model returns, **not requested from the model**" | both emitted in `properties` and `propertyOrdering` by `to_gemini_schema` — finding 4 |
| A.12 `GET /v1/runs` | query params | `?status&integration&limit&cursor`; `{items, next_cursor}` | `status` and `limit` only; `integration` and `cursor` silently ignored (`?integration=other` returns every run); `next_cursor` always `null` |
| A.12 error bodies | all errors | RFC 9457 `application/problem+json`; `detail` through the `Redactor` | validation errors are FastAPI's default 422 `application/json` echoing the request body; `problem()` bypasses the `Redactor` — findings 5, 6 |
| A.9 `TraceRecorder.span` | decorator form | `@asynccontextmanager def span(...)` | `async def` — forced by `asynccontextmanager`; recorded in `build.md` §Deviations 1, no caller affected. **Accepted, not a defect.** |
| A.8 `retry_structured` | `MAX_TOKENS` → `max_output_tokens × 1.5`; 400 → re-assemble at `budget × 0.5` | two knobs the frozen signature cannot reach | brevity instruction and prompt halving; recorded in `build.md` §Deviations 2. **Accepted.** |
| `Evidence.locator` | comment | `"log:job/2001#L512-531" \| "diff:requirements.txt@+12"` | comment generalised to "opaque pointer into `source`" — deliberate de-domaining of the harness layer. **Accepted.** |

Everything else in A.1, A.2, A.3, A.4, A.7, A.8, A.9, A.10 and A.11 matches field for field: names, types, `Literal` members, defaults, `Field` constraints, `extra="forbid"`, `frozen=True`. `RunState` is correctly the one `extra="allow", frozen=False` model. Appendix E matches `src/settings.py` exactly.

## Unhandled failure paths

| External call | Missing condition |
|---|---|
| Gemini 429 | `Retry-After` / `retryDelay` never extracted; `retry_after_s` is hardcoded `None` — finding 2 |
| Gemini, no candidates returned | not terminal; retried 3× and `detail.finish_reason` not recorded — finding 9 |
| Gemini 503/504 exhausted | escalates `tool_failure`; B.1 says "same as 429", i.e. `rate_limited`. (B.1's "escalate `llm_upstream`" for `ConnectError` is not expressible — `llm_upstream` is not a member of `EscalationRecord.reason`. The code's choice of `tool_failure` is the only valid one; this is a PLAN.md inconsistency, not a code defect.) |
| Gemini `ConnectError` | budget is `transient_max_attempts` = 4; B.1 specifies 3 for network-unreachable. Over-generous, not dangerous. |
| Replay gateway, missing `api/` file on a *baseline* read | returns `not_found` (correct per B.2) but the Investigator marks `baseline` degraded **and** sets `cold_start=True`, so the run takes both the −0.10 `gateway_degraded` and the −0.05 `cold_start` penalty for one condition. Replay-only; the live gateway returns an empty list, not a 404. |
| SQLite | B.3's `retries_for_signature_24h = 999` fail-closed default is absent; `PriorHistory.retries_in_24h` defaults to `0` alongside `unavailable=True` — finding 11. B.3's busy/lock retries, corruption rename and WAL are Phase 3 (`memory.py` is a stub), correctly out of scope. |
| GitHub API | entirely out of scope — `gateway_github.py` is a 4-line stub, per the phase plan. |
| Escalation webhook (B.4) | not implemented; `_escalate` logs only and `escalation_channels` defaults to `("log",)`. Phase 1 does not require it, and `EscalationRecord.channels` records honestly what was used. **Correct for this phase.** |

Correctly handled and worth recording as verified: the 401/403 auth path including the 400 `API_KEY_INVALID` variant (fixed message, no retry, key value never in it), the timeout path (2 attempts → `llm_timeout` → escalated), the 429 attempt budget and escalation reason, the 400-too-large downshift with its `context_downshift` span attribute, non-JSON and schema-invalid recovery, and B.2's "404 on a read tool is data, not a run failure".

## Layer separation

Nothing survives verification here. I checked every harness signature against the "could an incident-triage adapter supply this?" question and found no semantic leak beyond what `test_layering.py` already covers: anchors, priorities and the whole regex list are parameters (`ContextRequest.anchor_patterns`, populated from `investigator.ANCHOR_PATTERNS`); stage names, artifact keys and the gate are injected (`Orchestrator(artifact_keys=…, stages=…)`); the seventh adjustment row is correctly left in `wiring.build_confidence_model()` with a loud `UNREGISTERED_SIGNAL_REASON` fallback if it goes missing; the credential regexes live in `deps.py`, not `observability.py`; `read_only_decision` and `load_forbidden` are in the integration. `LLMAgent`'s default `schema_translator=to_gemini_schema` and `recovery._TERMINAL_FINISH_REASONS` are *provider*-coupled, not *domain*-coupled, and both are injectable/PLAN-mandated. Phase 6's `git diff --stat -- src/harness/` should come back clean on layering grounds.

## Guardrails invariants

Both hold, to the extent Phase 1 can exercise them. `MAX_SIDE_EFFECTING_ACTIONS_PER_RUN: Final[int] = 1` is a module constant in `guardrails.py` with no `Settings` field and no policy key that could reach it. The gateway re-checks `call.tool in self.forbidden` as the first statement of `invoke`, before any file is opened, and returns `forbidden_by_policy` rather than raising — it does not consult `decision.effect`, and the docstring contract is unusually explicit about why. The only caveat is the fail-open constructor default in finding 12. `policy.yaml`'s `forbidden` list and `load_forbidden()`'s single-key read keep the gateway's set and the policy file from drifting, which is the right shape for Phase 2 to absorb.

## Plan drift

**Built but not planned**

- `app.py`, `requirements.txt`, `docs/deploy-huggingface.md`, README Spaces front matter — PLAN.md targets Fly.io (`fly deploy` is literally Verify step 5). Retargeting is fully documented in `handoff-space-deploy.md` and `verify.md`, forced by a payment-information wall, and confined to files nothing in `src/`, the image or the suite imports. `app.py` reads no environment (confirmed by `test_no_env_access.py`, which scans it) and uses the port as a literal for exactly that reason. **Accepted.**
- `src/api/run_registry.py` — not in the repo layout. In-process, replaced by `MemoryStore.save_run` in Phase 3, cost stated in its own docstring. **Accepted.**
- `harness/agent.py`'s `AgentPrompt` and `Agent`/`LLMAgent` — no Appendix A section defines them; marked DERIVED in the module docstring per the dispatch ruling. **Accepted.**
- `src/harness/observability.py`'s `run_scope` / ambient run-id `ContextVar` — not in A.9, and it fixes a real defect found while verifying. **Accepted.**

**Planned but missing**

- **Prompts as versioned `*.md` files** — finding 10. The one place PLAN.md's explicitly-rejected alternative was silently adopted, and `build.md` does not record it.
- **"the Diagnostician given a larger thinking budget"** (PLAN.md:158). `LLMAgent` and `LlmRequest` both carry `thinking_budget`, and `GeminiClient` wires it to `ThinkingConfig` — but `wiring.build_agents` passes it for neither agent, so both run at the provider default. There is no `Settings` field for it either, so it is not even expressible as config today.
- **`gateway_replay.py`'s "+ fault injection"** (repo layout line 120). `HARNESS_FAULT_INJECT` exists in `Settings` with its "refused when env != dev" comment, and is read by nothing anywhere in the tree.
- **Fly.io deploy** — replaced, documented, and the accepted regression (a Space has no persistent disk, so traces do not survive a restart) is written down in `verify.md`. Note `fly.toml` still has `min_machines_running = 0` against Open Risk 11's stated default of `1`; moot while Fly is not the target, but it will bite if anyone returns to it.
- `scripts/replay.py` / `scripts/eval.py` — in the layout, not in Phase 1's Built list, not used by the Verify block. **Not missing for this phase.**

---

## SUPERSEDING VERDICT   SHIP  — recorded 2026-09-11 against `d096e1f`

**Read this before acting on the FIX FIRST verdict at the top of this file.** That verdict was
correct when written and is now spent: every finding it raised is closed, and closure is
recorded finding-by-finding in `docs/progress/phase-1/backlog.md`. The original text is left
unedited, the same way `review-2.md`'s miscounted verdict line was left and corrected
elsewhere rather than rewritten.

**Authored by the coordinator, not by `phase-reviewer`.** That distinction is the point of
this section, so it is stated rather than buried:

- **Three independent `phase-reviewer` audits ran during this phase**, and between them
  produced the 24 findings that are now closed — the 12 in this file, 5 in `review-2.md`, and
  6 returned by a third pass over `741a292..862e8e0`.
- **The third pass reported in-conversation and wrote no file.** It was briefed not to, on the
  standing rule that agents do not write outside their territory and the coordinator records.
  That was right about territory and wrong about the paper trail: its verdict existed only in
  a session transcript. Its six findings are in `backlog.md` under "Closed by the final audit
  round"; this section is the missing verdict line.
- **The fix round that closed those six (`862e8e0..d2f7c6e`) has had no independent pass**, nor
  has `dcf480f..830332e`. Both are coordinator-verified and both are recorded as such under
  "Audit provenance" in `backlog.md`. That is the honest limit of this SHIP.

**Why SHIP rather than another round.** The stopping rule matters more than the verdict here,
because this phase demonstrated that every fix round introduces a defect: three rounds, three
new defects, each caught only by an independent pass. Taken literally that argues for auditing
forever. The reason to stop is that the last round is the smallest and best-evidenced of the
three — six fixes, each with a test proved non-vacuous by reproducing the defect under the old
code rather than merely passing under the new (the truncation test leaks `ghp_AAAAAAAAAAAAAAAA`
when the cut precedes the scrub; the marker sweep splits a marker at three of thirteen
offsets) — and that the remaining exposure is bounded and written down. An independent pass
over those two spans is still owed, is cheap, and is named in
`docs/progress/phase-2/handoff.md` §8 as the first thing to spend on if anything in the error
paths misbehaves.

**Gate at this tree:** 392 passed, 1 skipped; `ruff check` clean; `mypy --strict src/harness`
clean under both 1.14.1 and the pinned 2.3.1. Deployed to
<https://shakti-agent-harness.hf.space> and verified live — see `backlog.md`, "Deploy state —
closed".

Open findings: **none.** What remains is residuals and hazards, ranked and recorded in
`backlog.md`, plus the two design decisions in `handoff.md` §6 that belong to the project owner
rather than to a reviewer.
