# Phase 2 — audit

Independent `phase-reviewer` pass over `85a168b..17fd4b5`, run 2026-09-11, read-only, no
model quota spent. The reviewer's report is reproduced below with the disposition of each
item added by the coordinator. **The fix round that followed (`17fd4b5..HEAD`) was not
re-audited**: the user directed that the review not be restarted after the fixes, so the
round is coordinator-verified, and the backlog records it as such — the Phase 1 precedent
(`docs/progress/phase-1/backlog.md`, "Audit provenance").

## Verdict as returned: FIX FIRST

> Two medium findings, each a one-line fix; nothing else survives. 140 tests across the
> seven Phase 2 files pass; the findings below were confirmed by executing the inputs, not
> by reading alone.

**Verdict after the fix round: SHIP** — coordinator's call, on the basis below. Every
finding is closed with a test that reproduces the reviewer's confirming input; every
deviation is either reconciled into PLAN.md or accepted in writing.

## Findings

| # | Sev | Where | Defect (reviewer's words, condensed) | Disposition |
|---|---|---|---|---|
| 1 | medium | `gateway_github.py` log blob path | The anonymous blob-host client caught only `httpx.TimeoutException`; any other transport error escaped `invoke` as an exception (A.4: "invoke never raises for a remote failure") → generic 500, no `ToolError`, no degraded component, nothing in the trace. Confirmed with respx: `ConnectError` escaped. | **Closed.** `except httpx.HTTPError` → `_Failure("unknown", retryable=True)`, the classification the API-host path already applied. Pinned: `test_a_transport_error_on_the_log_blob_host_is_returned_not_raised`. |
| 2 | medium | `remediation.normalize_plan` | Model-proposed calls passed through unconstrained by the stated action when nothing could be derived — `no_action` + `[rerun_failed_jobs]` was judged and, under a readable history, would execute; `open_fix_pr` (no draft) + `[rerun_failed_jobs]` executed a retry under a PR action. Confirmed. | **Closed.** `TOOLS_FOR_ACTION` restricts pass-through calls to the action's tool set; `no_action` keeps none; dropped calls are returned and recorded on the span. One deliberate exception, added by the coordinator: a proposed call naming a *forbidden* tool is always carried into the judged plan so it is denied by name and the run escalates — dropping it would have made the loudest thing the model can do the quietest. Pinned: `test_calls_outside_the_action_are_dropped_and_recorded`, `test_a_forbidden_call_beside_a_derivable_action_still_denies_the_whole_plan`. |
| 3 | low | `main._settle_run` | A re-evaluation that *denies* on approve stored the run `completed` with no `EscalationRecord`, whereas the same result in-run escalates `policy_denied`. Unreachable this phase, latent for Phase 3. | **Closed.** `_escalation_after_approval` mirrors `wiring.remediation_suspend`: `tool_failure` on a failed execution, `policy_denied` on a denied re-evaluation, payload shape aligned (`action`, `executed`, plus `stage: "approval"` and `approval_id`). Not separately pinned — unreachable until facts can change; recorded in the backlog for Phase 3. |
| 4 | low | `gateway_github._request` | `float(reset)` unguarded: a non-numeric `x-ratelimit-reset` became `ToolError(kind="invalid_args", retryable=False)`. Confirmed. | **Closed.** Malformed reset → wait the cap, as the `retry-after` branch already did. Pinned: `test_a_malformed_rate_limit_reset_is_still_a_rate_limit`. |
| 5 | low | `remediator.py` span | Under a derivable action a hallucinated forbidden call was silently replaced; the `remediation.plan` span recorded only post-normalisation names. | **Closed**, twice over: the span now carries `proposed_tool_calls` and `dropped_tool_calls`, and (per finding 2's exception) a forbidden proposal is judged rather than dropped. |
| 6 | low | `test_approvals_e2e.py` | The "re-evaluates" assertion was satisfied by echoing the stored decisions; nothing distinguished re-evaluation from echo. Test vacuity only — the code did re-evaluate. | **Closed.** Every response decision's `evaluated_at` is asserted newer than the stored one's. |

## Cleared suspicions (as returned)

S1 forbidden-before-facts, missing facts fail every operator, `_same` refuses bool/number,
`default_effect` validation-enforced. S2 one plan per run; approval single-use under one
lock; the route's hardcoded `0` accurate. S5 check-and-write under one lock; concurrent
approves → one applies, one 409; decided-then-expired → 409. S6 the three `awaiting_approval`
conditions are equivalent. S7 every `_request` branch terminates; the anonymous client has no
auth header; no error message, span or log line carries the token (findings 1 and 4 aside).
S9 `content_b64` digested in both plan locations; both extra routes scrubbed; nothing
raw-content-shaped in a span. S10 the 90-case matrix would fail on a `<default>`
fall-through; respx intercepts the `base_url` client, proven non-vacuous. S11 both fixtures
internally consistent; `flaky_test` is trimmed and the anchors survive. S12 the amendments
describe what is built. S13 `skipped` widens only the two intended rules. S14 no Phase 1
regression.

## Contract deviations (as returned) and how each was reconciled

| Deviation | Reconciled |
|---|---|
| `guardrails.py` imports `yaml`; the harness allowlist omitted it | PLAN "Architectural rules" table now lists pyyaml (and google-genai, which `llm.py` already imported) |
| `rerun_failed_jobs` args `{run_id, attempt?}` vs A.4's `{run_id}` | A.4 row amended; `attempt` is what makes Appendix C's "already advanced" no-op possible |
| `PrDraft.branch` prompted as `agent/fix/{head_sha[:8]}` vs A.11's `signature_id[:8]` | A.11 comment amended: `head_sha` until signatures exist (Phase 3) |
| `Suspension.escalate_as` coerced rather than required | A.2 listing amended to say so |
| New modules not in the tree (`catalog.py`, `remediation.py`, `approval_registry.py`, `gen_fixture_log.py`); routes in `main.py` not `routes_approvals.py` | Repo layout amended; the `routes_*.py` split is deferred until a second file is needed |
| `denied` used for a human rejection | `RemediationResult.status` gains `rejected` (A.11 amended); the route sets it; a rejection is never an escalation |
| Approval-route escalation payload differed from the in-run shape; `channels=["log"]` hardcoded | Shape aligned (finding 3). `channels` stays `["log"]`: it is what the composition root passes the orchestrator too, and the outbound channels arrive with the observability phase |
| `NotImplementedError` → `ToolError`; "Orchestrator consults" → Remediator consults; step 1 against the deny; step 5 blocked | Already documented in PLAN's Phase 2 status block; accepted |

## Notes for the record (as returned), with responses

- *The rerun 403 "in progress" substring is PLAN's paraphrase; GitHub's real text is
  unverified offline.* → Matched on four phrasings now (`_ALREADY_RUNNING_PHRASES`), and the
  miss degrades loudly (`auth` → `tool_failure`), not silently. Verify against the real
  endpoint when step 5's environment exists.
- *`decide_approval` transitions before executing; an exception there would burn the
  approval.* → The two reachable causes (missing run, `final` without bundle/diagnosis) are
  now checked *before* the transition and answer `404`. Anything else is a programming error.
- *`scripts/replay.py --json` prints raw content; a printed `approval_id` is undecidable.* →
  Documented in the script's docstring; it is the operator's own terminal.
- *`"skipped"` defaulted in two places.* → `build_facts` no longer defaults
  `evaluation_verdict`; both callers pass it explicitly with a Phase 4 note.
- *`tests/test_no_secret_leak.py` does not exist yet.* → Phase 5, per PLAN; unchanged.
