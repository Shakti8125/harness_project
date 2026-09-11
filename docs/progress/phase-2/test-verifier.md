# Phase 2 — gate verdict

**Verdict: PASS** — with one step blocked on environment and one recorded against the plan's
own amendment, both stated in `verify.md` rather than folded into the PASS.

**Provenance: coordinator-run, not agent-run.** This phase was built inline (see
`dispatch.md`, "How this phase is being run"), so the person writing the tests and running the
gate is the person who wrote the code. That is weaker evidence than a separate `test-verifier`
pass, and it is why the independent `phase-reviewer` audit that follows is the load-bearing
half of closing this phase, not a formality. Recorded the way Phase 1's coordinator-verified
rounds were recorded (`docs/progress/phase-1/backlog.md`, "Audit provenance").

## The gate, at `020a64f`

```
$ uv run pytest -q
486 passed, 2 skipped, 2 warnings in 32.36s
$ uv run ruff check .
All checks passed!
$ uv run mypy --strict src/harness
Success: no issues found in 14 source files        (mypy 2.3.1, the uv.lock pin)
```

Both skips are designed: the `gradio` guard carried from Phase 1, and
`test_no_env_access.py`'s "scripts/ now exists — covered by the general collector" skip, which
turned itself off the moment `scripts/` appeared.

Phase 1's gate was 404 passed; the 82 new tests are:

| File | Tests | What it pins |
|---|---|---|
| `tests/unit/test_guardrails.py` | 21 | Verify step 3's three named tests; the matcher fails closed on a missing fact; `<default>` names the failing clause; booleans are not numbers; first match wins; `load_policy` rejects malformed files; the shipped policy is the amended text |
| `tests/unit/test_gateway_github.py` | 21 | every Appendix B.2 row over `respx`; `rerun_failed_jobs`' Appendix C rules; the write-call cache; unimplemented tools answer a `ToolError`, never raise |
| `tests/unit/test_orchestrator_suspend.py` | 5 | `StageSpec.suspend` in isolation: status, artifact kept, escalation record, unknown reason coerced, not consulted on a failed stage |
| `tests/integration/test_remediation_stage.py` | 10 | the cap denies and the trace says why (both new fixtures); the allow path with a readable history; second retry denied; cold start denied; a forbidden plan never reaches the gateway; calls derived from the drafts; `no_action`; a model failure at the Remediator keeps the diagnosis |
| `tests/integration/test_approvals_e2e.py` | 11 | Verify steps 2 and 4 over HTTP; approve re-evaluates and executes; `404`/`409`/`410`/`422`; escalations carry `run_id`; `readyz.policy_loaded`; live mode `501`/`403`; replay without a fixture `422` |
| `tests/unit/test_gateway_replay.py` | +0 (one rewritten) | the catalog is A.4's thirteen tools, shared with the live gateway |
| existing replay/e2e suites | 13 updated | every stub answers the Remediator; `real_regression`'s terminal state is `awaiting_approval`; three `agent.run` spans; token totals |

## Non-vacuity, where it was cheap to show

- The forged-allow gateway test is paired with `test_the_zero_http_property_is_not_vacuous`:
  with `forbidden=("rerun_failed_jobs",)` the run lookup never happens; with `forbidden=()` the
  same call goes out once. `merge_pull_request` alone could not show this — it has no live
  body and would reach the wire zero times even unguarded.
- `test_a_missing_fact_never_satisfies_a_clause` checks `ne` and `nin` as well as the
  threshold operators; a naive `facts.get(key)` implementation passes the threshold cases and
  fails these.
- `test_action_cap_fails_closed_when_the_count_is_not_supplied` was written after the engine,
  against the stated intent in `dispatch.md` decision 5, and would fail on the obvious
  `facts.get(key, 0)`.

## What the gate does not show

- The live model. Every test stubs it. `verify.md` records the two live replays that were
  run, including the one that exposed the `MAX_TOKENS` / hollow-plan defect the stubs could
  not.
- Verify step 5 (live GitHub). Blocked on environment; the gateway is pinned with mocked HTTP.
- Anything the `phase-reviewer` is about to find.
