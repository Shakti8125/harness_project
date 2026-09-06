# Phase 0 — handoff

**Status: DONE, tagged `phase-0-green`.** Gate PASS, audit SHIP, post-ship residue round PASS.
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
| `tests/**` | `test_layering.py` (66), `test_no_env_access.py` (33), `test_settings_construction_gate.py` (69), `test_healthz_status_codes.py` (3), `conftest.py`. **171 passing.** |

Verify block results: `verify.md`. Gate verdict: `test-verifier.md`. Audit + re-audit: `review.md`.
Per-agent build notes in the four other files here.

## Three PLAN.md amendments were made this phase

All three fix places where the plan contradicted itself. Phase 0 is the freeze point, so they were
made here rather than after agents had coded against them.

1. **A.11 `Diagnosis` now lists `reasoning` first.** PLAN.md:165-172 argues output quality improves
   when reasoning precedes the conclusion, and `to_gemini_schema()` derives `propertyOrdering` from
   field order — but the listing put `category` first, defeating it.
2. **A.11 `RemediationPlan` now lists `rationale` before `action`.** Same defect, in the other model
   marked "the Remediator's LLM output" — the one agent that proposes side-effecting actions.
3. **Phase 0's Verify block no longer pins `1 passed`.** The brief asks for two test modules plus a
   conftest, written as parametrized suites so a violation names the exact offending file.

**Decision recorded — `InvestigationNotes` was deliberately NOT reordered.** cicd-integration
identified it as a possible third instance and stopped rather than changing it. The call: the rule is
that the *reasoning* field precedes the *conclusion* field, and here the only conclusion-shaped field
(`additional_tool_calls`) already follows both prose fields — unlike the other two, nothing
action-shaped leads. Substantively, `Diagnosis.citations` are evidence supporting a stated
conclusion, whereas `InvestigationNotes.observations` *are* the primary output, so enumerating before
narrating is arguably right for a collection step. Reversible in two lines if a later phase disagrees.

## Environment

- `uv` is at `C:\Users\Shakti\.local\bin\uv.exe`, **not on the inherited PATH**. In Bash:
  `export PATH="/c/Users/Shakti/.local/bin:$PATH"`.
- **`jq` 1.8.2 installed** (winget) at `C:\Users\Shakti\AppData\Local\Microsoft\WinGet\Links`. New
  shells get it from PATH; older ones need that directory prepended. Phase 1's blocker is closed.
- A local placeholder-only `.env` exists (gitignored, verified untracked) so the app boots.
- `Settings` rejects unrecognised `HARNESS_`-prefixed env vars at boot — a typo crashes rather than
  being silently ignored, which is what PLAN.md:1770 always claimed. Caveat in its docstring: assumes
  the platform injects no non-config `HARNESS_`-prefixed vars (K8s `enableServiceLinks` would).

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
> 5. **`src/api/deps.py` must call `get_settings()`, never `Settings()`.** A raw `ValidationError`
>    from `Settings()` prints secret values verbatim into the boot log. This is now enforced by
>    `tests/unit/test_settings_construction_gate.py`, so a bypass fails the build rather than shipping
>    — but know *why* the gate exists before working around it. Also: never add a `field_validator` to
>    a `SecretStr` field whose `ValueError` message interpolates the value.
> 6. **Thread `settings.log_char_budget` into `ContextBudget`.** `120_000` has two homes, both
>    plan-mandated. A bare `ContextBudget()` makes `HARNESS_LOG_CHAR_BUDGET` a silent no-op.
> 7. **`calibrate()`'s four-step contract is the spec — implement it literally.** Especially step 4's
>    single clamp: `0.95 / +0.05 / -0.15 -> 0.85`, and `0.84` is wrong. Two unit tests fall straight
>    out of the docstring; write both.
> 8. **cicd-integration must register `{"empty_diff_contradiction": -0.10}` in `ConfidenceModel.deltas`
>    at the composition root.** That row is deliberately not in the harness. Forgetting now produces a
>    visible zero-delta row carrying `UNREGISTERED_SIGNAL_REASON` rather than nothing — detectable,
>    but not harmless.
> 9. **`real_regression`'s log is 159 lines, not several thousand.** `fixtures/README.md:133`
>    describes the padded end state in the present tense. The padding job — several thousand lines
>    with the `assert 91 == 90` anchor buried well before the end, timestamps spanning the real
>    `14:03:59-14:05:40` job window — is recorded in `fixtures-eval.md:135-153` and still outstanding.
>    Separate from the 50,000-line synthetic log `test_error_lines_never_trimmed` needs.
> 10. **Record, don't discover:** A.9 writes `@asynccontextmanager def span(...)`; it must become
>     `async def` when implemented. That is a deviation from Appendix A and belongs in the Phase 1
>     handoff note, not in a Phase 5 surprise.

