# Phase 0 — handoff

**Status: DONE and tagged `phase-0-green` (commit `a241c01`).** Gate PASS, audit SHIP.
Next action: `/phase-run 1`.

## What exists now

Scaffold plus the contract freeze — PLAN.md Appendix A.1–A.11 is on disk as real Pydantic models
and Protocols with `NotImplementedError` bodies. That freeze is what makes Phases 1–6 safe to run
four agents at a time.

| Territory | State |
|---|---|
| `src/harness/**` | 13 modules, 42 models, 5 Protocols. **Zero drift from A.1–A.10** (audited twice). `mypy --strict` clean. |
| `src/integrations/cicd/**` | 17 A.11 schemas; `policy.yaml` byte-equivalent to PLAN.md:404-449 (Phase 2 content, loader not written). Phase 1+ modules are docstring-only placeholders. |
| `src/api/**`, `settings.py`, packaging | uv env (Python 3.12.14, 56 pkgs), Appendix E `Settings`, Dockerfile/compose/fly, `/healthz` + `/readyz`. |
| `fixtures/**` | Format spec in `fixtures/README.md`; `real_regression` skeleton, id-consistent, secret-free. |
| `tests/**` | `test_layering.py` (66 cases), `tests/unit/test_no_env_access.py` (33), `conftest.py`. **99 passing.** |

Verify block results: `docs/progress/phase-0/verify.md`. Gate verdict: `test-verifier.md`.
Full audit + re-audit: `review.md`. Per-agent build notes in the four other files here.

## Two PLAN.md amendments were made this phase

1. **Appendix A.11 `Diagnosis` now lists `reasoning` first.** Its own lines 165–172 argue that output
   quality improves when reasoning precedes the conclusion, and `to_gemini_schema()` derives
   `propertyOrdering` from field order — but the listing put `category` first, defeating it. Code and
   plan now agree.
2. **Phase 0's Verify block no longer pins `1 passed`.** The brief asks for two test modules plus a
   conftest, written as parametrized suites so a violation names the exact offending file. The
   substantive assertion ("test_layering passes") holds; the count was superseded.

## Environment gotchas

- `uv` is at `C:\Users\Shakti\.local\bin\uv.exe` and is **not on the inherited PATH**. In Bash:
  `export PATH="/c/Users/Shakti/.local/bin:$PATH"`.
- **`jq` is not installed.** PLAN.md's Phase 1 Verify block uses it in four of five steps. Install it
  first or those steps are BLOCKED, not passed.
- A local placeholder-only `.env` exists (gitignored, verified untracked) so the app boots.
- `Settings` now rejects unrecognised `HARNESS_`-prefixed env vars at boot — a typo crashes rather
  than being silently ignored, which is what PLAN.md:1770 always claimed.

## Phase 1 carry-forward — paste into the dispatch

