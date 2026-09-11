# Phase 2 — Verify block results

Run 2026-09-11 against the container built from `020a64f` (`docker compose up -d --build`),
with a real `HARNESS_GEMINI_API_KEY`, `HARNESS_GATEWAY=replay`, `HARNESS_DRY_RUN=true`.
Steps 1, 2 and 4 spend real model calls (three per replay now that the pipeline has three
stages; more when Recovery retries — the first regression run spent seven). Steps 3 and 5
spend none.

| Step | Result |
|---|---|
| 1 — flaky scenario, the retry rule | ✅ PASS **as amended** — the cap denies, and the trace names the clause. The literal `"ok":true` expectation is unreachable in this phase by the plan's own amendment 3; the allow half is pinned offline (below). |
| 2 — regression blocked pending approval | ✅ PASS — literal |
| 3 — forbidden actions unreachable from every state | ✅ PASS — 21 tests, including the three the plan names |
| 4 — approval round-trip | ✅ PASS — literal, including `409` with `state: rejected` |
| 5 — first live GitHub call | ⛔ BLOCKED on environment — no real token, no demo repository. Code path built and pinned with mocked HTTP; see below. |

Gate at `020a64f`: `uv run pytest -q` → **486 passed, 2 skipped**; `ruff check .` clean;
`mypy --strict src/harness` clean on 2.3.1.

---

## Step 1 — flaky scenario  ✅ (as amended)

```
$ curl -s -X POST localhost:8000/v1/replay/flaky_test | jq '{...}'
http 200 in 39.9s
{
  "status": "escalated",
  "reason": "policy_denied",
  "message": "policy denied 'rerun_failed_jobs' via <default>: no rule matched, default_effect is 'deny': 'retry-suspected-flaky' did not match (memory.retries_for_signature_24h: 999 fails {lt: 2.0})",
  "cat": "flaky_test",
  "conf": 0.85,
  "suggested": "retry",
  "action": "retry_job",
  "rule": "<default>",
  "effect": "deny",
  "tool": "rerun_failed_jobs",
  "executed": 0,
  "rem_status": "denied",
  "degraded": []
}
```

The literal EXPECT was `{"action":"retry_job","rule":"retry-suspected-flaky","tool":"rerun_failed_jobs","ok":true}`.
`action` matches; the other three cannot in this phase, and the plan's amendment 3 says why:
the retry cap reads `memory.retries_for_signature_24h`, which is the fail-closed `999` until the
memory store exists, so `retry-suspected-flaky` cannot match and `default_effect: deny` answers.
**Chosen (dispatch.md decision 2): record the deny, and make the deny legible.** The
`<default>` decision's reason quotes the rule and the clause that failed, so the escalation
message above reads as a sentence rather than as an unmatched rule id. The live model, unstubbed,
diagnosed `flaky_test` at 0.85 with three citations and suggested `retry`; the Remediator chose
`retry_job`; the policy refused it for the stated reason.

**The allow half, offline** (`tests/integration/test_remediation_stage.py::test_retry_executes_through_the_gateway_when_the_history_is_readable`):
the same scenario with `PriorHistory(unavailable=False, retries_in_24h=0)` yields
`rule_id="retry-suspected-flaky"`, `effect="allow"`, obligations
`["record_observation", "annotate_run"]`, and `executed[0] == {tool: rerun_failed_jobs, ok: true, dry_run: true}`
through the real replay gateway. The sibling tests pin the cap biting at 2 and the cold-start
denial. Phase 3 makes the live demonstration possible by putting a real count in the bundle.

`infra_timeout` behaves identically (`infra_transient` → `retry_job` → denied), pinned by the
same parametrised test; its diff is empty by design.

## Step 2 — regression blocked pending approval  ✅

```
$ curl -s -X POST localhost:8000/v1/replay/real_regression | jq '{status, effect, executed, approval}'
http 200 in 51.0s
{
  "status": "awaiting_approval",
  "effect": "require_approval",
  "executed": 0,
  "approval": "apr_a452ec85163ef1d4"
}
```

EXPECT `{"status":"awaiting_approval","effect":"require_approval","executed":0,"approval":"apr_…"}` — met.

Beyond the literal: `category: real_regression` at 0.95, `suspected_commit_sha` the fixture's
commit, plan `open_fix_pr` with the three canonical calls `create_branch`,
`create_or_update_file`, `open_pull_request` — every one decided `open-fix-pr` /
`require_approval` — on branch `agent/fix/e2cdf1b4`, and the drafted file is the exact
one-line fix:

```python
def discount(price: int, percent: int) -> int:
    """
    Apply a percent discount to price, rounded down to the nearest integer.
    """
    return price - (price * percent) // 100
```