## Later-phase notes (written down now so they survive the gap)

**Phase 2:**
- Close the `calibrate()` numeric residual in **data, not in the harness**: cicd-integration's fact
  builder emits `diagnosis.unregistered_adjustments: int`, and `policy.yaml` adds
  `diagnosis.unregistered_adjustments: {eq: 0}` to the `when` block of `retry-suspected-flaky`,
  `open-fix-pr` and `file-ticket` (leave `read-only-always` alone). No `guardrails.py` change —
  putting "diagnosis carries adjustments" into the generic engine is the disease, not the cure.
- **`ToolGateway.invoke`'s four-step re-check contract is written** (`src/harness/gateway.py`) — the
  concrete gateway must implement it: hold its own copy of `PolicySpec.forbidden` (wired at the
  composition root, since the Protocol signature does not supply it), refuse a forbidden tool even
  when `decision.effect == "allow"`, return rather than raise, and run the check **before client
  construction** so the forged-allow test's zero-outbound-request assertion holds.
- Open contract question harness-core deliberately did not legislate: what a gateway does when
  reached with `effect="deny"` / `"require_approval"` for a *non-forbidden* tool. Currently the
  caller's invariant. If Phase 2 wants gateway-side enforcement, that is a PLAN.md change, not a
  local decision inside one gateway.
- B.2 rows that are easy to skip and must not be: 404 on a read tool returns
  `ToolError(kind="not_found")` **as data to the agent**, not a run failure; 409 branch-exists and 422
  PR-already-exists are treated as **success** returning the existing resource.

**Phase 3:** `retries_for_signature_24h` defaults to **999** when memory is unavailable
(PLAN.md:1598), so the flaky-retry rule fails closed. No representation in code today and not implied
by any existing signature.

**Phase 4:** `real_regression`'s `min_confidence: 0.85` and `effect: require_approval` are no longer
independent assertions — both now read the same quantity through `open-fix-pr`'s `gte: 0.85`. If Open
Risk 4's recalibration moves that policy threshold, `scenario.yaml` must move in lockstep or the eval
becomes stricter than the policy it checks.

**Phase 6 adapter review:** `Evidence.source`'s `Literal` may need widening beyond `"diff"`, and
`Observation.commit_sha` may need renaming to something like `subject_version`. Both are Appendix A
verbatim today and must not be changed before then.

## Known-open, deliberately not closed in Phase 0

| Item | Why deferred |
|---|---|
| `calibrate()`'s unregistered-signal hole is fail-*visible*, not fail-*safe* | No `PolicyEngine` exists before Phase 2, so it cannot cause an unauthorised action yet. Close it as policy data (see Phase 2 above). |
| `calibrate()` and `ToolGateway.invoke` contracts are unenforced by tests | Both bodies are `NotImplementedError` / Protocol-only until Phase 1–2. The contracts are docstrings today; the first commit implementing each owes the tests. |
| `/healthz` "degraded" path string-matches an exception message | No fault-injection harness until Phase 3. The status-code behaviour itself is now pinned by `tests/integration/test_healthz_status_codes.py`. |
| `scripts/scrub_fixtures.py --check` never run | `scripts/` does not exist until Phase 1. Fixture secret-scanning is a manual grep so far. |
