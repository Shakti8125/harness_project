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

## Try it

Live at <https://shakti-agent-harness.hf.space>. There is no CLI — the surface is HTTP.

```bash
BASE=https://shakti-agent-harness.hf.space

curl -s -X POST $BASE/v1/replay/real_regression | jq '.final.diagnosis'
```

That runs the whole pipeline synchronously and takes about 50 seconds, because it makes two
real model calls. The asynchronous form returns `202` immediately and runs in the background:

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
> costs two, so the demo can be exhausted by about ten requests. Nothing worse is exposed —
> `HARNESS_DRY_RUN=true` and `HARNESS_GATEWAY=replay` are the defaults, so no live repository
> is ever touched.

`real_regression` is a recorded scenario: an off-by-one in a `discount()` helper makes two
pricing tests fail. The system fetches the job list, downloads a 3,800-line job log,
resolves the last green run on the branch, diffs against it, budgets the log down to fit a
model's context *without ever dropping the error lines*, and returns a diagnosis that
blames the right commit and quotes the patch line responsible.

Everything replays from `fixtures/scenarios/` — no live repository is touched, and
`HARNESS_DRY_RUN` defaults to `true`.

| Endpoint | What it does |
|---|---|
| `POST /v1/replay/{scenario}` | Run a recorded scenario end to end, synchronously |
| `POST /v1/runs` | Accept a run, execute it in the background (`202`) |
| `GET /v1/runs/{run_id}` | The run outcome |
| `GET /v1/runs/{run_id}/trace` | Every span, with token usage and timings |
| `GET /healthz` · `GET /readyz` | Liveness and readiness |

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
argues before it concludes, and the response is still re-validated on the way back.

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

`PLAN.md` is the normative build plan; `docs/progress/` records each phase's verification.