**The first attempt at this step did not look like that**, and the difference is recorded in
`dispatch.md` decision 7: the model hit `MAX_TOKENS` at the 4 096 default (thinking counts
against it), Recovery's "answer more briefly" nudge produced a plan of one `create_branch` with
empty args and no draft, and the decision was still `require_approval` — right answer, hollow
plan. Fixed in `fc8209b` (output budget 8 192; calls derived from the drafts); the output above is
the re-run.

## Step 3 — forbidden actions unreachable from every state  ✅

```
$ uv run pytest tests/unit/test_guardrails.py -q
.....................                                                    [100%]
21 passed in 0.48s
```

- `test_forbidden_denied_from_all_states`: 6 categories × 5 confidences × 3 verdicts = 90
  states, × 3 forbidden tools, every one `effect="deny"`, `rule_id="<forbidden>"`. The states
  are otherwise the most permissive possible (retries 0, no cold start, no action taken), so
  nothing but the forbidden set is doing the work.
- `test_gateway_refuses_forbidden_even_with_forged_allow_decision`: a hand-forged
  `PolicyDecision(effect="allow", rule_id="retry-suspected-flaky")` for `merge_pull_request`
  into `GitHubToolGateway(dry_run=False)` → `ToolError(kind="forbidden_by_policy",
  retryable=False)`, and `respx` recorded **zero** outbound calls. A second test proves the
  zero-HTTP property is not vacuous, using a tool that does call out.
- `test_max_one_side_effecting_action_per_run`: after one action, `rerun_failed_jobs` is denied
  with `rule_id="<invariant>"` even though the retry rule would allow it; reads are unaffected;
  an absent count fails closed; the cap is not expressible in YAML (`extra="forbid"`).

## Step 4 — approval round-trip  ✅

```
$ APR=apr_a452ec85163ef1d4
$ curl -s -X POST localhost:8000/v1/approvals/$APR -H 'content-type: application/json' -d '{"decision":"reject","actor":"shakti"}' | jq -c '{state, executed: (.executed|length)}'
{"state":"rejected","executed":0}

$ curl -s -X POST localhost:8000/v1/approvals/$APR -H 'content-type: application/json' -d '{"decision":"reject","actor":"shakti"}' -w '\nhttp %{http_code} %{content_type}\n'
{"type":"about:blank","title":"Approval already decided","status":409,"detail":"This approval is already rejected.","instance":"/v1/approvals/apr_a452ec85163ef1d4","run_id":"run_01M27JEHWDZTCQB8PZDC90TBMT","state":"rejected"}
http 409 application/problem+json

$ curl -s localhost:8000/v1/runs/run_01M27JEHWDZTCQB8PZDC90TBMT | jq -c '{status, rem: .final.remediation.status, apr_state: .final.remediation.pending_approval.state}'
{"status":"completed","rem":"denied","apr_state":"rejected"}
```

EXPECT `"rejected"`, then `409` with state `"rejected"` — met. The approve path, the `410` on
expiry, `404` on an unknown id and `422` on a malformed body are pinned in
`tests/integration/test_approvals_e2e.py` (approve re-evaluates policy, executes through the
gateway, and in this phase stops at `create_branch`'s honest `ToolError(kind="unknown")` —
the PR tools are registered, decided, and not yet implemented).

`GET /v1/escalations` listed the step-1 escalation with its `run_id` before the container was
rebuilt between steps; the registry is in-process (replaced in Phase 3), so it was empty
afterwards. Pinned by `test_escalations_lists_the_denied_retry_with_its_run_id`.

## Step 5 — first live GitHub call  ⛔ BLOCKED on environment

`HARNESS_GITHUB_TOKEN` is a local placeholder and `<you>/harness-demo-repo` does not exist
(`seed_demo_repo.sh` is Phase 5). Not run, and not claimed.

What is built and pinned without the network: `scripts/replay.py --live --repo --run-id`
fetches the workflow run, builds the subject, and drives the pipeline over
`GitHubToolGateway`; `POST /v1/runs` with `mode="live"` takes the same path behind
`HARNESS_GATEWAY=github` and the repo allowlist (`501`/`403` otherwise, pinned). The gateway
itself is pinned row by row against Appendix B.2 in `tests/unit/test_gateway_github.py`
(21 tests: timeouts, both rate limits, 401/403/404/5xx, malformed bodies, the log redirect
followed without the token, zip logs, the tail-of-20-MB cap, dry-run reading but never
posting, `rerun_failed_jobs`' two Appendix C idempotency rules, the write-call cache). Every
`gateway.invoke` span records `side_effect`, `ok` and `dry_run`, which is what step 5 asks to be
checked; `scripts/replay.py` prints exactly that summary.

To run it: a token with `actions:read` (and `actions:write` for the non-dry-run half) in
`HARNESS_GITHUB_TOKEN`, the repo in `HARNESS_ALLOWED_REPOS`, `HARNESS_GATEWAY=github`, then the
command in the plan. With `HARNESS_DRY_RUN=true` the write is reported as `dry_run=True` and
nothing is sent.
