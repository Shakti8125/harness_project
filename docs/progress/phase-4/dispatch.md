# Phase 4 — dispatch decisions (written BEFORE the build)

Written 2026-09-13 at `472120b`, the tree after `phase-3-green`. Normative for this phase.
Where this file and an implementer's instinct disagree, this file wins. Settled here so the
audit can check the build against a stated intent rather than reverse-engineer one.

## How this phase is being run

**Inline by the coordinator**, as Phases 2 and 3 were. Nothing runs in parallel, so no
agent needs another's territory while it is busy; `phase-reviewer` is dispatched once,
after the build, with named suspicions. The gate verdict (`test-verifier.md`) is
coordinator-run and recorded as such. Every live model call is budgeted in `verify.md`
before it is spent (handoff §1: quota is the demo's binding constraint).

## The one place this phase changes Phase 3's semantics (handoff §5.1–5.2)

### 1. Memory is written after evaluation, by the evaluate stage

The Diagnostician's `_remember` ran inside the `diagnose` stage, so a verdict the
Evaluator then refutes would already be tallied in `verdict_counts` at its pre-penalty
confidence, and `upsert_signature(verdict=None)` cannot un-count it. Decided: **the write
moves to the `evaluate` stage**, which is the first point at which the diagnosis's
`final_confidence` is final. The Diagnostician no longer takes `memory` or
`verdict_threshold`; the evaluate agent takes both, and `_counts_as_verdict` is decided on
the post-penalty confidence — so the threshold comparison and the remediation gate read the
same number (handoff §5.2). Dispatch decision 9 of Phase 3 ("who writes") is amended: the
sighting and the verdict are recorded by the evaluate stage; the Remediator's action
rewrite is unchanged.

A run that ends at the diagnose stage (the Diagnostician failed) records nothing, as before
— there is no verdict to remember. A run whose diagnosis is refuted is still a sighting
(`occurrences` counts it) and its observation row carries the penalised confidence and the
verdict; only the signature's tally depends on the threshold.

### 2. The evaluate stage re-files the diagnosis with the evaluator's adjustment

PLAN's adjustment table has two Evaluator rows (`evidence_fully_verified` +0.05,
`evidence_refuted` −0.15), and `final_confidence = clamp(self_confidence + Σ deltas)` —
sum first, clamp once. So the evaluate agent **re-calibrates from `self_confidence`** with
the Diagnostician's signals (rebuilt from `confidence_adjustments`) plus its own, and
replaces `state.artifacts["diagnosis"]` with the result. `RunState` is the one mutable
model and this is what it is for; the alternative — the gate and `build_facts` each adding
`evaluation.confidence_delta` to `diagnosis.final_confidence` — spreads the arithmetic
across three readers. The Diagnostician's `agent.run` span records the pre-penalty figure,
the evaluate stage's records the final one, and `EvaluationReport.confidence_delta` says
what changed. `AgentResult.confidence` for the evaluate stage is the final figure.

## Decisions

### 3. The Evaluator is a fourth stage, `evaluate`, with a deterministic agent

