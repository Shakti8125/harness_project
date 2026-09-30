---
title: Agent Harness
emoji: 🔍
colorFrom: indigo
colorTo: gray
sdk: gradio
sdk_version: 6.26.0
python_version: '3.12'
app_file: app.py
pinned: false
---

# Agent Harness

A domain-agnostic multi-agent control plane, with CI/CD failure triage as its first
integration. A failed workflow run goes in; a validated, evidence-cited `Diagnosis` comes
out, with every claim traceable to the log line or diff hunk it rests on.

The point of the architecture is the seam: `src/harness/**` knows nothing about CI, tests,
pull requests or any particular vendor — it is orchestration, context budgeting, tool
sandboxing, policy, memory, calibration and tracing. The domain lives entirely in
`src/integrations/cicd/**` and is injected at a composition root (`src/api/deps.py`). That
separation is enforced by a test that walks the AST of every harness module and fails the
build if a domain word appears.

**Status (2026-09-17):** complete through Phase 5 of `PLAN.md` -- `phase-5-green` is the
tagged tree and the one the Space serves. Six phases were planned; the sixth (a second
domain adapter, to make the separation claim falsifiable by diff) was not started. Every
phase carries its own record under `docs/progress/<phase>/`: the decisions it was built
against, the Verify block's literal expected-vs-actual, the gate verdict, an independent
audit, and what was left open.

## Try it

Live at <https://shakti-agent-harness.hf.space>. There is no CLI — the surface is HTTP.

```bash
BASE=https://shakti-agent-harness.hf.space

curl -s -X POST $BASE/v1/replay/real_regression | jq '.final.diagnosis'
```

That runs the whole pipeline synchronously and takes 30–90 seconds, because it makes three
real model calls (investigate, diagnose, remediate; the evaluate stage between the last two
makes none). The asynchronous form returns `202` immediately and runs in the background:

```bash
RID=$(curl -s -X POST $BASE/v1/runs -H 'content-type: application/json' -d '{
  "integration": "cicd",
  "subject": {"workflow_run": {"id": 1, "run_attempt": 1, "head_sha": "abc",
                               "head_branch": "main", "workflow_id": 1,
                               "name": "ci", "event": "push"},
              "repository": {"full_name": "demo/repo"}},
  "idempotency_key": "demo-0001",
  "mode": "replay",
  "replay_fixture": "real_regression"
}' | jq -r .run_id)

curl -s $BASE/v1/runs/$RID       | jq '{status, category: .final.diagnosis.category}'
curl -s $BASE/v1/runs/$RID/trace | jq '.spans | length'
```

`subject` is a GitHub `workflow_run` webhook body — the whole delivery or just the inner
object — and the scenario is named separately by `replay_fixture`. `mode` must be `"replay"`:
the live gateway arrives in a later phase, and asking for `"live"` returns a `501` that says
so. Every error is RFC 9457 `application/problem+json`.

> **Heads up on quota.** The free model tier allows **20 requests per day** and one replay
> costs three, so the demo can be exhausted by about six requests (the day resets at
> midnight Pacific). Nothing worse is exposed — `HARNESS_DRY_RUN=true` and
> `HARNESS_GATEWAY=replay` are the defaults, so no live repository is ever touched, and the
> one write the policy allows on its own (re-running a flaky job) is executed dry-run.

`real_regression` is a recorded scenario: an off-by-one in a `discount()` helper makes two
pricing tests fail. The system fetches the job list, downloads a 3,800-line job log,
resolves the last green run on the branch, diffs against it, budgets the log down to fit a
model's context *without ever dropping the error lines*, returns a diagnosis that blames the
right commit and quotes the patch line responsible, **checks every quote against the log and
the diff it came from**, and drafts a fix PR that waits for a person to approve it. The other
scenarios are `flaky_test` (a timing assertion; the harness re-runs the job itself, up to a
cap), `infra_timeout` (a registry outage under an empty commit), `dependency_break` (a
pydantic 1→2 bump that fails at import) and `cold_start` (no green baseline exists, so no
autonomous action is allowed).

