# Phase 4 — Verify block, literal expected vs actual

Run 2026-09-14 (00:35–00:50 IST) against `dbc63ab` (the build commit), locally: `uv run
uvicorn src.api.main:app --host 127.0.0.1 --port 8000`, one process per
`HARNESS_FAULT_INJECT` value (settings are read once at boot), over a **fresh**
`./data/harness.db` (the Phase 3 file was moved to `harness.db.phase3-<ts>.bak` first), with
the real Gemini key from `.env` (`HARNESS_ENV=dev`, `HARNESS_GATEWAY=replay`,
`HARNESS_DRY_RUN=true`). No Docker container was listening on 8000 this time. The server was
addressed as `localhost:8000` exactly as the block spells it (it resolved to the uvicorn
process; the Phase 3 hazard was a container that is gone).

**Live quota spent: 3 model calls** (step 2: two; step 3a: one before the provider itself
answered 429). The rest of the block is free by construction: `llm_bad_json` answers never
leave the process, and step 4 ran under `--llm stub`.

The Verify block as amended by this phase's PLAN.md note (item 14): step 2's `stages` list
carries the evaluate stage and the gated stage's `null`; step 3's `attempts` is per agent;
the `sqlite3` line reads `invalid_output|db`; step 4's gate is the stub run.

## Step 1 — a fabricated citation is caught and blocks remediation

```
uv run pytest tests/unit/test_evaluator.py -q
20 passed in 0.25s

PASSED tests/unit/test_evaluator.py::test_fabricated_quote_refuted
PASSED tests/unit/test_evaluator.py::test_paraphrased_quote_verified_fuzzy
PASSED tests/unit/test_evaluator.py::test_missing_artifact_is_unverifiable_not_refuted
```

`test_fabricated_quote_refuted` builds a `Diagnosis` citing `AssertionError: expected 42`
against a bundle whose log has no such line: verdict `fail`, `confidence_delta == -0.15`,
the claim `refuted` with detail `quote not found in log:job/601234567`.
`test_paraphrased_quote_verified_fuzzy`: the whitespace-collapsed quote is `verified`/`exact`
(collapse makes it a substring), and a one-character retype of a 40-character line is
`verified`/`fuzzy 0.9x` at or above the 0.92 line. `test_missing_artifact_is_unverifiable_
not_refuted`: with no log collected the log claim is `unverifiable`, `refuted == 0`, delta 0
— and the report is `warn` beside a verified diff claim, `fail`-by-share alone (PLAN
amendment item 4, the literal reading).

## Step 2 — refuted evidence escalates and the Remediator never runs

`HARNESS_FAULT_INJECT=diagnostician_fabricate_citation`, `POST /v1/replay/real_regression`
(28.2 s, two model calls, 35 429 tokens):

```
{
  "status": "escalated",
  "verdict": "fail",
  "reason": "evidence_refuted",
  "stages": ["investigator", "diagnostician", "evaluator", null]
}
```

Expected `{"status":"escalated","verdict":"fail","reason":"evidence_refuted","stages":
["investigator","diagnostician"]}` — **matches as amended**: the evaluate stage is a real
stage with an agent, the gated remediate stage records `agent: null` (it always has; the
Phase 1 low-confidence run showed the same), and there is no `"remediator"`. The rest of
the body, run `run_01M2E2MPF9KT5H58VVT8GR6ZRT`:

- `escalation.message`: `evaluator verdict fail: 1 of 1 claim(s) refuted`
- `final.evaluation`: `verified 0, refuted 1, unverifiable 0, confidence_delta -0.15`; the
  one verdict `{kind: quote_exists, result: refuted, detail: "quote not found in
  log:job/601234567", matched_locator: null}`
- `final.diagnosis.citations`: the single fault-injected citation quoting `AssertionError:
  expected 42` with the note `fault-injected: this line is not in the log`; the model's own
  citations were replaced after it answered (`category: real_regression`,
  `self_confidence: 0.95`)
- `final.diagnosis.final_confidence`: 0.80 = 0.95 − 0.15, `confidence_adjustments`
  `[evidence_refuted −0.15]`
- `final` has `bundle`, `diagnosis`, `evaluation` and no `remediation`; `stages[3]` is
  `{stage: remediate, agent: null, status: gated}`
- the trace carries one `evaluation.claim` span (component `evaluator`, `result: refuted`)
  under the evaluator's `agent.run`, and the `escalation` row reads `evidence_refuted|db`.

## Step 3 — Recovery recovers

### 3a `llm_bad_json:2` — the fault worked; the provider's quota did not (re-run pending)

`POST /v1/replay/flaky_test` (19.5 s):

```
{"status": "escalated", "attempts": 4}
```

Expected `{"status":"completed","attempts":3}`. What actually happened, from the body and
the `llm.attempt` spans of `run_01M2E2P2C3P754TPVW5T2J8YA9`:

| Stage | Attempts | What each was |
|---|---|---|
| investigate | 3 | 1–2: the injected non-JSON answer, `tokens.total 0`, `finish_reason STOP`, classified `invalid_output` and repair-retried; 3: a real call, 17 608 tokens, `ok` |
| diagnose | 4 | 1–2: the injected answer, 0 tokens; 3–4: real calls answered **`LlmRateLimited` by Gemini**; the loop ended on the stated delay against `RETRY_DELAY_BUDGET_S` and the run escalated `rate_limited` |