`StageSpec(name="evaluate", agent_key="evaluator", output_model=EvaluationReport)` between
`diagnose` and `remediate`; `ARTIFACT_KEYS["evaluate"] = "evaluation"`, so `final` gains
`evaluation` (A.1's `final` names exactly `bundle/diagnosis/evaluation/remediation`) and
`StageRecord.stage` takes the fourth value A.1 lists. The agent is
`integrations/cicd/agents/evaluator.py::EvidenceEvaluator` (`key = "evaluator"`) — a plain
`Agent`, not an `LLMAgent`: it makes no model call, has no prompt and no evidence of its
own. It builds one `Claim` per `Citation`, runs the harness `Evaluator` over the injected
checkers, records one `evaluation.claim` span per verdict (component `evaluator`, like
`policy.decide`), re-calibrates (decision 2), remembers (decision 1), and returns the
report. `evidence_refuted` on `fail` stays where Phase 1 put it: the gate on `remediate`.

The existing `remediation_gate` already reads `artifacts["evaluation"].verdict == "fail"`;
nothing in the orchestrator changes for the stage.

### 4. Claims are built from citations; checkers read the bundle, the diagnosis, and an availability model

`Claim{claim_id="cl_<n>", kind=claim_kind, payload={locator, quote, note}}`. The checkers
receive `artifacts = {"bundle": FailureBundle, "diagnosis": Diagnosis, "availability":
EvidenceAvailability}`. The third is new and small: `logs: bool, diff: bool`, computed by
the evaluate agent from the bundle and `state.degraded` — the Investigator's `"logs"` /
`"diff"` degraded components are the only record of *whether the artifact could be
fetched*, and PLAN's "missing artifact → `unverifiable`, never `refuted`" cannot be
decided without it (`files == []` after a failed compare is indistinguishable from
`infra_timeout`'s genuinely empty diff). A cold start (`baseline_kind == "none"`) has no
diff by definition: every diff-side claim on a cold-start run is `unverifiable`.

Per kind, the reading of PLAN's table:

- `quote_exists` — the quote and the source are whitespace-collapsed; substring → `exact`.
  Otherwise the best-matching *line* (a quote with k lines is compared to windows of k
  consecutive lines) by `difflib.SequenceMatcher.ratio() >= 0.92` → `verified`, detail
  `fuzzy 0.94`; below → `refuted`. The locator names the source (`log:job/<id>`,
  `diff:<path>`); if it names a job or file the bundle does not have, every source of
  that kind is searched and `matched_locator` says where the quote was found — the quote
  is the evidence, the locator its address. An unparseable locator searches everything.
- `file_in_diff` — the path is the locator's (`diff:<path>`) or the quote. Exact match on
  `DiffSummary.files[].path` (after stripping `./`, `a/`, `b/`) → `exact`; a match on a
  `/` boundary suffix → `verified`, detail `suffix`. Not found and `diff.truncated` →
  `unverifiable` (GitHub caps a compare at 300 files; absence is not proof); not found
  otherwise → `refuted`, detail `path not in diff (N files)`.
- `dependency_bump` — the quote is parsed for a package name and any version tokens
  (`pip: pydantic 1.10.13 -> 2.9.2`, `pydantic==2.9.2`, `pydantic 1.10.13 → 2.9.2` all
  read); a package the quote does not name falls back to `Diagnosis.suspected_package`.
  `verified` when a `DependencyChange` names that package and every version the quote
  mentions is that change's `from_version` or `to_version` (`from_version=None` for a new
  dependency tolerated, as PLAN says); `refuted` when no change names the package or a
  quoted version is neither; `unverifiable` when the diff is unavailable.
- `test_in_log` — the quote is the test id; `verified` when it is a substring of any line
  `fingerprint.anchor_lines(excerpt)` returns (handoff §4: the one anchor set every reader
  agrees on); `refuted` otherwise; `unverifiable` without a log.
- `commit_in_range` — the quote's first 7–40 hex token, prefix-matched against
  `DiffSummary.commit_shas` (new, decision 5) plus `head_sha`. `unverifiable` when the
  diff is unavailable, or when `commit_shas` is empty and the token is not the head (a
  cold start knows only its head commit).

The verdict: no claims → `skipped` (delta 0, "no claims to evaluate" — `no_citations`
already penalises this); any `refuted` → `fail`, delta −0.15; `verified/total < 0.5` →
`fail`, delta 0 (no citation was refuted, so PLAN's −0.15 row does not fire; the reason
string says which condition failed); any `unverifiable` → `warn`, delta 0; else `pass`,
delta +0.05. `total` is every claim, unverifiable included — PLAN's "Concrete numbers"
row is taken literally and recorded here rather than softened to the decidable share.

### 5. `DiffSummary.commit_shas` — A.11 amended additively

`commit_in_range` checks "the commit list between `base_sha` and `head_sha`", and nothing
retained that list: `diff_from_compare` reads `files` off the compare response and drops
`commits`. `DiffSummary` gains `commit_shas: list[str] = []`, filled from
`compare.commits[].sha` (and left empty on a cold start). Defaulted, so a stored bundle
from an earlier phase still validates.

### 6. `warn` downgrades in `decide_plan`, through a guardrails function

`guardrails.downgrade_for_warn(decision)` is the ladder PLAN states (`allow` →
`require_approval`, `require_approval` unchanged, `deny` unchanged) and the only writer
of `PolicyDecision.downgraded_from`; `remediation.decide_plan` applies it to every
side-effecting decision when `facts["evaluation.verdict"] == "warn"`, and the
`policy.decide` span carries `downgraded_from`. `plan_verdict` already turns
`require_approval` into a suspension, so a warned run that would have retried now waits
for a person. The ladder lives in the harness because `downgraded_from` and the effect
vocabulary do; the trigger lives in the integration because `evaluation.verdict` is its
fact namespace.

### 7. `skipped` leaves the policy; `warn` enters it

PLAN Phase 2 amendment 2: "Phase 4 removes `skipped` from the rule when a real verdict
exists." Both rules become `evaluation.verdict: {in: [pass, warn]}` — `warn` must match
so that decision 6 has an `allow` to downgrade. A diagnosis with no citations evaluates
`skipped` and therefore matches neither `retry-suspected-flaky` nor `open-fix-pr`: the
default deny answers, the run escalates `policy_denied` naming the clause, and
`file-ticket` (confidence only) still works. That is the honest reading of the phase goal
— the Remediator cannot act on a claim the log does not support, and no claim at all is
the limit case. The `EVALUATION_SKIPPED` constant stays as the fallback for a run with no
`evaluation` artifact (a hand-built orchestrator without the stage), which is what the
approval route sees for a run suspended before this phase.

### 8. The Remediator and the approval route read the run's real verdict

`Remediator(evaluation_verdict=...)` goes away; `build_facts` gets
`remediation.evaluation_verdict_of(state.artifacts.get("evaluation"))`, and
`_execute_approved` the same helper over `outcome.final.get("evaluation")`. One function,
two callers, the same fallback (decision 7).

### 9. The outbound webhook is a harness notifier the orchestrator awaits

`src/harness/escalation.py` (a layout addition, like `storage.py`): `EscalationNotifier`
(Protocol: `async deliver(run_id, record) -> EscalationRecord`) and `WebhookNotifier`
over `httpx` — B.4's 5 s timeout and 2 retries, exponential backoff, never raises. The
body is `{"text": <one line>, "run_id", "escalation_id", "reason", "message", "payload",
"trace_url"}`, so a Slack Incoming Webhook renders the first key and any other consumer
gets the structure; it is scrubbed through the same `Redactor` the recorder uses before it
leaves. The URL is a `SecretStr` (already registered with the redactor); the notifier
never puts it in a log line or an error string — `delivery_error` is built from the
exception class and the HTTP status, never from `str(exc)`, because httpx spells the URL
into its messages.

`EscalationRecord` gains `delivery_error: str | None = None` (A.1 amended additively;
B.4 names the field, the table has the column, the contract lacked it). `delivered_at`
means: with a webhook configured, when the webhook accepted the record (`None` and
`delivery_error` set when it did not); without one, when the log line was written, as
before. `SqliteMemoryStore.save_run` writes the record's `delivered_at` and
`delivery_error` onto the row instead of `now`. `Orchestrator._escalate` becomes async
and awaits the notifier when one is configured; it is not under the stage's `wait_for`,
so the delivery is bounded by the notifier's own budget (≤ 3 × 5 s + backoff), not the
run's. `main._escalation_after_approval` delivers through the same notifier, so an
approval-time escalation reaches the same channel. `ESCALATION_CHANNELS` in `deps.py`
grows `webhook` when `settings.escalation_webhook_url` is set.

### 10. Fault injection has one parser, one guard, and two homes

`src/harness/faults.py`: `Fault(name, count)`, `parse_fault("llm_bad_json:2")`, and
`FaultInjectingLlmClient` — an `LlmClient` that answers the first `count` requests
*per agent* with junk (`llm_bad_json`) or `LlmRateLimited` (`llm_429`) **without calling
the wrapped client**, then delegates. "Per agent" is keyed by the request's schema: each
`LLMAgent.run` translates its output model once and sends the same schema on every
attempt, and the client is built per run (`AppContext.build_orchestrator_for` wraps
`self.llm` for the run), so the count resets for every agent of every run. That is the
semantics under which PLAN's step 3 reads as written: `llm_bad_json:2` gives every stage
`attempts == 3`, `llm_bad_json:9` exhausts the Diagnostician (the Investigator degrades
and continues, as designed) and escalates `invalid_output`, `llm_429:3` completes on the
fourth transient attempt of each agent. A process-wide counter would spend the two junk
responses on the Investigator and leave `.stages[1].attempts == 1`.

`deps.build_fault(settings)` owns the `env != dev` refusal and the unknown-name refusal
(moved up from `build_memory_store`, handoff §4), against the union of the harness's
names (`sqlite_locked`, `llm_bad_json`, `llm_429`) and the integration's
(`wiring.FAULT_FABRICATE_CITATION = "diagnostician_fabricate_citation"`). The store fault
reaches the store, the LLM faults reach the per-run client wrapper, and the citation
fault reaches `build_agents(fabricate_citation=True)`: the Diagnostician then replaces
the model's citations with one `quote_exists` citation whose quote (`AssertionError:
expected 42`) is nowhere in the log, after the model has answered and before
calibration. That fault is domain-specific by nature — an LLM client cannot know what a
citation is — so it lives on the agent behind an explicit flag rather than in a schema-
sniffing rewrite at the client. `docker-compose.yml` forwards `HARNESS_FAULT_INJECT`
(handoff §1); the `env` guard is what keeps it harmless there.

### 11. `MAX_TOKENS` raises the output budget through `recovery.OutputBudget`

A.8's `call` takes only a prompt, so the loop has never been able to ask for more output
room (handoff §5.5; Phase 2 backlog). `retry_structured` gains `output_budget:
OutputBudget | None = None` — a small mutable object the caller's `call` closure and the
loop share. On `finish_reason == "MAX_TOKENS"` with a budget present, the loop grows it
×1.5 (capped at 65 536) before the repaired attempt and records `max_output_tokens` on
every `llm.attempt` span; the repair instruction now says the budget was raised and asks
for the complete object rather than only a shorter one. `LLMAgent.run` builds the budget
from its `max_output_tokens`. Without a budget, the loop behaves exactly as before, so
every existing test of the loop holds.

### 12. `scripts/eval.py` runs in-process, one fresh database per run, and says which model answered

The harness is the pipeline the API drives, minus HTTP: `AppContext` built by hand, the
orchestrator run per scenario, `--runs N --concurrency C`. **Each run gets its own
temporary database**, so every run is a first sighting: the labels (`effect: allow` for
`flaky_test`) are first-sighting labels, and a shared file would deny the third retry by
the cap and score a correct pipeline as a miss. `--shared-db` opts into one file for the
whole eval, which is how finding 10 (the cap under concurrency) is exercised — the report
then records the resulting `policy_denied` escalations rather than treating them as a bug
(handoff §5.6). `--llm gemini` (default) spends quota; `--llm stub` answers from
`tests/stubs.ScenarioStubLlm` (extended to every scenario) and the report's `llm` field
says so — a stub run measures the pipeline (gate, policy, forbidden set, the Evaluator
over canned citations), never the model, and the README must quote only a `gemini` run
for accuracy. Scored per `scenario.yaml`: `category` (the headline `category_accuracy`),
`min_confidence`, `commit`, `cites_any_of`, `action` (`Diagnosis.suggested_action`),
`effect` (`executed` → allow, `awaiting_approval` → require_approval, `denied` → deny),
`baseline_kind`, `cold_start`; `forbidden_actions_executed` counts executed tools in the
policy's forbidden set from both `final.remediation.executed` and the
`remediation.execute` spans; `escalation_rate`, `p50/p95` of `duration_ms`, mean tokens,
and cost from `--price-in/--price-out` (USD per million tokens; unpriced when omitted,
and the report says so). Exit 1 on `category_accuracy < 1.0` or
`forbidden_actions_executed > 0`, per PLAN.

### 13. `dependency_break` is the fourth fixture; `hallucination` is the fault injection

`fixtures/scenarios/dependency_break/`: `requirements.txt` bumps `pydantic==1.10.13` →
`2.9.2` in the diff (one file, so `parse_dependency_changes` yields one `pip` change),
the log fails at import with `PydanticImportError: BaseSettings has been moved to the
pydantic-settings package` and cascades into collection errors across every module; a
green baseline exists. Label: `dependency_break`, `min_confidence: 0.85`, the head
commit, `cites_any_of: [pydantic, BaseSettings, pydantic-settings]`, `action:
open_fix_pr`, `effect: require_approval`. The log comes from `gen_fixture_log.py` (a
third scenario there), deterministic like the others. The README's `hallucination` row
is not a fixture: PLAN's Verify block exercises the refuted-citation path through
`HARNESS_FAULT_INJECT=diagnostician_fabricate_citation` on `real_regression`, and a
fixture that steers a model into fabricating would be a prompt-injection test, not an
Evaluator test. The README table is updated to say so.

### 14. The Diagnostician prompt says what each claim kind's `quote` is (version 3)

The checkers parse the quote, so the prompt must say what to put in it: a verbatim log or
patch fragment for `quote_exists`, the path as listed for `file_in_diff`, the dependency
line as rendered for `dependency_bump`, the test id as it appears on a `FAILED` line for
`test_in_log`, the full sha for `commit_in_range`. The version bump reaches the trace
through the `prompt.render` span, as before.

### 15. The Verify block, as this phase can honestly meet it — PLAN amended

- **Step 1** passes as written (`tests/unit/test_evaluator.py`, the three named tests).
- **Step 2** — `.stages[].agent` on the escalated run is `["investigator",
  "diagnostician", "evaluator", null]`: the evaluate stage is a real stage with an agent,
  and the gated remediate stage records `agent: null` (it always did; PLAN's expected
  list elides it). The assertion that matters — no `"remediator"` — holds. Two live
  calls.
- **Step 3** — `attempts: 3` on `.stages[1]` under decision 10's per-agent semantics
  (three live calls: the third attempt of each agent). `llm_bad_json:9` spends none. The
  `sqlite3` line reads `invalid_output|db`: Phase 3 files the row on the `db` channel
  (dispatch decision 13) and the `channel` column names the row's own channel; the
  `channels` list on the record is where `log` (and `webhook`) appear. `llm_429:3`
  completes on three live calls after ≤ 3.5 s of jittered backoff per agent.
- **Step 4** — `--runs 5 --concurrency 1 --llm stub` is the CI gate and costs nothing;
  the README accuracy number comes from `--runs 1 --llm gemini` (six scenarios, ≤ 18
  calls, a day's quota on its own) and `verify.md` records which was run and when.

## Territory map for this phase (coordinator writes all of it)

| Area | Files |
|---|---|
| harness | `evaluator.py`, `escalation.py` (new), `faults.py` (new), `contracts.py` (`EscalationRecord.delivery_error`), `guardrails.py` (`downgrade_for_warn`), `orchestrator.py` (async `_escalate`, notifier), `recovery.py` (`OutputBudget`), `agent.py`, `memory.py` (`save_run` columns) |
| integration | `claim_checkers.py`, `agents/evaluator.py` (new), `agents/diagnostician.py`, `agents/remediator.py`, `remediation.py`, `wiring.py`, `schemas.py` (`DiffSummary.commit_shas`), `rendering.py`, `policy.yaml`, `prompts/diagnostician.md` |
| api | `deps.py`, `main.py`, `docker-compose.yml`, `.env.example` |
| fixtures / scripts | `dependency_break/`, `fixtures/README.md`, `scripts/gen_fixture_log.py`, `scripts/eval.py` (new) |
| tests | `test_evaluator.py`, `test_claim_checkers.py`, `test_faults.py`, `test_escalation_webhook.py`, `test_recovery_output_budget.py`, `test_evaluator_e2e.py`, `test_eval_script.py`; updates to the guardrails, remediation-stage, replay, memory and approvals tests; `tests/stubs.py` |
| docs | this file, `verify.md`, `test-verifier.md`, `review.md` (reviewer), `backlog.md`, PLAN.md amendments, the Phase 5 handoff |
