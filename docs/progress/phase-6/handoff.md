# Handoff — start of Phase 6 (Second Tool Gateway adapter)

Written 2026-09-14 on `master` at the Phase 5 fix round (`9e3b4df`) plus the records
commit. Phase 5's build is closed: gate green (892 passed, 2 skipped), one independent
audit with nine findings — all nine closed in a same-day fix round, the fix round
coordinator-verified per the standing one-audit-per-phase rule
(`docs/progress/phase-5/backlog.md`, "Audit provenance"). **Phase 5 is not tagged**: its
Verify block's live steps have not run (§1), and the tag goes on the tree that passes them.

**Read this file first and in full.** It is the index. It does not restate `PLAN.md` or the
Phase 5 documents — it tells you which parts are load-bearing for Phase 6 and records what
is true about this tree but written down nowhere else.

---

## 1. What is unfinished — and gated

Phase 5's live Verify steps, in order of what they need:

| Step | Needs | Calls | Who |
|---|---|---|---|
| 1 literal (`POST /v1/replay/real_regression` on the real model, open `/runs/<id>/view`, the `jq` components line) | quota | 3 | this session, after counting the day |
| 3 signed delivery (`scripts/replay.py --post-signed fixtures/scenarios/flaky_test/webhook.json --wait` on the real model) | quota | 3 | same |
| 4 redelivery + the two `sqlite3` counts on `./data/harness.db` | step 3's run | 0 | same |
| the Phase 4 eval's queued rows (`eval.py --runs 1 --llm gemini --scenario cold_start --scenario dependency_break`; update `phase-4/verify.md` §4 and the README table to five scenarios) | quota | 6 | same |
| 5 end to end (`scripts/seed_demo_repo.sh <you>/harness-demo-repo`, a fine-grained PAT, the Space's secrets/variables, `git push space master:main`, the webhook, `gh workflow run flaky.yml`, Redeliver, `curl …/v1/runs \| jq`) | four user-only actions | 3 | **the user; ask first** |

Twelve calls fit one Pacific day (~20). Count the day's `llm.attempt` spans with
`tokens.total > 0` on that clock first (2026-09-14's day was spent before the build
started). Record each step in `docs/progress/phase-5/verify.md` beside its in-process
twin, then tag `phase-5-green` on the recording commit. Step 5's four parts are each a
change to exposure or a spend of the user's credentials; the standing rule is "ask before
changing exposure or deploying", and it applies to every one of them separately.

Nothing in Phase 6 depends on those steps having run. Phase 6 can start on this tree.

---

## 2. Deploy state

The Space at <https://shakti-agent-harness.hf.space> serves **`phase-4-green` (`5abe6ad`)**,
pushed 2026-09-14 13:16 IST. Nothing from Phase 5 is deployed: no trace view, no webhook
route, no write tools, no log redaction. When it is redeployed (step 5's fourth part,
user's call), the notes that matter:

- **`HARNESS_GITHUB_WEBHOOK_SECRET` must be the value GitHub was given.** A placeholder
  is honoured (backlog); blank refuses everything with one warning at first delivery.
- **Live mode is two variables**: `HARNESS_GATEWAY=github` and `HARNESS_ALLOWED_REPOS`
  (a JSON list). Without both, `POST /webhooks/github` is replay-mode and accepts only a
  delivery whose Appendix C key matches a recorded scenario (`403` otherwise). Writes
  stay dry-run under `HARNESS_DRY_RUN=true`, which is the default and what step 5 expects.
- **The build answers `/readyz` about 2–3 minutes after the push**; poll for something
  only the new build can do (`GET /runs/<any>/view` is `404 problem+json` on the new
  image and `404 text/html` on the old one), not for `200`.
- Unchanged: no persistent volume, migrations by hand-call from `app.py:main()`, no
  authentication on the read routes, `zero-a10g`, `docker-compose.yml` forwards
  `HARNESS_FAULT_INJECT` from the shell.

---

## 3. What Phase 6 is

`PLAN.md` §"Phase 6 — Second Tool Gateway adapter (stretch)": *make the separation claim
falsifiable rather than rhetorical.* Built: `tests/contract/test_tool_gateway_contract.py`
(one abstract conformance suite parametrized over **every** gateway), `src/integrations/
incident/` (`schemas.py`, `policy.yaml`, `gateway_incident.py` against a fake
Sentry/PagerDuty with a real, complete `catalog()` and `NotImplementedError` bodies), and
`docs/ADAPTER_GUIDE.md`. The claim: **adding the second domain touches zero lines under
`src/harness/`** — `git diff --stat <phase5-tag>..HEAD -- src/harness/` is empty. Read
that section, A.4 (the `invoke` contract's four steps are the conformance suite's spine),
A.7 (what a `PolicySpec` is), and the Phase 5 amendment items 1–16.

`tests/contract/` exists and holds only `__init__.py`.

---

## 4. Where the tree stands

`5abe6ad..HEAD` on `master`:

| Commit | What |
|---|---|
| `66e2448` | the build: stage/gateway/memory/context spans, the trace view, `POST /webhooks/github`, the five write tools in both gateways, `evidence_unverifiable`, the secret pattern set, `record_fixture.py`, `scrub_fixtures.py`, `seed_demo_repo.sh`, `replay.py --post-signed`, the leak test, 10 new test files |
| `8f9e0de`, `ece361f`, `deef51e` | `.gitattributes` (`*.sh` LF), the Verify record and gate verdict, PLAN.md items 1–10, README; the seed script's dispatch retry |
| `9e3b4df` | the audit's fix round: findings 1–9, Appendix D's chain, the log record factory, the execution-input guard, PLAN.md items 11–16, `review.md` |
| (next) | `backlog.md`, the fix-round records, this handoff |

Gate on HEAD: `ruff` clean; `mypy --strict src/harness` clean (17 files); `mypy src` 8
errors, all pre-existing from Phase 2; **892 passed, 2 skipped**. `uv.lock` and
`pyproject.toml` are unchanged since `862e8e0`: `jinja2` has been a declared dependency
since Phase 0's scaffold, so the trace view added no pin.

Documents: `docs/progress/phase-5/{dispatch,verify,test-verifier,review,backlog}.md`.

---

## 5. What Phase 5 built that Phase 6 depends on

- **Two complete `ToolGateway` implementations to conform against.** `GitHubToolGateway`
  (`src/integrations/cicd/gateway_github.py`) and `ReplayToolGateway` (`gateway_replay.py`)
  both implement every catalog tool, reads and writes, with the same result shapes; both
  take `forbidden: tuple[str, ...]` explicitly (no default — `test_forbidden_has_no_default…`)
  and a `recorder: TraceRecorder | None`. The conformance suite's "forbidden tools rejected
  regardless of the decision" and "the refusal costs zero requests" already exist as
  per-gateway tests (`tests/unit/test_gateway_replay.py::test_forbidden_*`,
  `test_gateway_github.py`); Phase 6 lifts them into one parametrized suite rather than
  writing a third copy.
- **The gateway span is opened before the forbidden re-check, in both gateways**
  (amendment 16). A refusal leaves a `gateway.invoke` span with `error_kind=
  forbidden_by_policy`; `tests/unit/test_gateway_replay_writes.py` pins it. The
  conformance suite should expect a span on a refusal, not the absence of one.
- **The per-run write cache is keyed by `idempotency_key`** and lives on the gateway
  instance (one gateway per run, one per approval decision). `cached=True` is also the
  answer for "already existed" on every write (branch, PR, issue). "Idempotency keys
  honoured on write tools" in the contract suite means: the same key twice → the second
  is `cached=True` and reaches no transport. **Neither gateway refuses a keyless write**
  — it runs and is simply not cached (`gateway_github.py:903/949`,
  `gateway_replay.py:282/297`), although A.4 says the key is "required when
  `side_effect != "read"`". The contract suite is where that gap gets decided: either
  both gateways refuse a keyless write as `invalid_args`, or A.4's comment is amended to
  "cached when present". Decide once, in the dispatch record, before writing the test.
- **`catalog.py` is the shape a domain's catalog takes**: `_spec(name, description,
  properties, required, side_effect)`, `READ_TOOLS`, `IMPLEMENTED_WRITE_TOOLS`,
  `side_effect_of`, `not_implemented_message`. `PolicyEngine` keys `"read:*"` on
  `side_effect`, and the per-run action cap counts it.
- **`execute_plan` refuses any call whose arguments carry the redaction placeholder**
  (amendment 11) before the gateway sees it. An incident gateway gets the same guard for
  free if its remediation path goes through `execute_plan`; if it does not, it must own
  the same rule.
- **`install_log_redaction`** is process-wide and installed by `AppContext.__post_init__`.
  A second integration's loggers are covered without doing anything.
- **`harness.observability.carries_redaction`** and the two-tier `Redactor` are the
  harness's; the *patterns* stay in `src/api/deps.py` (`SECRET_PATTERNS`,
  `HEURISTIC_SECRET_PATTERNS`, `build_redactor`). A domain that has its own credential
  shapes adds them there, not in the harness.
- **`AppContext.gateway_for(RunContext)`** (`src/api/deps.py`) is the one place a gateway
  is chosen: `mode` (`live` | `replay`), `repo`, `scenario_dir`. A second integration
  means a second `mode` or a second field, and `RunContext` is stored as opaque JSON on
  the approval row — additive changes only.

---

## 6. Design decisions Phase 5 made that Phase 6 will bump into

1. **Layering is enforced by `tests/test_layering.py` on a denylist, not a whitelist**:
   `github, workflow_run, pytest, pull_request, ci` may not appear under `src/harness/`
   in imports, string literals, comments or docstrings (the raw-source check). Phase 6
   adds no words to the list — an incident domain's words (`sentry`, `pagerduty`,
   `alert`, `deploy`) are not in it, so the layering test is *necessary, not sufficient*
   (the Phase 5 audit's S15 said the same); the `git diff --stat -- src/harness/` gate is
   the sufficient one.
2. **`ToolError.kind` is closed** (nine members). The Phase 5 build wanted a `conflict`
   for the `409` and did not add one (`unknown` + `http_status=409`); the fix round wanted
   an `altered_at_rest` and used `invalid_args`. An incident gateway's failure matrix maps
   onto the nine or records why it cannot.
3. **`EscalationRecord.reason` is closed and drift-guarded**
   (`tests/unit/test_escalation_reason_drift_guard.py` pins the contracts Literal, the
   orchestrator alias and the gate's choice). `evidence_unverifiable` and `run_timeout`
   are in A.1's block now.
4. **Fixture slugs are a pure function of `(repo, tool, args)`** —
   `gateway_replay.fixture_slug_for`, shared with `record_fixture.py`. The workflow-runs
   listing's slug carries `-branch-<name>`. A second replayable domain either reuses the
   rule or owns its own; the README's "one path, two queries → a suffix" rule is the
   precedent.
5. **Appendix D's chain is in the Investigator, not the gateway** (amendment 12). A
   cold-start scenario needs a `get_commit` fixture; `cold_start`'s is a history root.
   The eval scores `baseline_kind` and `cold_start` as labels.
6. **The webhook route logs a verdict `code`, never a header value** (amendment 15); the
   `reason` string that quotes the delivery goes only into a `400` problem document. A
   second inbound webhook (Sentry's, PagerDuty's) follows the same split.

---

## 7. Residuals worth knowing (full text in `docs/progress/phase-5/backlog.md`)

- The live Verify steps and the eval's two rows are pending quota (§1); step 5 is
  user-gated; `phase-5-green` is not tagged.
- A placeholder webhook secret is honoured; the heuristic tier does not look through
  base64 (by design); the log factory does not scrub `exc_info` tracebacks.
- `execute_plan`'s refusal of an altered plan is `invalid_args` → `tool_failure`, with the
  calls before it already run.
- Recorded scenarios carry the gateway's post-filter runs listing and a tail-capped log,
  and say so.
- `create_issue` can duplicate when a person removed one of its labels; a PR's labels are
  met on the retry, not the failing attempt.
- Phase 3 finding 10 (the check-then-act cap) is open; `annotate_run` is recorded, never
  executed.

## 8. How to work on this codebase

Unchanged from the Phase 5 handoff §8 (inline by the coordinator; one independent audit;
fix rounds coordinator-verified and recorded as such; count live calls on the Pacific
day; one uvicorn per fault value; kill the process on the port), plus what this phase
learned:

- **Bash heredocs mangle backslashes as well as backticks here.** A `\n` inside a
  quoted heredoc reached Python as a real newline and split a string literal. Write every
  patch script to the scratchpad with the Write tool and run it with `python <file>`; use
  heredocs only for plain prose with no escapes.
- **Prove fix-round tests by ordering**: write them, run them on the unfixed tree, record
  the failures (`test-verifier.md`, "Ordering"), then fix. A test that only fails at
  import (`ImportError` on a new symbol) is weaker proof; say so.
- **A subagent dispatch can die on the session rate limit** with nothing written. The
  audit's first dispatch did (reset at 4:50 pm IST); re-dispatch with the same brief
  after the reset. Check `review.md` exists before assuming it does.
- The `phase-reviewer` audit cost ~303 k tokens this phase (sixteen suspicions, two
  HIGH findings reproduced by execution); worth it, once.

## 9. Constraints that outlive Phase 5

Unchanged: `git add` new files explicitly; a mounted sub-app gets no lifespan events;
`app.py` must not read `os.environ` — and neither may a *docstring* quote it
(`tests/unit/test_no_env_access.py` greps raw source); the Space is pinned to `zero-a10g`;
never print the HF token; `uv.lock` is the pin of record; ask before changing exposure or
deploying; never quote a stub-mode eval number as accuracy. New: **`main` of the demo
repository is never force-pushed** — `seed_demo_repo.sh --force` re-seeds on top of it
and `tests/unit/test_seed_script_invariants.py` reads the script to keep it that way.

## 10. Definition of done for Phase 6

`PLAN.md`'s Phase 6 Verify block: the contract suite green ×3 (`GitHubToolGateway`,
`ReplayToolGateway`, `IncidentToolGateway`), `git diff --stat <phase5-tag>..HEAD --
src/harness/` empty, `tests/test_layering.py` green with two integrations present. Since
`phase-5-green` is not yet tagged, diff against `9e3b4df` (the last commit that touched
`src/harness/`) until it is. A phase is done when `test-verifier.md` says PASS and
`review.md` says SHIP (or, after a fix round, records the coordinator's SHIP with its
basis); tag `phase-6-green` on the tree that is actually finished.

## 11. Read order for a fresh session

1. This file.
2. `docs/progress/phase-5/backlog.md` ("Audit provenance", then the residuals) and
   `verify.md` (what ran, what is pending).
3. `PLAN.md` Phase 6, A.4, A.7, the Phase 5 amendment (items 1–16).
4. `docs/progress/phase-5/dispatch.md` — the decisions the Phase 5 build was made
   against, decision 2 amended in place.
5. `src/harness/gateway.py` (the `invoke` contract), `src/integrations/cicd/catalog.py`,
   `gateway_replay.py`, `gateway_github.py` (`invoke`, `_invoke_write`, the write cache),
   `src/api/deps.py` (`RunContext`, `gateway_for`, `build_redactor`),
   `tests/unit/test_gateway_replay.py` and `test_gateway_github.py` (what the conformance
   suite lifts).
6. `docs/progress/phase-5/review.md` only if a finding number comes up.