So the fault did exactly what item 9 says — two junk answers per agent, none reaching the
provider, the Investigator recovering with `attempts == 3` — and the third attempt of the
Diagnostician met the free tier's daily cap (Phase 3's 15 verify calls and the Space check's
3 were made in the same Pacific-time day as today's 3). The `rate_limited` escalation is the
Phase 1 path working as designed. **This step is re-run once the quota resets; its expected
values are pinned in-process by
`tests/integration/test_evaluator_e2e.py::test_llm_bad_json_2_recovers_with_three_attempts_
and_one_real_call_per_agent` (`[3, 3, 1, 3]` attempts, three calls to the model behind the
fault).**

### 3b `llm_bad_json:9` — never recovers, spends nothing

`POST /v1/replay/flaky_test` (0.39 s, **zero** model calls):

```
{"status": "escalated", "reason": "invalid_output"}
```

**Matches.** `run_01M2E2RHH1F88PRWXK71J69HKH`: `stages` `[investigate ok attempts 3,
diagnose invalid_output attempts 3]` — the Investigator degrades (`degraded_components:
["investigator_notes"]`) and continues, the Diagnostician exhausts its three structured
attempts and the run ends there; `total_tokens.total: 0`; six `llm.attempt` spans all with
`tokens.total 0`; `escalation.message`: `model output did not satisfy the contract`;
`escalation.channels: ["log", "db"]`.

```
sqlite3 ./data/harness.db "select reason, channel from escalation order by created_at desc limit 1;"
invalid_output|db
```

Expected `invalid_output|log` — **matches as amended** (item 14): since Phase 3 the row is
filed by `save_run` on the `db` channel and the `channel` column names the row's own
channel; `log` (and `webhook`, when configured) are on the record's `channels` list.

### 3c `llm_429:3` — pending quota

Not run: it needs three real calls (the fourth attempt of each agent) and the quota was
already exhausted at 3a. Pinned in-process by `test_llm_429_3_completes_on_the_fourth_attempt`
(`[4, 4, 1, 4]` attempts, twelve `llm.attempt` spans of which nine carry an error, three
calls to the model behind the fault). **Re-run once the quota resets.**

## Step 4 — the eval harness

```
uv run python scripts/eval.py --runs 5 --concurrency 1 --llm stub
eval (stub, 5 run(s) x 5 scenario(s), concurrency 1, db fresh-per-run)
  category_accuracy          1.00 (25/25)
  forbidden_actions_executed 0
  escalation_rate            0.20
  latency_ms p50/p95         187/219
  mean tokens                4500
  est. cost per run (USD)    0.0 [unpriced (pass --price-in/--price-out)]
  cold_start         5/5 correct, 0 label miss(es), 5 escalated, verdicts {'pass': 5}
  dependency_break   5/5 correct, 0 label miss(es), 0 escalated, verdicts {'pass': 5}
  flaky_test         5/5 correct, 0 label miss(es), 0 escalated, verdicts {'pass': 5}
  infra_timeout      5/5 correct, 0 label miss(es), 0 escalated, verdicts {'pass': 5}
  real_regression    5/5 correct, 0 label miss(es), 0 escalated, verdicts {'pass': 5}
exit 0
```

`eval_report.json` (gitignored; generated) carries `category_accuracy 1.0`,
`forbidden_actions_executed 0`, `escalation_rate 0.2` (the five `cold_start` runs, each
`policy_denied` on the `context.cold_start` clause — the label's `effect: deny`), `latency_ms
{p50 187, p95 219, mean 184}`, `tokens {mean_prompt 3600, mean_completion 900, mean_total
4500}` (three stub calls at 1 500), `estimated_cost_usd_per_run 0.0` with `cost_basis:
unpriced`, `wall_clock_s 5.6`, and per-run rows with every evaluation verdict (`pass` × 25,
`refuted_claims 0`).

**What this number is and is not.** PLAN's expectation is `category_accuracy == 1.00
(20/20)` for four scenarios; the set is five now and the stub run is 25/25. The stub run
measures the pipeline — the four stages over the real fixtures, the Evaluator over their
real logs and diffs (every canned citation verified against the artifact it names), the
policy, the forbidden set, memory — and **not the model**: the report says `llm: stub`, and
the README must quote a `--llm gemini` run for accuracy. That run (`--runs 1`, five
scenarios, ≤ 15 calls) is a day's quota on its own and is the second thing to spend on
once the quota resets, after 3a/3c; the handoff carries the instruction. The shared-database
variant (`--shared-db --runs 3 --scenario flaky_test`, stub) was also run once during the
build: 3/3 correct, and the third run's `effect: 'deny' != 'allow'` label miss is the retry
cap biting as designed — reported, not gated.

## Gate

Recorded in `test-verifier.md`: ruff clean, `mypy --strict src/harness` clean (17 files),
`mypy src` at the 8 pre-existing errors, **736 passed, 2 skipped**.
