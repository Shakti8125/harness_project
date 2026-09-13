# Phase 4 — audit (`phase-reviewer`, independent, read-only)

Dispatched 2026-09-14 against `phase-3-green..e43b99c` (the build `dbc63ab` and the records
`e43b99c`), with the brief's ten named suspicions. The report below is the reviewer's text
verbatim; the coordinator's fix round and its verification are in `backlog.md` ("Audit
provenance") and `test-verifier.md` (the last section).

---

## VERDICT: FIX FIRST

Three small fixes (findings 1–3), each a few lines. Nothing in the harness layer needs redesign. Note separately that Verify steps 3a and 3c are still pending live re-runs per `verify.md`; the phase's own definition of done (handoff §9) makes the tag conditional on them regardless of this review.

## Findings

**1. [medium] `src/integrations/cicd/agents/evaluator.py:162` — a refuted (`fail`) diagnosis is still tallied as a signature verdict whenever the post-penalty confidence clears 0.70.**
`verdict = diagnosis.category if self._counts_as_verdict(diagnosis) else None` looks only at the threshold; the report in hand is ignored. Dispatch decision 1 says "only the signature's tally depends on the threshold", which contradicts PLAN's "a refuted claim overrides confidence entirely — grounding beats self-belief" and the Phase 3 finding-4 principle (a verdict the gate refuses is a sighting, not a verdict) without saying so — the evidence gate refuses these runs.
Failure scenario (reproduced): three `real_regression` replays under `diagnostician_fabricate_citation` at self-confidence 0.95 → each escalates `evidence_refuted`, the Remediator never runs, and `failure_signature` reads `(3, 'real_regression', '{"real_regression":3}')`. The fourth run of that signature gets `memory_agreement` +0.10 (≥3 priors, 100 % share) from three diagnoses the harness refused to act on; at self 0.60–0.69 with verified citations that lifts it over the 0.70 gate. The existing test pins only the 0.80−0.15=0.65 case, where the threshold happens to catch it.
Smallest fix: pass `report` into `_remember` and use `verdict = diagnosis.category if report.verdict != "fail" and self._counts_as_verdict(diagnosis) else None`; add the 0.95 case to `test_the_verdict_is_remembered_after_the_penalty_not_before`.
Owner: cicd-integration

**2. [medium] `src/harness/escalation.py:107,110` — httpx itself logs the webhook URL at INFO on every delivery, on both client paths.**
httpx's `_client.py` emits `logger.info('HTTP Request: %s %s "..."', method, request.url, ...)` on the `httpx` logger for every request, mock transport included. Reproduced: with `logging.basicConfig(level=logging.INFO)` a successful delivery prints `INFO:httpx:HTTP Request: POST https://hooks.example.com/services/T000/B000/SECRETPART "HTTP/1.1 200 OK"`. Latent today only because nothing in the repo configures the root logger — but `Settings.log_level` (`HARNESS_LOG_LEVEL=INFO`) exists precisely to do that, and `tests/unit/test_escalation_webhook.py:111` captures only `harness.escalation` at WARNING, so the path is untested. Unlike the Gemini/GitHub clients, here the URL *is* the credential. The `Redactor` scrubs spans and rows, not log records.
Smallest fix: `logging.getLogger("httpx").setLevel(logging.WARNING)` where the notifier is built (or a `Redactor`-backed `logging.Filter` on the root handler, the principled version), plus a caplog test at INFO on the root logger asserting the URL is absent.
Owner: harness-core

**3. [medium] `src/integrations/cicd/claim_checkers.py:50,320` — `_VERSION` swallows trailing `-`/`.` into the version token, refuting correct `dependency_bump` claims on punctuation.**
`(?<![\w.])v?(\d+(?:\.\d+)+[0-9A-Za-z.+-]*)` — the tail class includes `-` and `.`. Reproduced against the `dependency_break` bundle: `pydantic 1.10.13->2.9.2` → `refuted` ("not 1.10.13-, 2.9.2"); `pydantic bumped from 1.10.13 to 2.9.2.` → `refuted` ("not 1.10.13, 2.9.2."). Both are true claims; each costs −0.15, `evidence_refuted`, and the Remediator is skipped. PLAN's own justification for the 0.92 fuzzy line applies verbatim: "A strict check refutes correct diagnoses on formatting alone, which is worse than the alternative failure." The parametrised test covers `bumped from 1.10.13 to 2.9.2` without the period and `->` only with spaces (the unicode `→` without spaces happens to work because it is outside the class).
Smallest fix: `return [m.group(1).rstrip(".+-") for m in _VERSION.finditer(quote)]` in `_versions_in`, and add the two shapes to `test_dependency_bump_verified_shapes`.
Owner: cicd-integration

