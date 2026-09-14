# Phase 4 — Verify block, literal expected vs actual

Run 2026-09-14 (00:35–00:50 IST) against `dbc63ab` (the build commit), locally: `uv run
uvicorn src.api.main:app --host 127.0.0.1 --port 8000`, one process per
`HARNESS_FAULT_INJECT` value (settings are read once at boot), over a **fresh**
`./data/harness.db` (the Phase 3 file was moved to `harness.db.phase3-<ts>.bak` first), with
the real Gemini key from `.env` (`HARNESS_ENV=dev`, `HARNESS_GATEWAY=replay`,
`HARNESS_DRY_RUN=true`). No Docker container was listening on 8000 this time. The server was
addressed as `localhost:8000` exactly as the block spells it (it resolved to the uvicorn
process; the Phase 3 hazard was a container that is gone).

**Live quota spent: 3 model calls on 2026-09-14 00:35–00:50 IST** (step 2: two; step 3a's
first attempt: one, before the provider itself answered 429 — the daily cap, see 3a) and
**6 more at 13:15–13:20 IST**, after the reset, on the post-fix tree `d205151` (3a and 3c,
three each). The rest of the block is free by construction: `llm_bad_json` answers never
leave the process, and step 4 ran under `--llm stub`.

The Verify block as amended by this phase's PLAN.md note (item 14): step 2's `stages` list
carries the evaluate stage and the gated stage's `null`; step 3's `attempts` is per agent;
the `sqlite3` line reads `invalid_output|db`; step 4's gate is the stub run. **Every step
passes**; 3a and 3c were run on the post-fix tree, the others on the build commit (the fix
round changes none of their expected values — `backlog.md`, "Audit provenance").

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

### 3a `llm_bad_json:2` — recovers

Re-run 2026-09-14 13:15 IST on `d205151`, `POST /v1/replay/flaky_test` (54.0 s, three
model calls, 40 338 tokens):

```
{"status": "completed", "attempts": 3}
```

**Matches.** `run_01M2FDW8TE445JFZWHE3S7GT2W`: `stages` `[investigate ok 3, diagnose ok 3,
evaluate ok 1, remediate ok 3]` — every model-backed agent's first two attempts were the
injected non-JSON answer (zero tokens), the third real; the evaluate stage makes no call.
The real diagnosis cited four claims, all `verified` (`pass`, +0.05, `final_confidence
0.90`, `flaky_test`); the retry-suspected-flaky rule allowed the rerun on a first sighting
and it executed dry-run (`rerun_failed_jobs ok dry_run:true`); `degraded_components: []`.

*The first attempt, 00:45 IST on `dbc63ab`, is kept below because it is how the daily
cap was discovered.* `POST /v1/replay/flaky_test` (19.5 s): `{"status": "escalated",
"attempts": 4}`. From the body and the `llm.attempt` spans of
`run_01M2E2P2C3P754TPVW5T2J8YA9`:

| Stage | Attempts | What each was |
|---|---|---|
| investigate | 3 | 1–2: the injected non-JSON answer, `tokens.total 0`, `finish_reason STOP`, classified `invalid_output` and repair-retried; 3: a real call, 17 608 tokens, `ok` |
| diagnose | 4 | 1–2: the injected answer, 0 tokens; 3–4: real calls answered **`LlmRateLimited` by Gemini**; the loop ended on the stated delay against `RETRY_DELAY_BUDGET_S` and the run escalated `rate_limited` |

So the fault did exactly what item 9 says — two junk answers per agent, none reaching the
provider, the Investigator recovering with `attempts == 3` — and the third attempt of the
Diagnostician met the free tier's daily cap (Phase 3's 15 verify calls and the Space check's
3 were made in the same Pacific-time day as that night's 3; the cap resets at midnight
Pacific, ~12:30 IST). The `rate_limited` escalation is the Phase 1 path working as designed.
The expected values are also pinned in-process by
`tests/integration/test_evaluator_e2e.py::test_llm_bad_json_2_recovers_with_three_attempts_
and_one_real_call_per_agent` (`[3, 3, 1, 3]` attempts, three calls to the model behind the
fault).

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

### 3c `llm_429:3` — completes on the fourth attempt

Run 2026-09-14 13:18 IST on `d205151`, `POST /v1/replay/flaky_test` (92.1 s — three
agents × three jittered backoffs of up to 0.5, 1 and 2 s, plus the three real calls; three
model calls, 41 785 tokens):

```
completed
```