Everything replays from `fixtures/scenarios/` — no live repository is touched, and
`HARNESS_DRY_RUN` defaults to `true`.

| Endpoint | What it does |
|---|---|
| `POST /v1/replay/{scenario}` | Run a recorded scenario end to end, synchronously |
| `POST /v1/runs` | Accept a run, execute it in the background (`202`); the same delivery twice is one run |
| `GET /v1/runs/{run_id}` | The run outcome: bundle, diagnosis, evaluation, remediation |
| `GET /v1/runs/{run_id}/trace` | Every span, with token usage and timings |
| `GET /runs/{run_id}/view` | The same run as one page: a waterfall of every span and a card per stage — confidence with each adjustment itemised, every citation beside its verify verdict, the policy rule quoted from `policy.yaml` |
| `POST /webhooks/github` | GitHub's `workflow_run` delivery: HMAC-verified over the raw bytes, filtered to completed failures, keyed so a redelivery is the same run (`202`, then background) |
| `POST /v1/approvals/{approval_id}` | Approve or reject a held plan; the policy is re-checked before anything runs |
| `GET /v1/escalations` | Every run that stopped to ask a person, and why |
| `GET /healthz` · `GET /readyz` | Liveness and readiness |

Open a run's `/view` after a replay: the trail from webhook to decision is one page — which
log lines were kept, what the model claimed, which claims the Evaluator verified, which rule
the policy matched, and what would have been written.

## How well it does

`scripts/eval.py` replays every scenario against its `scenario.yaml` label and exits non-zero
if any category is wrong or any forbidden action ever executed. Two numbers, and they mean
different things:

| Run | Scenarios | Category accuracy | Refuted citations | Forbidden actions executed |
|---|---|---|---|---|
| `--llm gemini` (the model, 2026-09-14 and -17) | all five, one pass each | **5/5** | 0 | 0 |
| `--llm stub` (the pipeline; canned diagnoses, real evidence checks) | all five, ×5 | 25/25 | 0 | 0 |

The model run is the accuracy claim; it is one pass per scenario, over two days, because
the free tier's daily quota is the binding constraint (`docs/progress/phase-4/verify.md`
§4 is the record; one run of `dependency_break` filed a ticket where the label expects a
held fix PR — right category, reported as a label miss, not gated). The stub run proves
the plumbing — the gate, the Evaluator over the fixtures' real logs and diffs, the policy,
the forbidden set — and says nothing about the model; its report is labelled `llm: stub` so
the two cannot be confused. Every live diagnosis so far has had all of its citations verified
by the Evaluator; the refuted-citation path is demonstrated by fault injection
(`HARNESS_FAULT_INJECT=diagnostician_fabricate_citation`), which escalates the run and skips
the Remediator.

## The part worth reading

**Context budgeting that cannot eat the error.** Logs are far larger than any context
window. Rather than embed-and-rank, the assembler marks *anchors* (regexes supplied by the
integration — the harness has no opinion about what an error looks like), keeps a ±20-line
window around each one as inviolable, fills what remains with the first 200 and last 400
lines, and reports precisely what it cut in a `TruncationReport`. An anchor either survives
verbatim or is counted in `anchors_dropped`; there is no third outcome where it silently
disappears. `test_error_lines_never_trimmed` pins that against a 50,000-line log with the
assertion buried at line 12,345.

**Confidence the model does not get to grade.** The model reports `self_confidence` against
a written rubric; the harness then applies deterministic adjustments in code — no citations,
cold start, degraded gateway, empty-diff contradiction — sums them, and clamps *once*. The
model never sees the adjusted number.