**4. [low] `src/integrations/cicd/prompts/diagnostician.md:21` vs `src/integrations/cicd/rendering.py:208` — the v3 prompt promises a commit list the rendering never shows.**
The prompt says `commit_in_range`'s quote is "the full commit sha as listed under 'Diff against the baseline'"; `render_diff_summary` lists only `base <sha> -> head <sha>` and files. `DiffSummary.commit_shas` (decision 5) is never rendered, so the model can never cite an intermediate commit — the "in range" branch of `CommitInRangeChecker` is unreachable from the prompt — and the one other sha it is shown, the base, is refuted (`5e7a9b1c… is not among the 1 commit(s) in range`). Concrete: a range with 4 commits, the model blames the second → it cannot quote it; it quotes the base sha it can see → −0.15 and `evidence_refuted`.
Smallest fix: render `commit_shas` (oldest first) under "Diff against the baseline".
Owner: cicd-integration

**5. [low] `scripts/eval.py:193` — `sqlite_locked` passes the guard but never reaches the store.**
`memory=build_memory_store(scoped, redactor)` is built with `fault=None` before `__post_init__` parses `HARNESS_FAULT_INJECT`; LLM faults and the citation fault route through `build_orchestrator_for`, the store fault does not. `HARNESS_FAULT_INJECT=sqlite_locked uv run python scripts/eval.py --llm stub` runs on a healthy store while `context.fault` says otherwise — the "injects nothing silently" case `build_fault`'s docstring says it exists to prevent.
Smallest fix: drop `memory=` from the `AppContext(...)` call so `__post_init__` builds the store with the fault (it uses `self.settings`, the scoped copy), or pass `fault=build_fault(scoped)` to both.
Owner: fixtures-eval

## Named suspicions