**Matches.** `run_01M2FDYD030GSGRFJPN4ENFHKA`: `stages` `[investigate ok 4, diagnose ok 4,
evaluate ok 1, remediate ok 4]`; twelve `llm.attempt` spans on the run, nine carrying
`LlmRateLimited` (the injected 429s, `retry_after_s` unset so the backoff path, not the
stated-delay path, was exercised) and three clean; the diagnosis `flaky_test` at 0.99 with
four claims verified; the retry executed dry-run. Pinned in-process by
`test_llm_429_3_completes_on_the_fourth_attempt` (`[4, 4, 1, 4]`, 12 spans / 9 errors).

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
the README quotes only the `--llm gemini` run for accuracy. The shared-database variant
(`--shared-db --runs 3 --scenario flaky_test`, stub) was also run once during the build:
3/3 correct, and the third run's `effect: 'deny' != 'allow'` label miss is the retry cap
biting as designed — reported, not gated.

### The live number (the model), 2026-09-14 13:25 IST, on `5abe6ad`

Nine calls were left of the day after 3a, 3c and the Space check, so three scenarios:

```
uv run python scripts/eval.py --runs 1 --concurrency 1 --llm gemini     --scenario real_regression --scenario flaky_test --scenario infra_timeout
eval (gemini, 1 run(s) x 3 scenario(s), concurrency 1, db fresh-per-run)
  category_accuracy          1.00 (3/3)
  forbidden_actions_executed 0
  escalation_rate            0.00
  latency_ms p50/p95         41109/86375
  mean tokens                40625
  est. cost per run (USD)    0.0 [unpriced (pass --price-in/--price-out)]
  flaky_test         1/1 correct, 0 label miss(es), 0 escalated, verdicts {'pass': 1}
  infra_timeout      1/1 correct, 0 label miss(es), 0 escalated, verdicts {'pass': 1}
  real_regression    1/1 correct, 0 label miss(es), 0 escalated, verdicts {'pass': 1}
exit 0
```

`model: gemini-3.6-flash`; per run: `flaky_test` completed (86.4 s, 41 508 tokens, the retry
executed dry-run), `infra_timeout` completed (28.5 s, 38 400), `real_regression`
awaiting approval (41.1 s, 41 969); every evaluation verdict `pass`, `refuted_claims 0`,
no label miss on any of `min_confidence`, `commit`, `cites_any_of`, `action`, `effect`,
`baseline_kind`, `cold_start`. Tokens: mean prompt 34 172, completion 1 301. Unpriced.

**`dependency_break`, live but not through the eval:** the Space's post-deploy check
(below, §"Deploy") ran it with the real model — `dependency_break` at 0.99, three citations
of three kinds all verified, `open_fix_pr` awaiting approval, `baseline_kind branch_green`,
`cold_start false`, the head commit named — which meets every key of its label when scored
by hand. **`cold_start` and a scored `dependency_break` row are queued for the next quota
window** (six calls); the README's table says three scenarios until then.

## Deploy — 2026-09-14 13:16–13:30 IST

`git push space master:main`, a fast-forward `472120b..5abe6ad` (the `phase-4-green` tag).
The new build was polled with `POST /v1/replay/dependency_break`, which the old image answers
`404` (no such fixture): `404` at 13:16:53 and 13:17:25, `200` at 13:19:37 — about 2 m 40 s
after the push, one live replay (three model calls, 100.8 s, 53 969 tokens):
`run_01M2FE63EX8GR5BGYMYPX8DD02`, `awaiting_approval`, stages `[investigate 1, diagnose 2,
evaluate 1, remediate 1]` (the Diagnostician's first attempt did not validate and Recovery's
repaired second one did), `dependency_break`, `self_confidence 0.95` → `0.99` with
`evidence_fully_verified`, citations `commit_in_range` (the head sha), `dependency_bump`
(`pip: pydantic 1.10.13 -> 2.9.2 (requirements.txt, manifest_diff)` — the rendered line, as
the v3 prompt asks) and `quote_exists` (the `PydanticImportError` line), all `verified`;
`open-fix-pr` on all three calls, `require_approval`, nothing downgraded, nothing executed.

Free checks on the same container: `healthz` `{"status":"ok","db":"ok","version":"0.1.0"}`;
`readyz` all four fields true; `GET /v1/runs` one row, `awaiting_approval`; the run's trace
19 spans with components `agent, evaluator, guardrails, llm, orchestrator` and names
including `evaluation.claim` (×3) and `policy.decide`; `GET /v1/escalations` `[]`;
`POST /v1/approvals/apr_nope`, `GET /v1/runs/run_01J8NOPE…`, `POST /v1/replay/hallucination`
all `404 application/problem+json`; 25 771 bytes of served bodies with zero
credential-shaped matches. The exposure story is unchanged (no authentication, replay
gateway, dry-run, no allowlisted repo, no persistent volume).

## Gate

Recorded in `test-verifier.md`: ruff clean, `mypy --strict src/harness` clean (17 files),
`mypy src` at the 8 pre-existing errors, **736 passed, 2 skipped**.