> **Sole-owner assignments. The other three agents must not invent these.**
>
> 1. **`src/harness/agent.py` — owner: harness-core, exclusively.** PLAN.md:306 assigns it to Phase 1;
>    Appendix A specifies no `Agent`, `LLMAgent` or any agent protocol anywhere. **api-surface,
>    cicd-integration and fixtures-eval: do not define an agent protocol, base class or `run()`
>    signature. If you need one, block and ask.** Two conflicting `Agent` protocols in one parallel
>    wave is the single most likely way to repeat Phase 0's `Adjustment` break.
> 2. **The three missing constructors — owner: harness-core, exclusively.** `ContextManager`,
>    `TraceRecorder` and `MemoryStore` have no `__init__` specified in Appendix A.
> 3. **The trace read path — decide the owner before dispatch, not during.** A.12 requires
>    `GET /v1/runs/{run_id}/trace -> TraceResponse` and nothing in the tree returns a `TraceResponse`.
>    Name it as `TraceRecorder`'s or `MemoryStore`'s, in writing, in the brief. Otherwise api-surface
>    and harness-core will each assume the other built it.
> 4. **`get_job_logs`'s replay contract — quote it into cicd-integration's brief.** It lives only in
>    `fixtures/README.md`, which is not in PLAN.md and which the agent writing `gateway_replay.py` has
>    no reason to open. The load-bearing sentence: *`ReplayToolGateway.get_job_logs(job_id, max_bytes)`
>    reads `logs/job_<id>.txt` directly, **keeps the LAST `max_bytes`** (`content[-max_bytes:]`, never
>    `f.read(max_bytes)`), and treats a missing file as the tool-error case.*
> 5. **`fly.toml`'s health check must move off `/healthz`, or `/healthz` must 503 on `db: "error"`.**
>    Phase 1 is when `fly deploy` happens (PLAN.md:372). Today a machine whose volume failed to mount
>    returns HTTP 200 and stays in rotation. `/readyz` cannot be the target until `policy_loaded`
>    stops being hardcoded `False` in Phase 2 — so pick the 503 option now or accept a broken machine
>    in rotation for one phase, deliberately.
> 6. **`Settings` may only be constructed via `get_settings()`.** `src/api/deps.py` must call
>    `get_settings()`, never `Settings()`. A raw `ValidationError` from `Settings()` prints secret
>    values verbatim into the boot log. Never write `except ValidationError as e: log(e)`. Never add a
>    `field_validator` to a `SecretStr` field whose `ValueError` message interpolates the value.
>    `test_no_secret_leak.py` is a merge gate before the repo goes public.
> 7. **Thread `settings.log_char_budget` into `ContextBudget`.** `120_000` has two homes, both
>    plan-mandated. A bare `ContextBudget()` makes `HARNESS_LOG_CHAR_BUDGET` a silent no-op.
> 8. **`calibrate()`'s four-step contract is the spec — implement it literally.** Especially step 4's
>    single clamp: `0.95 / +0.05 / -0.15 -> 0.85`, and `0.84` is wrong. Two unit tests fall straight
>    out of the docstring; write both.
> 9. **cicd-integration must register `{"empty_diff_contradiction": -0.10}` in `ConfidenceModel.deltas`
>    at the composition root.** That row is deliberately not in the harness. Forgetting now produces a
>    visible zero-delta row carrying `UNREGISTERED_SIGNAL_REASON` rather than nothing — detectable,
>    but not harmless.
> 10. **`real_regression`'s log is 159 lines, not several thousand.** `fixtures/README.md:133`
>     describes the padded end state in the present tense. The padding job — several thousand lines
>     with the `assert 91 == 90` anchor buried well before the end, timestamps spanning the real
>     `14:03:59-14:05:40` job window — is recorded in `fixtures-eval.md:135-153` and still outstanding.
>     Separate from the 50,000-line synthetic log `test_error_lines_never_trimmed` needs.
> 11. **`jq` is not installed on the build machine.** Install before the Phase 1 gate.
> 12. **Record, don't discover:** A.9 writes `@asynccontextmanager def span(...)`; it must become
>     `async def` when implemented. That is a deviation from Appendix A and belongs in the Phase 1
>     handoff note, not in a Phase 5 surprise.

## Later-phase notes (write them down now so they survive the gap)

**Phase 2:**
- Close the `calibrate()` numeric residual in **data, not in the harness**: cicd-integration's fact
  builder emits `diagnosis.unregistered_adjustments: int`, and `policy.yaml` adds
  `diagnosis.unregistered_adjustments: {eq: 0}` to the `when` block of `retry-suspected-flaky`,
  `open-fix-pr` and `file-ticket` (leave `read-only-always` alone). No `guardrails.py` change —
  putting "diagnosis carries adjustments" into the generic engine is the disease, not the cure.
- `ToolGateway.invoke()` **must re-check the tool name against `forbidden` itself** and must not trust
  the `PolicyDecision` handed to it (PLAN.md:454-459). The gateway is authoritative. The Protocol
  carries the argument but currently states no re-check obligation — open finding, owner harness-core.
- `RemediationPlan` still lists `action` before `rationale` — the same defect just fixed in
  `Diagnosis`, in the other model that goes through `propertyOrdering`. Fix at the freeze point, not
  after Phase 2 codes against it. Owner: cicd-integration + a PLAN.md amendment.
- B.2 rows that are easy to skip and must not be: 404 on a read tool returns
  `ToolError(kind="not_found")` **as data to the agent**, not a run failure; 409 branch-exists and 422
  PR-already-exists are treated as **success** returning the existing resource.

**Phase 3:** `retries_for_signature_24h` defaults to **999** when memory is unavailable
(PLAN.md:1598), so the flaky-retry rule fails closed. No representation in code today and not implied
by any existing signature.

**Phase 6 adapter review:** `Evidence.source`'s `Literal` may need widening beyond `"diff"`, and
`Observation.commit_sha` may need renaming to something like `subject_version`. Both are Appendix A
verbatim today and must not be changed before then.

## Known-open, deliberately not fixed in Phase 0

| Item | Why deferred |
|---|---|
| `/healthz` returns 200 when `db == "error"`, so a DB-less machine stays in rotation | Fix belongs with Phase 1's first `fly deploy`; `/readyz` can't be the check target until Phase 2 |
| `calibrate()`'s unregistered-signal hole is fail-*visible*, not fail-*safe* | No `PolicyEngine` exists before Phase 2, so it cannot cause an unauthorised action yet |
| Nothing enforces that `Settings` is built only via `get_settings()` | One-test grep gate; owner api-surface (barrier) / test-verifier (gate) |
| `/healthz` "degraded" path string-matches an exception message | No fault-injection harness until Phase 3 |