- **S1 — NOT A PROBLEM.** Every downstream reader dereferences `state.artifacts["diagnosis"]` after the replacement at `agents/evaluator.py:210`: the gate (`wiring.py:172`), `Remediator._diagnosis(state)`, `final` (built from `state.artifacts` at `orchestrator.py:568`), `_remember(recalibrated, …)`. The pre-penalty figure survives only in the Diagnostician's `AgentResult.confidence`, which is not served: `StageRecord` carries no confidence and `LLMAgent.run` sets no confidence attribute on its span (`agent.py:249–260`).
- **S2 — I rule for the literal reading, and the recorded rationale is honest.** Under "any `refuted` → fail", the share condition over *decidable* claims would be dead code (refuted>0 already fails); it is live only when `total` includes unverifiable claims, so PLAN's numbers row can only mean what `evaluator.py:137` does. "Absence of evidence is not evidence of hallucination" governs the per-claim verdict (never `refuted`), which the checkers honour. `final.evaluation.reason` and the gate message name the share; the one dishonest token is the `evidence_refuted` enum, which amendment item 4 admits and backlogs. Recommend an additive A.1 reason (`evidence_unverifiable`) in Phase 5.
- **S3 — NOT A PROBLEM.** One `generate` call site (`agent.py:225`), schema built once per `run` (`:219`); `retry_structured` never alters the schema (PLAN's "attempt 3 strips optionals" is a Phase 1 adaptation, unimplemented); the three LLM agents use three distinct output models (`InvestigationNotes`, `Diagnosis` via `_diagnosis_schema`, `RemediationPlan`); the evaluate agent makes no call. Would break for a future agent sharing an output model or a schema-less request (`json.dumps(None)` → all share `"null"`).
- **S4 — NOT A PROBLEM on the named paths; CONFIRMED on an unnamed one (finding 2).** `delivery_error` comes from `_describe_failure` (class/status only); the notifier's `logger.warning` logs `last_error`; the `exc_info=True` at `orchestrator.py:334` / `deps.py:238` is reachable only by exceptions from `webhook_body`/`format`/`scrub` outside the try, none of which carry the URL; `httpx.InvalidURL` (not an `HTTPError`) is caught by `except Exception` and named by class. Injected and per-delivery paths both resolve to `httpx.Timeout(5.0)`.
- **S5 — NOT A PROBLEM, one caveat.** `_escalate` runs inside `heartbeating` (`orchestrator.py:350`), so the heartbeat continues; it is outside `wait_for`, so not charged to `run_budget_s`; the replay route awaits inline with no client timeout. Caveat: `httpx.Timeout(5.0)` is per phase (connect/write/read each 5 s), so one attempt can take ~15 s and the worst case is ~46 s, not "3 × 5 s + backoff" — held under the concurrency semaphore (`main.py:780–784`).
- **S6 — NOT A PROBLEM.** `downgrade_for_warn` rewrites only `allow` (`guardrails.py:112`), preserving `rule_id`/`obligations` via `model_copy(update=)`; `<default>`/`<forbidden>` are `deny` and untouched; `plan_verdict` → `await_approval`; `_execute_approved` re-judges under the stored `warn` (`evaluation_verdict_of` Mapping branch), gets `require_approval` again, executes on `verdict in ("execute","await_approval")` (`main.py:1293`), and `_record_approved_action` records. No upgrade path exists.
- **S7 — CONFIRMED** (findings 3 and 4). Probe results in the residuals for the rest.
- **S8 — CONFIRMED for `scripts/eval.py`** (finding 5). Tests' `make_context` passes no `memory=`/`fault=` so `__post_init__` routes both; `replay.py` and `app.py` use `get_app_context()`.
- **S9 — NOT A PROBLEM.** Gate is exactly `accuracy < GATE_ACCURACY or forbidden_executed > 0` (`eval.py:422`); the `tests.stubs` import is lazy inside `if mode == "stub"` (`:201`), and the image ships neither `scripts/` nor `tests/` (Dockerfile copies `src` and `fixtures` only). Shared-db caveat is stated in the docstring and visible via `db: "shared"` plus per-run `escalation_reason`; the summary line's "N escalated" does not distinguish it (residual).
- **S10 — NOT A PROBLEM.** `grow()` at `recovery.py:430` runs before the next iteration; the next attempt's span sets `max_output_tokens` at `:321` before `call`, so it records the grown value. The `too_large` branch (`:358–360`) leaves the budget alone. No cumulative cap; each attempt capped at 65 536, at most two growths (MAX_TOKENS counts toward `max_attempts`): Remediator 8192 → 12288 → 18432.

**Fixture `dependency_break`:** consistent. Repo/run 501235417/attempt 1/head `c3d9e1f2…` agree across `webhook.json`, the jobs list (job 601235417), the baseline run (`5e7a9b1c…` on `main`), the compare filename and body (`commits[]` = `[head]`, `total_commits 1`, one modified `requirements.txt`), and the log (`* [new ref] c3d9e1f2…`, 15 `ERROR collecting` sections). `parse_dependency_changes` yields exactly `pip pydantic 1.10.13 -> 2.9.2`; all four stub citations verify (`exact`, `exact`, `head commit`, dependency detail).

## Contract drift

- **A.8 `retry_structured`** — the appendix block still shows the Phase 1 signature; the `output_budget` keyword is recorded only in amendment item 10. Update the block.
- **A.2 `remediation_gate` example** — `reason="evidence refuted by evaluator"` vs code `f"evaluator verdict fail: {why}"`. Illustrative string, not a field.
- **`Evaluator.check` / `Evaluator.kinds`** — additive, unrecorded, harmless.
- **`EvidenceAvailability`** — new model in `claim_checkers.py`, recorded in dispatch decision 4, absent from A.11; internal (never serialised).
- `EscalationRecord.delivery_error` and `DiffSummary.commit_shas` — recorded in A.1/A.11. No field renames, type changes, or default changes found in any Appendix A model.

## Residual notes (backlog lines, not findings)

- `dependency_bump` with no version in the quote verifies vacuously (`"pydantic"` alone; `"pydantic v1 -> v2"`) — pinned by test as intended; a two-package quote is refuted.
- `quote_exists` with an empty quote → `refuted` (−0.15) rather than `unverifiable`; rendering artefacts the model can see but the artifact lacks (`--- path (status)`, `### log`, elision markers) refute if quoted.
- `_SHA` (`\b[0-9a-f]{7,40}\b`) treats a 9-digit job id as a sha candidate.
- A pending approval created before the Phase 4 deploy is judged under `skipped` and denied by name (dispatch 7 says so); worth one line in the deploy notes.
- `WebhookNotifier` per-attempt wall time is ~15 s worst case, not 5 s (S5).
- `HARNESS_ESCALATION_WEBHOOK_URL` is not validated at boot; a malformed value surfaces only as `delivery_error` on the first escalation.
- `docker-compose.yml`'s `HARNESS_FAULT_INJECT: ${HARNESS_FAULT_INJECT:-}` overrides a value set in `.env` with the empty string when the shell does not export it.
- `faults.py:6` docstring names the integration's fault (`diagnostician_fabricate_citation`) — outside the layering denylist, but the harness "knowing" it is a cosmetic leak.
- `test_escalation_webhook.py` should capture the root logger at INFO (see finding 2).

I did not write `docs/progress/phase-4/review.md` — the brief was read-only; this text is the review for the coordinator to file.