**Structured output as a contract, not a hope.** Pydantic models are translated into the
provider's constrained-decoding dialect, with reasoning fields ordered *first* so the model
argues before it concludes, and the response is still re-validated on the way back. When it
does not validate, the validation errors are fed back and the call is retried; when the
provider rate-limits, the retry backs off with jitter; when the answer is cut off, the output
budget grows. A malformed response is a retry, not a dead run.

**Citations are checked, not trusted.** Every citation the Diagnostician makes is a claim of a
declared kind — a quote exists in this log, this file is in the diff, this dependency moved,
this test failed, this commit is in range — and a deterministic checker verifies each one
against the artifact the harness actually collected. No second model is asked, because using
a model to check a model just moves the credulity. A refuted claim overrides confidence
entirely: the run escalates and nothing is acted on. A claim that cannot be checked because
the artifact is missing is *unverifiable*, never refuted — absence of evidence is not evidence
of fabrication — and it downgrades every action to needing approval instead.

**No secret reaches the trace, by three independent mechanisms.** Every credential in
`Settings` is a `SecretStr`, so an accidental `repr` prints stars; its resolved value is
registered with a `Redactor` that rewrites it literally in every span attribute, log line,
escalation payload, stored row and served body; and a regex set catches the shapes that never
passed through `Settings` at all — a token a careless workflow echoed into its own CI log.
`tests/test_no_secret_leak.py` plants sentinel values (one of them *in* a fixture log), runs
every scenario, a failing escalation webhook, a forged webhook signature and a GitHub error
body that echoes the token, and asserts zero occurrences across every span row, every
escalation row, stdout, stderr, the raw bytes of the SQLite file and every JSON and HTML body
served. It is a gate, and it fails when any one barrier is removed. Two rules keep the
scrub from becoming corruption: the assignment-shaped heuristics (`password=…`) apply to
plain text only, never through a base64 file body an approval may later commit; and a
stored plan the scrub *did* alter is refused at execution, not committed with
`***REDACTED***` in it.

**Policy is data, and it is enforced twice.** What the system may do is a YAML file — which
tools, under which diagnosis, above which confidence, within which retry cap — evaluated by a
tiny matcher and quoted verbatim into the trace. The gateway re-checks the forbidden set on
its own, so a plan that names `merge_pull_request` is refused even if the paperwork said
`allow`. Plans that need a person are stored and wait; approving one re-runs the policy
against memory as it is *now*, not as it was.

## Local development

```bash
uv sync
uv run pytest -q
docker compose up -d --build
```

Configuration is environment-driven and validated at boot (`src/settings.py`); `.env.example`
lists every key. `HARNESS_GEMINI_API_KEY`, `HARNESS_GITHUB_TOKEN` and
`HARNESS_GITHUB_WEBHOOK_SECRET` are required — the process refuses to start without them
rather than failing on the first request.

To drive it from a real repository: `scripts/seed_demo_repo.sh <you>/harness-demo-repo`
seeds a repository whose four workflows fail the four recorded ways, `HARNESS_GATEWAY=github`
plus `HARNESS_ALLOWED_REPOS=["<you>/harness-demo-repo"]` opt the service into live mode, a
`workflow_run` webhook pointed at `/webhooks/github` delivers the failures, and
`scripts/record_fixture.py` turns any failing run into a new replayable scenario without a
model call. `scripts/replay.py --post-signed fixtures/scenarios/flaky_test/webhook.json`
delivers a recorded webhook to a local server the way GitHub would, signature and all.

On the free model tier, a `503` (overloaded) counts against the 20 requests a day. The
`HARNESS_LLM_*` retry settings have a free-tier profile in `.env.example`: two attempts,
5 s apart, instead of four within a second. `scripts/quota_ledger.py [--space URL]` counts
the Pacific day's spend from the trace, failed attempts included, at no cost.
`scripts/probe_gemini.py` spends one request to ask whether the model is answering, before
you spend a replay.

`PLAN.md` is the normative build plan; `docs/progress/` records each phase's verification.
