# PLAN.md — Agent Harness (investigate → diagnose → remediate), CI/CD triage as first integration

---

## Context

**Why this exists.** The portfolio artifact is *the harness*, not the CI/CD bot. Hiring managers for
GenAI / agentic / LLM-infra roles have seen a hundred "LLM reads my logs" demos. What they have not
seen often is a clean separation between a reusable multi-agent control plane (orchestration,
context budgeting, tool sandboxing, memory, claim verification, policy, tracing, recovery) and a
thin domain integration that plugs into it. The thing being demonstrated is that the seam holds:
that a second domain could be added without editing the harness.

**The problem the domain solves.** CI failures are triaged by humans doing the same four things
every time: read the log, look at the diff since the last green run, check whether this test has
failed before, then either hit retry, revert, or file a ticket. That loop is mechanical enough to
automate and consequential enough that it must not be automated naively — which is exactly why it
makes a good showcase for guardrails, claim verification, and human escalation.

**Intended outcome.** A containerized FastAPI service that accepts a failing CI job (via replay
fixture or a real GitHub webhook), runs Investigator → Diagnostician → Remediator, produces a
structured diagnosis with cited evidence that is machine-verified against the actual log and diff,
takes only the action policy allows, and emits a per-run trace showing every decision. Deployed on
a **Hugging Face Gradio Space** (<https://shakti-agent-harness.hf.space>). Six vertical slices,
each one runnable and demoable on its own.

> **Amendment (Phase 1, recorded 2026-09-11).** This document was written against Fly.io and named
> it throughout. Fly began requiring payment information before it would create an app, and a
> Docker Space was not available on the account either, so the "reachable over the public
> internet" requirement is met by a Gradio Space instead. `fly.toml` and the `Dockerfile` are kept
> and unchanged in substance -- any container host still serves `src/api/main.py` directly, so Fly
> remains *a* target, just not the live one. `docs/deploy-huggingface.md` is authoritative on the
> Space and documents three non-obvious things the substitution forced (`ssr_mode=False`, the mount
> order, and the `zero-a10g` pin). Every `fly deploy` / `<app>.fly.dev` line in the Verify blocks
> below is updated, and the Appendix D risk register is corrected where the Fly assumption changed
> the answer rather than only the vocabulary -- risks 2 and 11 both did.

**Non-goals.** No Kubernetes. No model training or fine-tuning. No multi-tenant SaaS. No
autonomous merging, ever. No live dependency on something actually breaking — the demo path is
deterministic fixture replay.

---

## Table of contents

- [Architectural rules](#architectural-rules)
- [Repository layout](#repository-layout)
- [Cross-cutting decisions](#cross-cutting-decisions)
- [Concrete numbers in one place](#concrete-numbers-in-one-place)
- [Phase 0 — Scaffold](#phase-0--scaffold-half-day)
- [Phase 1 — Investigator + Diagnostician](#phase-1--investigator--diagnostician-deployed)
- [Phase 2 — Remediator + Guardrails](#phase-2--remediator-retry-only--guardrails)
- [Phase 3 — Memory](#phase-3--memory)
- [Phase 4 — Evaluator + Recovery](#phase-4--evaluator--recovery)
- [Phase 5 — Observability + real webhook](#phase-5--observability-trace-view--real-github-webhook)
- [Phase 6 — Second adapter (stretch)](#phase-6--second-tool-gateway-adapter-stretch)
- [Appendix A — Interfaces](#appendix-a--interfaces-every-contract-in-one-place)
- [Appendix B — Failure-path matrix](#appendix-b--failure-path-matrix-every-external-call)
- [Appendix C — Idempotency](#appendix-c--idempotency)
- [Appendix D — Cold start](#appendix-d--cold-start-no-prior-green-run)
- [Appendix E — Secrets and config](#appendix-e--secrets-and-config)
- [Open risks](#open-risks)

---

## Architectural rules

Two layers, enforced mechanically rather than by discipline:

| Layer | Package | May import | May know about |
|---|---|---|---|
| Harness (domain-agnostic) | `src/harness/**` | stdlib, pydantic, httpx, aiosqlite, google-genai (`llm.py`), pyyaml (`guardrails.py`'s policy loader) | nothing CI-specific |
| Integration (domain) | `src/integrations/cicd/**` | harness + GitHub SDK | everything CI-specific |
| API/wiring | `src/api/**`, `src/settings.py` | both | composition root only |

**Enforcement:** `tests/test_layering.py` walks the AST of every module under `src/harness/`, collects
all `import` / `ImportFrom` targets, and asserts none resolve into `src.integrations` or a
denylist of domain words (`github`, `workflow_run`, `pytest`, `pull_request`, `ci`). It also greps
`src/harness/**` for those words as bare string literals. The test fails the build. This is the
single most important test in the repo — it is what makes "the harness is reusable" a checkable
claim rather than an assertion in a README.

**Decision (layering enforcement):** AST-level import lint in the test suite. Chose it because it is
zero-dependency, runs in CI, and produces a concrete failure message. Next-simplest alternative:
`import-linter` with a `.importlinter` contracts file — slightly nicer config, one more dependency;
worth swapping to if the ruleset grows past ~5 rules.

**Decision (harness is a library, not a framework):** `harness/` exposes classes and protocols; the
integration constructs and injects them at a composition root (`src/api/deps.py`). No plugin
discovery, no entry-point registry, no dependency-injection container. Next-simplest alternative:
setuptools entry points for integration discovery — deferred; it buys nothing with two integrations
and hides the wiring that reviewers most want to see.

---

## Repository layout

```
harness_project/
├─ PLAN.md                      ← this file
├─ README.md                    ← architecture diagram, demo GIF, eval table
├─ pyproject.toml               ← uv-managed, python = "==3.12.*"
├─ uv.lock
├─ Dockerfile                   ← multi-stage, python:3.12-slim
├─ docker-compose.yml           ← app + volume mount for ./data
├─ fly.toml                    ← kept; Fly is a target, not the live one (see Context)
├─ app.py                      ← Hugging Face Space entrypoint: mounts the FastAPI app
├─ requirements.txt            ← Space-only; gradio is deliberately absent from uv.lock
├─ .env.example                 ← committed, placeholders only
├─ .dockerignore                ← excludes .env, data/, fixtures/recorded/
├─ src/
│  ├─ settings.py               ← pydantic-settings, the ONLY place env is read
│  ├─ harness/                  ← DOMAIN-AGNOSTIC
│  │  ├─ contracts.py           ← RunRequest, RunOutcome, AgentResult, Evidence, StageRecord
│  │  ├─ agent.py               ← Agent protocol, LLMAgent base
│  │  ├─ orchestrator.py        ← Orchestrator, StageSpec, short-circuit logic
│  │  ├─ context_manager.py     ← ContextManager, ContextBudget, TruncationReport
│  │  ├─ gateway.py             ← ToolGateway protocol, ToolSpec/ToolCall/ToolResult/ToolError
│  │  ├─ memory.py              ← MemoryStore protocol, SqliteMemoryStore, migrations/
│  │  ├─ evaluator.py           ← Evaluator, Claim, ClaimVerdict, EvaluationReport, ClaimChecker
│  │  ├─ guardrails.py          ← PolicyEngine, PolicyDecision, YAML loader + matcher
│  │  ├─ observability.py       ← TraceRecorder, Span, Redactor, SecretRegistry
│  │  ├─ recovery.py            ← RetryPolicy, retry_structured(), backoff
│  │  ├─ llm.py                 ← LlmClient protocol, GeminiClient, to_gemini_schema()
│  │  ├─ confidence.py          ← ConfidenceModel, Adjustment, calibrate()
│  │  └─ errors.py
│  ├─ integrations/
│  │  ├─ cicd/                  ← DOMAIN
│  │  │  ├─ schemas.py          ← FailureBundle, Diagnosis, RemediationPlan, …
│  │  │  ├─ fingerprint.py      ← error normalization + signature_id
│  │  │  ├─ agents/{investigator,diagnostician,remediator}.py
│  │  │  ├─ prompts/*.md        ← versioned, hash logged into the trace
│  │  │  ├─ catalog.py          ← the A.4 tool catalog, shared by both gateways (Phase 2)
│  │  │  ├─ gateway_github.py   ← GitHubToolGateway(ToolGateway)
│  │  │  ├─ gateway_replay.py   ← ReplayToolGateway(ToolGateway) + fault injection
│  │  │  ├─ remediation.py      ← facts, plan normalisation, decisions, execution (Phase 2)
│  │  │  ├─ claim_checkers.py   ← quote_exists, dependency_bump, file_in_diff, …
│  │  │  ├─ policy.yaml         ← Guardrails policy for this integration
│  │  │  └─ wiring.py           ← builds the Orchestrator for this domain
│  │  └─ incident/              ← Phase 6 skeleton only
│  ├─ api/
│  │  ├─ main.py                ← every route so far (runs, replay, approvals, escalations);
│  │  │                            the routes_*.py split is deferred until a second file is needed
│  │  ├─ run_registry.py  approval_registry.py   ← in-process until the memory phase
│  │  ├─ deps.py                ← composition root
│  │  └─ templates/trace.html   ← Jinja2, Phase 5
├─ fixtures/scenarios/{flaky_test,real_regression,dependency_break,infra_timeout}/
├─ scripts/{replay.py,eval.py,record_fixture.py,scrub_fixtures.py,seed_demo_repo.sh,gen_fixture_log.py}
├─ docs/ADAPTER_GUIDE.md        ← Phase 6
└─ tests/
   ├─ test_layering.py          ← the seam test
   ├─ test_no_secret_leak.py    ← sentinel-secret scan
   ├─ contract/test_tool_gateway_contract.py   ← parametrized over ALL gateways
   └─ unit/…  integration/…
```

---

## Cross-cutting decisions

**Decision (Python 3.12, not 3.13).** Local machine has 3.13.5, but the Dockerfile pins
`python:3.12-slim` and `pyproject.toml` pins `==3.12.*` so local and container are identical.
3.12 has the widest wheel coverage for the async/DB stack. Alternative: 3.13 everywhere — fine, but
parity between dev and prod is worth more than a minor-version bump on a portfolio project.

**Decision (uv as package manager).** Not currently installed; Phase 0 installs it
(`irm https://astral.sh/uv/install.ps1 | iex`). `uv` gives a lockfile, fetches the pinned
interpreter itself, and produces a fast reproducible Docker layer. Alternative: `pip` +
`requirements.txt` + `pip-tools` — works, three times slower, and no interpreter pinning.

**Decision (Gemini SDK: `google-genai`, not `google-generativeai`).** The unified `google-genai`
package is the current SDK; the older one is legacy. Pin the model id in config
(`HARNESS_GEMINI_MODEL`), never hardcode it in a prompt or a call site.

**Decision (model per agent).** All three agents start on `gemini-3.6-flash` at `temperature=0.0`,
with the Diagnostician given a larger thinking budget. Model id is a per-agent config field
(`HARNESS_MODEL_INVESTIGATOR` etc.), so promoting the Diagnostician to `gemini-pro-latest` is one env
var. Alternative: Pro everywhere — ~10× cost and noticeably slower for a classification task that
Flash handles when the evidence is well-assembled. The eval harness (Phase 4) measures whether that
call was right instead of guessing.

**Decision (structured output = `response_schema` AND Pydantic validation).** Gemini's
`response_schema` accepts a subset of OpenAPI 3.0 schema, not full JSON Schema. `harness/llm.py`
exposes `to_gemini_schema(model: type[BaseModel]) -> dict` which:
1. dereferences `$defs`/`$ref` inline (Gemini's ref support is inconsistent across model versions),
2. rewrites `anyOf: [{...T}, {"type": "null"}]` (how Pydantic emits `Optional[T]`) into `T` +
   `"nullable": true`,
3. strips `title`, `default`, `additionalProperties`, `$schema`, `format` keywords Gemini rejects,
4. adds `propertyOrdering` from the Pydantic field order (Gemini honours it and output quality
   measurably improves when the reasoning field precedes the conclusion field),
5. rejects at import time any contract with a nested discriminated union — those must be flattened.

The response is **still** parsed with `Model.model_validate_json()`. `response_schema` is a strong
hint, not a guarantee, and the Recovery loop (Phase 4) exists precisely because it sometimes misses.
Contracts are therefore designed flat: enums over unions, `list[FlatModel]` over polymorphism.

**Decision (contracts live where their owner lives).** Generic envelopes (`AgentResult`, `ToolCall`,
`Span`, `PolicyDecision`) are in `harness/`. Payloads (`FailureBundle`, `Diagnosis`) are in
`integrations/cicd/schemas.py`. `AgentResult` is generic over its payload: `AgentResult[Diagnosis]`.
That is the seam in the type system.

**Decision (async everywhere, `aiosqlite` for the DB).** FastAPI is async; blocking `sqlite3` in the
event loop would stall concurrent runs. `aiosqlite` keeps one style. Alternative:
`asyncio.to_thread(sqlite3…)` — fewer deps, more boilerplate at every call site.

**Decision (background execution: in-process tasks, no queue).** The webhook handler returns 202
immediately and hands the run to `asyncio.create_task` behind an `asyncio.Semaphore(4)`. A run is
<60 s and there is one container. Alternative: arq + Redis — correct at scale, but adds a second
service to a project whose selling point is that it deploys as one container. The cost of this
choice is that a container restart loses in-flight runs; that is mitigated by the stale-heartbeat
takeover rule in [Appendix C](#appendix-c--idempotency) and by webhook redelivery.

**Decision (errors from the gateway are returned, not raised).** `ToolGateway.invoke()` always
returns a `ToolResult`; failures arrive as `ToolResult(ok=False, error=ToolError(...))`. Read
failures then become *evidence the agent can reason about* ("could not fetch the diff, so I cannot
rule out a regression"), and the harness gets one uniform place to classify and trace them. Only
programming errors raise. Alternative: raise typed exceptions — more Pythonic, but it forces
try/except at every agent call site and loses the "degraded but useful" path.

**Decision (prompts are files, versioned and hashed).** Each prompt lives in
`integrations/cicd/prompts/*.md` with a `version:` front-matter line. The SHA-256 of the rendered
prompt template is written into the trace span. Comparing eval runs across prompt edits is then
possible. Alternative: prompts as Python string constants — invisible in diffs and impossible to
attribute an eval regression to.

---

## Concrete numbers in one place

| Knob | Value | Where set |
|---|---|---|
| Escalation cutoff (skip Remediator, ask a human) | `final_confidence < 0.70` | `HARNESS_ESCALATION_THRESHOLD` |
| Minimum confidence to auto-retry a job | `>= 0.75` | `policy.yaml` rule `retry-flaky` |
| Minimum confidence to *propose* a fix PR | `>= 0.85` | `policy.yaml` rule `open-fix-pr` |
| Hard cap: retries per signature per 24 h | `2` | `policy.yaml` |
| Hard cap: side-effecting actions per run | `1` | `guardrails.py`, not overridable by config |
| Log context budget | 120 000 chars (~30 k tokens) | `HARNESS_LOG_CHAR_BUDGET` |
| Context anchor window | ±20 lines around each error anchor | `context_manager.py` |
| Context head / tail always kept | first 200 / last 400 lines | `context_manager.py` |
| Log download hard cap | 20 MB (keep the *last* 20 MB) | `gateway_github.py` |
| Structured-output retries (schema failure) | 3 attempts total | `RetryPolicy.max_attempts` |
| Gemini transient retries (429/503) | 4 attempts, exp backoff 0.5→8 s, full jitter | `RetryPolicy` |
| Gemini request timeout | 60 s | `HARNESS_GEMINI_TIMEOUT_S` |
| GitHub timeouts | 10 s connect / 30 s read | `HARNESS_GITHUB_TIMEOUT_S` |
| GitHub 5xx retries | 3, exp backoff 0.5→8 s | `gateway_github.py` |
| SQLite `busy_timeout` | 5 000 ms, then 3 app-level retries (100/200/400 ms) | `memory.py` |
| Run heartbeat interval / staleness | 15 s / 120 s | `orchestrator.py` |
| Approval expiry | 24 h | `HARNESS_APPROVAL_TTL_H` |
| Flaky prior threshold | ≥3 occurrences AND `flaky_count/occurrences ≥ 0.6` | `memory.py` |
| Evaluator fail condition | any `refuted`, or `verified/total < 0.5` | `evaluator.py` |
| Max concurrent runs | 4 | `HARNESS_MAX_CONCURRENT_RUNS` |
| Cumulative retry *sleep* per agent call | 20 s | `recovery.RETRY_DELAY_BUDGET_S` |
| Single stated `retry-after`, clamped | 20 s | `llm.MAX_RETRY_AFTER_S` |
| **Wall clock per run** | **240 s** | `orchestrator.DEFAULT_RUN_BUDGET_S` |

> **Amendment (recorded 2026-09-11).** The last row closes a residual Phase 1 carried
> knowingly: *"the retry bound is on sleeps, not on call durations."* The three
> retry-shaped numbers above bound different things and none of them bounds a run.
> `MAX_RETRY_AFTER_S` clamps one stated delay; `RETRY_DELAY_BUDGET_S` caps time spent
> asleep; the Gemini timeout caps one *call* and says nothing about how many calls a
> run makes. A provider that fails slowly rather than fast returns no `retry-after`
> to sleep on, so the delay budget never engages at all — which left the honest worst
> case at minutes per request on a public unauthenticated URL, with only four
> concurrency slots to exhaust. The run budget is measured from the first stage, so
> queuing on the semaphore is not charged to the run that eventually gets the slot,
> and a breach escalates as `run_timeout` rather than raising — a timed-out run
> serves the same `RunOutcome` shape as any other escalation, keeping the trace link
> that says *where* it died.

### How `final_confidence` is derived

The model self-reports `self_confidence ∈ [0,1]`, anchored by a rubric written into the
Diagnostician prompt:

| Band | Meaning stated in the prompt |
|---|---|
| 0.90–1.00 | Deterministic match: the error string and the diff independently name the same cause |
| 0.75–0.89 | One strong evidence source, nothing in the bundle contradicts it |
| 0.50–0.74 | Plausible, but the evidence is indirect or partial |
| 0.00–0.49 | Guess; prefer `unknown` |

Self-reported confidence from an LLM is not calibrated, so the harness applies **deterministic
adjustments in code** (`harness/confidence.py`) and records each one as an `Adjustment{name, delta,
reason}` in the trace. The model never sees the adjusted number.

| Adjustment | Δ | Condition |
|---|---|---|
| `memory_agreement` | +0.10 | ≥3 prior observations of this signature and ≥60 % share the verdict |
| `evidence_fully_verified` | +0.05 | Evaluator: all citations `verified` (Phase 4+) |
| `evidence_refuted` | −0.15 | Evaluator: any citation `refuted` (Phase 4+) |
| `no_citations` | −0.10 | `len(citations) == 0` |
| `empty_diff_contradiction` | −0.10 | category is `real_regression` but `DiffSummary.files == []` |
| `cold_start` | −0.05 | no green baseline existed (see Appendix D) |
| `gateway_degraded` | −0.10 | any required read tool returned an error |

`final_confidence = clamp(self_confidence + Σ deltas, 0.0, 0.99)`. The ceiling of 0.99 is
deliberate: nothing is certain, and a hard 1.0 tends to invite "confidence == 1.0" special-casing
downstream.

**Decision (0.70 as the escalation cutoff).** It sits at the boundary between the prompt's
"plausible but indirect" band and its "strong evidence" band, so a diagnosis the model itself
described as indirect never triggers an action. Action thresholds sit *above* it (0.75 retry, 0.85
PR) so that clearing escalation is necessary but not sufficient to act. Next-simplest alternative:
a single threshold used for both escalation and action — rejected because "confident enough to
report" and "confident enough to touch the repo" are genuinely different bars.
**This number is asserted, not measured** — see Open Risk 4.

**Decision (a refuted claim overrides confidence entirely).** If `EvaluationReport.verdict == "fail"`,
the run escalates regardless of `final_confidence`. Grounding beats self-belief. There is no config
override for this.

---

## Phase 0 — Scaffold (half-day)

**Built:** `uv init`, pinned deps, `src/` skeleton with empty modules, `settings.py`,
`Dockerfile`, `docker-compose.yml`, `.env.example`, `GET /healthz` + `GET /readyz`, ruff + mypy
(`strict` on `src/harness/` only), `tests/test_layering.py` passing against empty packages, git repo
initialised with `PLAN.md` as the first commit.

**Verify:**
```powershell
uv sync
uv run ruff check . ; uv run mypy src/harness
uv run pytest -q                      # expect: test_layering passes (count grows as later phases add tests)
docker compose up -d --build
curl.exe -s localhost:8000/healthz    # {"status":"ok","db":"ok","version":"0.1.0"}
```

**Deferred:** everything else.

---

## Phase 1 — Investigator + Diagnostician, deployed

**Goal of the slice:** one hardcoded scenario goes in, a validated `Diagnosis` with cited evidence
comes out, and it is reachable over the public internet.

### Built

- `harness/contracts.py`, `harness/agent.py`, `harness/llm.py` (`GeminiClient` +
  `to_gemini_schema`), `harness/context_manager.py`, `harness/gateway.py` (protocol only),
  `harness/observability.py` (span recording to SQLite + redaction), a minimal
  `harness/orchestrator.py` with the two-stage pipeline and the confidence short-circuit.
- `integrations/cicd/schemas.py`, `gateway_replay.py`, `agents/investigator.py`,
  `agents/diagnostician.py`, prompts, and the `real_regression` fixture.
- `POST /v1/replay/{scenario}`, `POST /v1/runs`, `GET /v1/runs/{run_id}`.
- Deployed and reachable on the public internet (Hugging Face Space; see Context).

### Design notes for this slice

**Decision (the Investigator is deliberately LLM-light).** Collection is deterministic Python:
fetch jobs → pick failed ones → download logs → resolve baseline → fetch compare → parse dependency
manifests. The single LLM call takes the *assembled* bundle and produces `InvestigationNotes`
(observations + up to 3 additional read-only tool calls it wants, chosen from the gateway catalog),
which are then executed and merged. Rationale: a full ReAct loop over the GitHub API for evidence
gathering is where these systems become slow, expensive, and nondeterministic, and it buys almost
nothing when the evidence set is known in advance. Next-simplest alternative: pure deterministic
collector with no LLM at all — cheaper still, but then the "agent" framing is a fiction and the
extensibility point (the model asking for evidence nobody anticipated) disappears. The hybrid keeps
both. Full tool-calling loop is deferred to post-Phase-6.

**Context Manager algorithm** (`assemble()`), deterministic and unit-tested:
1. Split the log into indexed lines; strip ANSI escapes and GitHub Actions timestamp prefixes.
2. Mark **anchors**: lines matching any of `^E\s`, `^FAILED\s`, `^ERROR\b`, `Traceback \(most
   recent call last\)`, `##\[error\]`, `AssertionError`, `Error:\s`, `npm ERR!`, `exit code \d+`,
   `\bTimeout\b`, `Connection refused`, `ModuleNotFoundError`, `ImportError`. Regex list is a
   config constant in the *integration*, passed in — the harness's Context Manager takes anchors as
   a parameter and knows nothing about pytest or Actions.
3. Anchors ± 20 lines are **inviolable**: they are selected first and never trimmed by the budget
   fill.
4. If inviolable content alone exceeds the budget, keep the **last** N anchor windows (the
   proximate failure is nearest the end) and set `truncation.anchors_dropped = k`.
5. Fill the remaining budget with the first 200 and last 400 lines.
6. Merge overlapping ranges; join with explicit markers: `\n… [4,812 lines elided] …\n`.
7. Return `ContextBundle` carrying a `TruncationReport` — the trace records exactly what was cut.

**Decision (rule-based context selection, not retrieval).** Deterministic, testable, zero extra API
calls, and — critically — it can *guarantee* the error lines survive, which an embedding ranker
cannot. Next-simplest alternative: chunk + embed + rank by similarity to "error"; revisit only if
real-repo logs defeat the anchor regexes.

### Verify

```powershell
# 1. The context manager never eats the error (the load-bearing unit test)
uv run pytest tests/unit/test_context_manager.py -q
#    includes test_error_lines_never_trimmed: 50 000-line synthetic log with an
#    AssertionError at line 12 345 → assert that line is present in the output verbatim.

# 2. End-to-end replay, locally
docker compose up -d --build
curl.exe -s -X POST localhost:8000/v1/replay/real_regression | `
  jq '{cat: .final.diagnosis.category, conf: .final.diagnosis.final_confidence, cites: (.final.diagnosis.citations | length)}'
# EXPECT exactly: {"cat":"real_regression","conf":>=0.75,"cites":>=1}

# 3. The diagnosis actually points at the right commit
curl.exe -s -X POST localhost:8000/v1/replay/real_regression | jq -r '.final.diagnosis.suspected_commit_sha'
# EXPECT the sha recorded in fixtures/scenarios/real_regression/scenario.yaml -> expected.commit

# 4. Escalation path fires
$env:HARNESS_ESCALATION_THRESHOLD="0.99"; docker compose up -d
curl.exe -s -X POST localhost:8000/v1/replay/real_regression | jq -r '.status, .escalation.reason'
# EXPECT "escalated"  "low_confidence"

# 5. Deployed   (Space; `fly deploy` stays valid for the Fly path -- see Context)
#    push to the Space remote, then:
curl.exe -s https://shakti-agent-harness.hf.space/healthz          # {"status":"ok",...}
curl.exe -s -X POST https://shakti-agent-harness.hf.space/v1/replay/real_regression | jq -r '.final.diagnosis.category'
# EXPECT "real_regression"
```

**Deferred from this phase:** Remediator, Guardrails, Memory (`prior_history` is a hardcoded empty
`PriorHistory`), Evaluator (`citations` are recorded but unverified), the HTML trace view, live
GitHub, webhooks. Three of the four fixtures.

> **Status: shipped, tagged `phase-1-green`.** Two corrections against the plan as written.
>
> **Recovery was not deferred — it shipped here.** `harness/recovery.py` is complete and wired into
> *every* agent call at `src/harness/agent.py:234`, with the structured-retry loop, the terminal
> finish-reason set, `MAX_RETRY_AFTER_S` and a cumulative `RETRY_DELAY_BUDGET_S`. It was pulled
> forward because the Gemini failure paths in Appendix B.1 are not optional once a real provider is
> in the loop. Phase 4's Built list is corrected to match.
>
> **Verify step 4 was satisfied offline.** The escalation-threshold override was covered by unit
> tests rather than by the literal `curl` above, and does not need re-running. Recorded in
> `docs/progress/phase-1/backlog.md`, "Settled — do not reopen".
>
> Three independent audits closed 24 findings. The carried residuals, the audit-provenance gap and
> the settled decisions are in `docs/progress/phase-1/backlog.md`;
> `docs/progress/phase-2/handoff.md` is the entry point for the next session.

---

## Phase 2 — Remediator (retry only) + Guardrails

**Goal of the slice:** the system takes its first real action, and cannot take a dangerous one.

> **Amendment (recorded 2026-09-11, at the close of Phase 1).** Three things below are not true as
> written any more. Read this before dispatching.
>
> **1. More exists than the Built list implies.** Phase 0 scaffolded the contracts, so
> `harness/guardrails.py` already carries `Condition`, `Rule`, `PolicySpec`, `ActionContext` and
> `PolicyDecision` fully transcribed from A.7, plus the hardcoded
> `MAX_SIDE_EFFECTING_ACTIONS_PER_RUN = 1`; only `PolicyEngine.__init__` and `PolicyEngine.decide`
> raise `NotImplementedError`. `src/integrations/cicd/policy.yaml` is already committed, verbatim
> from the block below. The real work is the engine and the two placeholder modules
> (`agents/remediator.py`, `gateway_github.py`), not model transcription.
>
> **2. `open-fix-pr` cannot match in this phase, so Verify step 2 cannot pass.** The rule gates on
> `evaluation.verdict: {eq: pass}`, and this phase's own Deferred paragraph sets
> `evaluation.verdict` to the literal `"skipped"` because the Evaluator is Phase 4. No other rule
> matches a write tool, so `default_effect: deny` answers and the expected `require_approval` never
> appears. The sibling `retry-suspected-flaky` rule was written `{in: [pass, skipped]}`, so this
> reads as an oversight in one rule rather than a deliberate gate. **The YAML below is amended to
> `{in: [pass, skipped]}`; `src/integrations/cicd/policy.yaml` still carries the old clause and
> syncing it is Phase 2's first task.** The alternative — having the deferred Evaluator return
> `"pass"` — was rejected: it fabricates a verdict nobody computed, and `"skipped"` is load-bearing
> precisely because it is honest. Phase 4 removes `skipped` from the rule when a real verdict exists.
>
> **3. The retry-count stub must fail closed, not open.** See the amended Deferred paragraph at the
> end of this phase.

### Built

- `harness/guardrails.py`: YAML policy loader + matcher + `PolicyDecision`.
- `harness/orchestrator.py`: third stage, plus the approval-pending suspend path.
- `integrations/cicd/agents/remediator.py`, `policy.yaml`.
- `integrations/cicd/gateway_github.py` — **read tools only, plus `rerun_failed_jobs`**.
- `POST /v1/approvals/{approval_id}`, `GET /v1/escalations`.
- Fixtures 2 and 3: `flaky_test`, `infra_timeout`.

### Guardrails representation — exact shape

Policy is a **declarative YAML file per integration**, loaded at startup, validated against a
Pydantic `PolicySpec`, and evaluated by a small in-process matcher. It is *not* Python predicates
and *not* an external policy server.

```yaml
# src/integrations/cicd/policy.yaml
version: 1
integration: cicd
default_effect: deny          # anything not matched is denied

# Hard denies evaluated FIRST, cannot be overridden by any allow rule below.
forbidden:
  - merge_pull_request
  - force_push
  - delete_branch
  - delete_workflow_run
  - create_deployment
  - update_branch_protection

rules:
  - id: retry-suspected-flaky
    tools: [rerun_failed_jobs]
    effect: allow
    when:
      diagnosis.category:            {in: [flaky_test, infra_transient]}
      diagnosis.final_confidence:    {gte: 0.75}
      evaluation.verdict:            {in: [pass, skipped]}
      memory.retries_for_signature_24h: {lt: 2}
      context.cold_start:            {eq: false}
    obligations: [record_observation, annotate_run]

  - id: open-fix-pr
    tools: [create_branch, create_or_update_file, open_pull_request]
    effect: require_approval
    when:
      diagnosis.category:         {in: [real_regression, dependency_break, config_issue]}
      diagnosis.final_confidence: {gte: 0.85}
      evaluation.verdict:         {in: [pass, skipped]}   # `skipped` comes back out in Phase 4,
                                                          # when a real verdict exists (amendment 2)
    obligations: [draft_only, label:agent-generated, no_auto_merge, assign_human_reviewer]

  - id: file-ticket
    tools: [create_issue]
    effect: allow
    when:
      diagnosis.final_confidence: {gte: 0.0}
    obligations: [label:agent-triage]

  - id: read-only-always
    tools: ["read:*"]        # any tool whose ToolSpec.side_effect == "read"
    effect: allow
```

`PolicyDecision{rule_id, effect, reason, obligations, evaluated_at}` is returned for every call and
written to the trace.

**Carried in from Phase 1, and load-bearing here.** `gateway_replay.py`'s `forbidden` is a
*required* keyword argument (`review.md` finding 12), so the authoritative safety re-check cannot be
skipped by omission at construction. **`GitHubToolGateway` must follow that pattern** — it is the
gateway where the re-check actually stops something, and a default-empty `forbidden` would make
`test_gateway_refuses_forbidden_even_with_forged_allow_decision` pass vacuously. Separately,
`diagnosis.final_confidence` is now trustworthy for the thresholds below: `review.md` finding 4 was
closed by stripping `final_confidence` and `confidence_adjustments` from the *wire* schema, so the
model is never asked for the two fields A.11 marks harness-added.

**Two enforcement points.** The Orchestrator consults the engine *before* invoking the Remediator's
plan (so it can suspend for approval), and `ToolGateway.invoke()` requires a `PolicyDecision`
argument and re-checks the tool name against `forbidden` itself. The gateway is authoritative: even
if the Remediator hallucinates `merge_pull_request`, the gateway refuses it and returns
`ToolError(kind="forbidden_by_policy")`. Two independent checks, because the interesting failure
mode is an agent inventing a tool call that never went through the planner.

Additionally, two invariants are hardcoded in `guardrails.py` and **not** expressible in YAML, so
no config edit can disable them: `max_side_effecting_actions_per_run = 1`, and "a tool in
`forbidden` is denied unconditionally".

**Decision (YAML policy + tiny matcher).** Policy is data, so it appears in diffs, can be shown to a
reviewer on one screen, and is quoted verbatim in the trace. The matcher supports only
`eq/ne/in/nin/gte/gt/lte/lt` over a flat dotted namespace (`diagnosis.*`, `memory.*`, `context.*`,
`evaluation.*`) — deliberately not a general expression language. Next-simplest alternative: an
`allowed_actions: dict[Category, Effect]` table in Python — half the code, but the conditions on
confidence and retry-count would then be scattered through the orchestrator instead of stated in
one place. Rejected alternative at the other end: OPA/Rego — correct for a real org, absurd
operational weight here.

**Decision (approvals are a persisted state machine, not a blocking wait).** When a decision is
`require_approval`, the Remediator produces the plan, the harness stores the exact `list[ToolCall]`
in the `approval` table with state `pending`, and the run completes with
`status="awaiting_approval"`. A human `POST`s a decision; the harness **re-evaluates policy against
the stored plan at execution time** before running it (the diagnosis may have been superseded).
Expires after 24 h. Alternative: block the request until a human responds — untenable with HTTP.

### Verify

```powershell
# 1. Flaky scenario auto-retries, and the trace names the rule that let it
curl.exe -s -X POST localhost:8000/v1/replay/flaky_test | `
  jq '{action: .final.remediation.plan.action, rule: .final.remediation.decisions[0].rule_id, tool: .final.remediation.executed[0].tool, ok: .final.remediation.executed[0].ok}'
# EXPECT {"action":"retry_job","rule":"retry-suspected-flaky","tool":"rerun_failed_jobs","ok":true}

# 2. Regression scenario is BLOCKED pending approval, nothing executed
curl.exe -s -X POST localhost:8000/v1/replay/real_regression | `
  jq '{status: .status, effect: .final.remediation.decisions[0].effect, executed: (.final.remediation.executed | length), approval: .final.remediation.pending_approval.approval_id}'
# EXPECT {"status":"awaiting_approval","effect":"require_approval","executed":0,"approval":"apr_…"}

# 3. Forbidden actions are unreachable from every state (the important test)
uv run pytest tests/unit/test_guardrails.py -q
#    test_forbidden_denied_from_all_states: for each of the 6 categories × confidence
#    {0.0,0.5,0.75,0.9,0.99} × evaluation {pass,warn,fail} → assert merge_pull_request,
#    force_push, delete_branch all return effect="deny".  90 cases, all must deny.
#    test_gateway_refuses_forbidden_even_with_forged_allow_decision: construct a
#    PolicyDecision(effect="allow") by hand for merge_pull_request, pass it to
#    GitHubToolGateway.invoke → assert ToolError(kind="forbidden_by_policy") and that
#    respx recorded ZERO outbound HTTP calls.
#    test_max_one_side_effecting_action_per_run.

# 4. Approval round-trip
$apr = (curl.exe -s -X POST localhost:8000/v1/replay/real_regression | jq -r '.final.remediation.pending_approval.approval_id')
curl.exe -s -X POST "localhost:8000/v1/approvals/$apr" -H "content-type: application/json" -d '{\"decision\":\"reject\",\"actor\":\"shakti\"}' | jq -r '.state'
# EXPECT "rejected"   and re-POSTing the same approval → 409 with state "rejected"

# 5. First live GitHub call, read-only, against the demo repo
$env:HARNESS_GATEWAY="github"; $env:HARNESS_DRY_RUN="true"
uv run python scripts/replay.py --live --repo <you>/harness-demo-repo --run-id <failing_run_id>
# EXPECT a diagnosis printed, and in the trace every span with component="gateway"
#        has side_effect="read".  Then set HARNESS_DRY_RUN=false and confirm the
#        job actually re-runs in the GitHub Actions UI.
```

**Deferred:** Memory (the retry-count guard reads a stub — see below), Evaluator
(`evaluation.verdict` is the literal `"skipped"`), trace view, webhooks, PR-writing tools (the
policy rule exists and is exercised, but `create_branch`/`open_pull_request` are registered in the
catalog with a `NotImplementedError` body — the point of this phase is that the *decision* is
right, not that the PR gets written). Recovery is **not** deferred here; it shipped in Phase 1.

> **Amendment 3 — the retry-count stub returns `999`, not `0`.** The original text said the guard
> "reads a stub returning 0". Against `memory.retries_for_signature_24h: {lt: 2}` that makes the
> clause *always true*, so the retry cap does not exist in this phase — it only looks like it does,
> including in the trace, which quotes the rule as though the clause bit.
>
> That is the same fail-open Phase 1 closed twice one layer down: `review.md` finding 11 closed it
> in `PriorHistory`, and the final audit then found that fix carried an escape hatch which fell open
> on exactly its motivating shape, closing it again unconditionally
> (`src/integrations/cicd/schemas.py`, `FAIL_CLOSED_RETRIES_IN_24H`). A 0-returning stub hands the
> same fail-open straight back at the policy layer.
>
> Use `FAIL_CLOSED_RETRIES_IN_24H` (999), per B.3. This phase then demonstrates "the cap denies",
> which is the honest thing to show and matches `default_effect: deny`; Phase 3 replaces a constant
> with a real query rather than changing a behaviour. Note the consequence for Verify step 1: with
> the cap failing closed, `retry-suspected-flaky` denies, so step 1 must either inject a memory stub
> returning a real count under 2 for that scenario, or assert the deny and move the auto-retry
> demonstration to Phase 3 where memory is real. Choose deliberately and write down which.

> **Status: built, at `fc8209b` and after.** Four decisions against the plan as written, all in
> `docs/progress/phase-2/dispatch.md` with their reasoning; the short form:
>
> **Verify step 1 is recorded against the deny.** The retry-count fact is read straight off
> `bundle.prior_history.retries_in_24h`, which `PriorHistory(unavailable=True)` forces to 999 --
> one fail-closed source of truth, no second stub constant. So the live `flaky_test` and
> `infra_timeout` replays deny `rerun_failed_jobs` and escalate as `policy_denied`, and the
> `<default>` decision's reason names the clause (`memory.retries_for_signature_24h: 999 fails
> {lt: 2}`). The allow path is pinned by a test that supplies a readable history and asserts the
> gateway executed the re-run. Phase 3 lights the live demonstration up.
>
> **A denied plan escalates the run.** `policy_denied` has existed in `EscalationReason` since
> Phase 0 and nothing else could produce it; the `RemediationResult` with every decision stays in
> `final`.
>
> **"Registered with a `NotImplementedError` body" is realised as a `ToolError`.** `invoke` must
> not raise for anything but a programming error, and a person approving a fix PR through the API
> is not one. The unimplemented write tools are in the catalog, the decision about them is real,
> and invoking one answers `ToolError(kind="unknown", retryable=False)` naming the phase.
>
> **Tool calls are derived from the drafts whenever the drafts determine them.** The first live
> run showed the model proposing `create_branch` with empty args and no PR; the harness now builds
> branch/files/PR from `pr_draft` (and the retry from the bundle's own run id), using the model's
> proposed calls only when nothing can be derived. The model chooses the action and writes the
> content; the calls are mechanical.
>
> Also: the `max_side_effecting_actions_per_run` invariant counts *actions* (executed plans), not
> tool calls -- a fix PR is one action made of three write calls -- and fails closed when the count
> fact is absent. A model-proposed call outside its stated action's tool set is dropped before it
> is judged and recorded on the `remediation.plan` span (audit finding 2); a proposed call naming a
> *forbidden* tool is the one exception, always carried into the judged plan so it is denied by
> name and the run escalates. A plan whose execution fails escalates `tool_failure` (B.2), in-run
> and after approval alike; a person's rejection is `RemediationResult.status="rejected"`, never an
> escalation. Live mode reached the API behind two opt-ins (`HARNESS_GATEWAY=github` and the
> repo allowlist), with `scripts/replay.py --live` driving the same path; step 5 is blocked on a
> real token and a demo repository and is recorded that way in `docs/progress/phase-2/verify.md`.

---

## Phase 3 — Memory

**Goal of the slice:** the fourth time a test flakes, the system says so instead of re-deriving it.

### Built

`harness/memory.py` (protocol + `SqliteMemoryStore` + migrations),
`integrations/cicd/fingerprint.py`, Investigator wired to populate `PriorHistory`, Remediator
writing an `observation` row, `memory.retries_for_signature_24h` now real.

### Schema

```sql
-- src/harness/migrations/001_init.sql   (applied in order, tracked in schema_version)
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS failure_signature (
  signature_id      TEXT PRIMARY KEY,       -- sha256(scope_key)[:32]
  scope             TEXT NOT NULL,          -- "repo:owner/name" — the per-repo partition
  subject_key       TEXT NOT NULL,          -- integration-defined: "workflow|job|test_id"
  fingerprint       TEXT NOT NULL,          -- normalized error fingerprint
  first_seen_at     TEXT NOT NULL,
  last_seen_at      TEXT NOT NULL,
  occurrences       INTEGER NOT NULL DEFAULT 0,
  verdict_counts    TEXT NOT NULL DEFAULT '{}',   -- JSON {category: n}
  last_verdict      TEXT,
  last_run_id       TEXT
);
CREATE INDEX IF NOT EXISTS ix_sig_scope_seen ON failure_signature(scope, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS observation (
  observation_id TEXT PRIMARY KEY,
  signature_id   TEXT NOT NULL REFERENCES failure_signature(signature_id),
  run_id         TEXT NOT NULL,
  occurred_at    TEXT NOT NULL,
  verdict        TEXT NOT NULL,
  confidence     REAL NOT NULL,
  action_taken   TEXT,
  action_outcome TEXT,             -- "passed_on_retry" | "failed_again" | "pending" | NULL
  commit_sha     TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_sig_time ON observation(signature_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS run (
  run_id          TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  integration     TEXT NOT NULL,
  status          TEXT NOT NULL,
  attempt         INTEGER NOT NULL DEFAULT 1,
  superseded_run_id TEXT,
  created_at      TEXT NOT NULL,
  heartbeat_at    TEXT,
  completed_at    TEXT,
  outcome_json    TEXT
);

CREATE TABLE IF NOT EXISTS trace_span (
  span_id TEXT PRIMARY KEY, parent_span_id TEXT, run_id TEXT NOT NULL,
  name TEXT NOT NULL, component TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, duration_ms INTEGER,
  attributes_json TEXT NOT NULL DEFAULT '{}', error_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_span_run ON trace_span(run_id, started_at);

CREATE TABLE IF NOT EXISTS approval (
  approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, state TEXT NOT NULL,
  plan_json TEXT NOT NULL, requested_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  decided_at TEXT, decided_by TEXT, decision_note TEXT
);

CREATE TABLE IF NOT EXISTS escalation (
  escalation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, reason TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
  channel TEXT NOT NULL, delivered_at TEXT, delivery_error TEXT
);
```

Note `scope` / `subject_key` / `fingerprint` rather than `repo` / `test_name` — the *harness* owns
this table and must not name CI concepts. The integration decides what goes in them.

**Fingerprint algorithm** (`integrations/cicd/fingerprint.py`, pure function, heavily unit-tested):
1. From the anchor lines, take the last exception block (or the first `FAILED …` line for pytest).
2. Extract `exc_type` and `message`.
3. Normalize `message`: absolute paths → basename; `0x[0-9a-f]+` → `<ADDR>`; UUIDs → `<UUID>`;
   ISO timestamps and `HH:MM:SS` → `<TS>`; hex runs ≥6 → `<HEX>`; integers ≥2 digits → `<N>`;
   `\d+(\.\d+)?\s*(ms|s|sec)` → `<DUR>`; collapse whitespace; truncate to 200 chars.
4. `fingerprint = sha256(f"{exc_type}|{normalized}")`.
5. `signature_id = sha256(f"{scope}|{subject_key}|{fingerprint}")[:32]`.

**Flakiness prior** (deterministic, computed in `memory.py`, *not* by the model):
`prior_hint = "likely_flaky"` when `occurrences >= 3` and
`verdict_counts["flaky_test"] / occurrences >= 0.6` and at least one prior observation has
`action_outcome == "passed_on_retry"`. Otherwise `"likely_real"` if the real-regression share
≥0.6, else `"unknown"`.

**Decision (memory supplies a prior, never a verdict).** `PriorHistory` is injected into the
Diagnostician prompt as context with an explicit instruction: *"prior history is a prior, not
evidence; you must still cite something from this run's log or diff. If this run's evidence
contradicts the prior, follow the evidence and say so."* Rationale: a memory that short-circuits
diagnosis will confidently retry a genuinely broken test forever the moment it was once mislabeled
flaky. Next-simplest alternative: skip the LLM entirely when `prior_hint == "likely_flaky"` —
faster and cheaper, and exactly the failure mode above. The retry cap (2 per signature per 24 h) is
the backstop.

**Decision (memory is a soft dependency).** Every `MemoryStore` call is wrapped; on failure the run
continues with `PriorHistory.unavailable = True`, `degraded_components += ["memory"]`, and the
`memory_agreement` confidence bonus is not applied. A dead cache must never take down triage.

**Decision (SQLite, single writer, WAL).** `PRAGMA journal_mode=WAL`, `busy_timeout=5000`,
`synchronous=NORMAL`, plus one process-wide `asyncio.Lock` around writes. With ≤4 concurrent runs
this is entirely sufficient. Next-simplest alternative: Postgres — the right answer the moment
there is more than one container, and the `MemoryStore` protocol exists so that is a one-class
change. See Open Risk 2.

### Verify

```powershell
# 1. Fingerprint stability — the property that makes memory work at all
uv run pytest tests/unit/test_fingerprint.py -q
#    test_stable_across_noise: two logs identical except timestamps, temp paths, durations,
#      pytest-xdist worker ids and object addresses → SAME signature_id.
#    test_distinguishes_real_difference: AssertionError vs TimeoutError → DIFFERENT ids.

# 2. Repeat flakiness is recognized across runs
rm ./data/harness.db
1..3 | ForEach-Object { curl.exe -s -X POST localhost:8000/v1/replay/flaky_test?fresh=1 | Out-Null }
curl.exe -s -X POST "localhost:8000/v1/replay/flaky_test?fresh=1" | `
  jq '{occ: .final.bundle.prior_history.occurrences, hint: .final.bundle.prior_history.prior_hint, adj: [.final.diagnosis.confidence_adjustments[] | select(.name=="memory_agreement") | .delta]}'
# EXPECT {"occ":3,"hint":"likely_flaky","adj":[0.10]}

sqlite3 ./data/harness.db "select occurrences, last_verdict, verdict_counts from failure_signature;"
# EXPECT  4|flaky_test|{"flaky_test":4}

# 3. The retry cap actually bites
1..4 | ForEach-Object { curl.exe -s -X POST localhost:8000/v1/replay/flaky_test | jq -r '.final.remediation.decisions[0].effect' }
# EXPECT  allow, allow, deny, deny      (rule retry-suspected-flaky, reason mentions retries_for_signature_24h)

# 4. Memory outage degrades, does not fail
$env:HARNESS_FAULT_INJECT="sqlite_locked"; docker compose up -d
curl.exe -s -X POST localhost:8000/v1/replay/flaky_test | jq '{status:.status, degraded:.degraded_components}'
# EXPECT {"status":"completed","degraded":["memory"]}
```

**Deferred:** Evaluator, trace view, webhooks, PR writing. (Recovery shipped in Phase 1.)

> **Amendment (recorded 2026-09-11).** Two things to settle at the *start* of this phase.
>
> **The live target has no persistent disk.** The Hugging Face Space that serves this resets
> `data/harness.db` on every restart or sleep, so a SQLite memory store is amnesiac there — which
> would make Verify step 2 above ("4th flaky run shows `occurrences=3`") unreachable on the live
> URL, though it still passes locally and under Docker. See Appendix D risk 2 for the options; the
> `MemoryStore` protocol exists precisely so this is one class and no call-site changes. Decide
> before building, not at the Verify step.
>
> **This phase is where the retry cap becomes real.** Phase 2 leaves
> `memory.retries_for_signature_24h` as a fail-closed constant (999, per that phase's amendment 3),
> so Verify step 3 here is the first time the cap is exercised against actual counts. If Phase 2
> moved its own auto-retry demonstration forward, it lands here.

> **Amendment (Phase 3, recorded 2026-09-13 — what was built, and how the Verify block reads
> against it).** Full reasoning in `docs/progress/phase-3/dispatch.md`.
>
> 1. **The live target stays SQLite; the cross-run demo runs locally.** No persistent volume and
>    no Postgres this phase (both the user's call). On the Space, memory is real within one waking
>    period and amnesiac across restarts.
> 2. **`src/harness/storage.py` owns the SQLite file** — connections with the pragmas above,
>    the migration runner (`migrations/NNN_*.sql`, tracked in `schema_version`, idempotent), and
>    the B.3 corrupt-file quarantine. It exists so `observability.py` and `memory.py` can share
>    the file without importing each other. **The recorder now writes `trace_span`**; a legacy
>    Phase 1 `spans` table is carried over on first migration and dropped.
> 3. **Fingerprints are computed from anchor lines only**, so the raw log and the budgeted
>    excerpt agree. Durations are normalised *before* integers (the listed order would leave
>    `1.207s` as `1.<N>s` and nothing for `<DUR>` to match); `[gwN]` → `<WORKER>` is added.
>    For a pytest log the subject is the first `FAILED` nodeid and the exception is its first
>    `E` line (or the summary suffix, or the `path:line: Type` location line for a bare
>    assertion); otherwise the last `Type: message` line.
> 4. **The prior's numbers live in the harness, its words in the integration.**
>    `memory.dominant_verdict(occurrences, verdict_counts)` applies the ≥3 / ≥0.6 rule;
>    `integrations/cicd/history.py` maps a dominant `flaky_test` plus a `passed_on_retry`
>    observation to `likely_flaky`, a dominant `real_regression` to `likely_real`. The
>    `memory_agreement` bonus fires when the dominant verdict equals the category, never on a
>    degraded read.
> 5. **`pending` outcomes resolve without a webhook.** At the next sighting the Investigator reads
>    each pending retry's original bundle back out of the `run` table and probes
>    `list_workflow_run_jobs(run_id, attempt + 1)`: all `success` → `passed_on_retry`, any
>    `failure` → `failed_again`, else still pending. The `flaky_test` fixture ships the rerun's
>    green job list; `infra_timeout` deliberately does not. Phase 5's webhook is the other source.
> 6. **Who writes:** the Diagnostician `upsert_signature`s and records the observation (verdict,
>    calibrated confidence, no action) for every diagnosed run; the Remediator re-records the
>    same row (deterministic id `sha256(signature_id|run_id)`) with `action_taken` = the plan's
>    terminal write tool and `action_outcome="pending"` once a side-effecting plan executed,
>    dry-run included. That is the `record_observation` obligation acted on; `annotate_run` is
>    still recorded, not executed.
> 7. **The Verify block, as it can honestly be met.** Step 2 passes as written. **Step 3 must
>    start from a fresh database** (step 2's first two runs already spent the window's two
>    retries; its own four runs are `allow, allow, deny, deny`). **Step 4's expectation is
>    `{"status":"escalated","degraded":["memory"]}`** with `escalation.reason ==
>    "policy_denied"` naming `memory.retries_for_signature_24h: 999`: under Phase 2's
>    deny-escalates decision and B.3's fail-closed cap, a memory outage produces the diagnosis,
>    reports the outage, and refuses to act on a cap it cannot verify. `HARNESS_FAULT_INJECT=
>    sqlite_locked` fails every store call before the B.3 ladder (migrations exempt) and is
>    refused outside `HARNESS_ENV=dev`.
> 8. **Memory's soft-dependency rule extends to the API's own use of the store.** A claim that
>    cannot be made runs the request unclaimed under a minted id (no dedup for that request,
>    logged); an outcome that cannot be saved is still returned; a read route that needs the
>    store answers `503 application/problem+json` with `Retry-After`.
>
> **Fix round (audit `docs/progress/phase-3/review.md`, same day).** What the ten findings
> changed at contract level; the rest is in `backlog.md`.
>
> 9. **A takeover chain is `<key>` and `<key>#<digits>`, nothing else.** The claim's chain
>    lookup decides membership exactly (`memory.is_chain_member`); the API's `<key>#fresh:<nonce>`
>    replay rows share the prefix and are not in the chain (finding 1).
> 10. **The claim is heartbeated from the moment it is written.** `orchestrator.heartbeating(
>     memory, run_id, interval_s)` is the one heartbeat loop; the orchestrator wraps `run()` in it
>     and the API wraps its wait for a concurrency slot in it, so a queued run cannot go stale
>     (finding 2).
> 11. **The most recent resolved retry outranks the counts.** `PriorHistory.last_retry_outcome`
>     (A.11, additive) carries it; `likely_flaky` needs it to be `passed_on_retry`, the
>     `memory_agreement` bonus is withheld while it is `failed_again`, and the rendered prior says
>     which. Open Risk 7's self-correction now exists (finding 3).
> 12. **`upsert_signature(key, verdict | None, run_id)`.** A `None` verdict counts the sighting
>     without tallying a verdict. The Diagnostician passes `None` for a verdict below the
>     remediation gate's threshold (`build_agents(escalation_threshold=...)`), so refused verdicts
>     dilute the dominant share instead of building it; the observation row still carries every
>     verdict with its confidence (finding 4).
> 13. **A rerun is judged on all of its jobs.** The GitHub gateway asks for `per_page=100` and the
>     probe stays `pending` when `total_count` exceeds the jobs it saw (finding 5).
> 14. **pytest's section header is an anchor line** (`^_{3,}\s.+\s_{3,}$`), and the fingerprint's
>     fallbacks read only the failed test's own section (finding 6).
> 15. **`history.action_observation` is the one rule for what counts as an action**, used by the
>     Remediator in-run and by the approval route after an approved execution (finding 7).
> 16. **The `Redactor` scrubs through base64.** A string that is entirely base64 and decodes to
>     UTF-8 text is scrubbed as text and re-encoded only if something was removed, so `content_b64`
>     at rest (and everywhere else the redactor runs) cannot hide a credential (finding 8).
> 17. **`scripts/replay.py` degrades like the API** -- unclaimed on a failed claim, outcome printed
>     before a failed save is reported (finding 9).

---

## Phase 4 — Evaluator + Recovery

**Goal of the slice:** the Remediator can no longer act on a claim the log does not support, and a
malformed model response no longer kills a run.

### Built

`harness/evaluator.py` + `integrations/cicd/claim_checkers.py`; escalation channel (`escalation`
table + optional outbound webhook); `scripts/eval.py` and the fourth fixture (`dependency_break`).

> **Amendment (recorded 2026-09-11).** This list used to include "`harness/recovery.py` wired into
> every agent call". **Recovery shipped in Phase 1** — it is complete and wired at
> `src/harness/agent.py:234`, and Phase 1 spent three audit rounds on its failure classification
> (the terminal finish-reason set, `MAX_RETRY_AFTER_S`, `RETRY_DELAY_BUDGET_S`). What remains for
> this phase is the Recovery **Verify** block below, which is still owed: step 3's
> `HARNESS_FAULT_INJECT` paths have no coverage yet.
>
> Also from Phase 1: `evaluation.verdict` reaching a real value here is what lets Phase 2's
> `open-fix-pr` rule drop `skipped` from its `when` clause (see that phase's amendment 2).

> **Amendment (Phase 4, recorded 2026-09-14 — what was built, and how the Verify block reads
> against it).** Full reasoning in `docs/progress/phase-4/dispatch.md`.
>
> 1. **The Evaluator is a fourth stage, `evaluate`, with a deterministic agent.** Between
>    `diagnose` and `remediate`; `final` gains `evaluation` (A.1's list, complete now). The
>    agent (`integrations/cicd/agents/evaluator.py`, key `evaluator`) builds one `Claim` per
>    `Citation`, runs `harness.evaluator.Evaluator` over `claim_checkers.build_claim_checkers()`,
>    records one `evaluation.claim` span per verdict, re-calibrates the diagnosis with the
>    evaluator's row, and writes memory. `evidence_refuted` on `fail` is the gate on `remediate`,
>    where Phase 1 put it.
> 2. **Memory is written after evaluation, not by the Diagnostician.** Phase 3's decision 9 is
>    amended: a verdict the evaluator refutes must not be tallied at its pre-penalty confidence,
>    so the sighting, the observation and the `verdict_threshold` comparison move to the evaluate
>    stage and read the post-penalty figure. The Diagnostician no longer takes `memory` or
>    `verdict_threshold`.
> 3. **The evaluate stage re-files the diagnosis.** `final_confidence` is one sum clamped once, so
>    the stage re-calibrates from `self_confidence` with the Diagnostician's signals plus
>    `evidence_fully_verified` (+0.05, every claim verified) or `evidence_refuted` (−0.15, any
>    refuted) and replaces `artifacts["diagnosis"]`. `EvaluationReport.confidence_delta` says
>    what changed.
> 4. **The verdict rule, read literally.** `skipped` when there are no claims (delta 0;
>    `no_citations` already prices it); `fail` on any `refuted` (−0.15) or when
>    `verified/total < 0.5` with `total` every claim, unverifiable included (delta 0 — nothing was
>    shown fabricated; the gate's reason quotes the report's); `warn` on any `unverifiable`; `pass`
>    otherwise (+0.05). A run whose only citations name a missing artifact therefore fails on the
>    share, not on a refutation; the escalation reason is still `evidence_refuted` because A.1's
>    reason list has no other member for it (backlog).
> 5. **`warn` downgrades in `remediation.decide_plan` through `guardrails.downgrade_for_warn`**:
>    `allow` → `require_approval` (`downgraded_from="allow"`, on the `policy.decide` span),
>    `require_approval` and `deny` unchanged, read tools untouched.
> 6. **`skipped` leaves `policy.yaml`, `warn` enters it** (Phase 2 amendment 2 closed): both
>    write rules read `evaluation.verdict: {in: [pass, warn]}`. A diagnosis with no citations
>    matches neither and is denied by name; `file-ticket` still matches. `EVALUATION_SKIPPED`
>    remains the fact for a run with no `evaluation` artifact.
> 7. **The Remediator and the approval route read the run's real verdict** through
>    `remediation.evaluation_verdict_of` — the live artifact in-run, `final["evaluation"]` at
>    approval time — so a plan judged under `warn` is judged under `warn` again.
> 8. **The webhook is `harness/escalation.py::WebhookNotifier`** (a layout addition): B.4's 5 s,
>    2 retries, exponential backoff, never raises; body `{"text", "run_id", "escalation_id",
>    "reason", "message", "payload", "trace_url"}` scrubbed through the recorder's `Redactor`;
>    `delivery_error` built from the exception class and status, never from `str(exc)`, so the
>    URL — a secret — is never stored or logged. `Orchestrator._escalate` is async and awaits
>    the configured notifier; the approval route delivers through `AppContext.deliver_escalation`.
>    `EscalationRecord` gains `delivery_error: str | None = None` (A.1, additive); with a webhook
>    configured `delivered_at` is the webhook's acceptance time (`None` with `delivery_error`
>    set on failure), otherwise the log line's as before; `save_run` writes both onto the row.
>    `HARNESS_ESCALATION_WEBHOOK_URL` set (non-blank) adds `webhook` to the channels.
> 9. **Fault injection: one parser, one guard, two homes.** `harness/faults.py` parses
>    `HARNESS_FAULT_INJECT` (`name` or `name:N`); `deps.build_fault` refuses it outside `dev` and
>    refuses any name outside `KNOWN_FAULTS` (`sqlite_locked`, `llm_bad_json`, `llm_429`,
>    `diagnostician_fabricate_citation`); a blank value is no fault. `FaultInjectingLlmClient`
>    is built **per run** and answers the first N requests **per agent** (keyed by the request's
>    schema) with non-JSON text or `LlmRateLimited(retry_after_s=None)` without calling the
>    provider. `diagnostician_fabricate_citation` reaches `Diagnostician(fabricate_citation=True)`
>    through `build_agents` and replaces the model's citations with one `quote_exists` quoting
>    `AssertionError: expected 42`. `docker-compose.yml` forwards the variable.
> 10. **`MAX_TOKENS` raises the output budget ×1.5** through `recovery.OutputBudget`, an additive
>     `retry_structured(output_budget=...)` keyword the `LLMAgent` closure shares with the loop;
>     capped at 65 536; every `llm.attempt` span records `max_output_tokens`. Without a budget
>     the Phase 1 "answer more briefly" path stands.
> 11. **`DiffSummary.commit_shas: list[str] = []`** (A.11, additive) is read off the compare
>     response's `commits[].sha`; `commit_in_range` checks it plus `head_sha` by prefix, and reads
>     an empty list as "range unknown" (`unverifiable`), never as "not in range".
> 12. **The Diagnostician prompt is version 3**: it says what `quote` and `locator` hold per
>     `claim_kind`, so the checkers parse what the model was told to write.
> 13. **`scripts/eval.py`** runs in-process, one temporary database per run (first-sighting
>     labels), `--shared-db` to exercise the cap under concurrency, `--llm stub` from
>     `tests/stubs.ScenarioStubLlm` (the report's `llm` field says which), `--price-in/--price-out`
>     for cost (unpriced otherwise, and the report says so). Exit 1 on the gate PLAN names.
> 14. **The Verify block, as it can honestly be met.** Step 2's `stages` is
>     `["investigator","diagnostician","evaluator",null]` — the evaluate stage is real and the gated
>     remediate stage records `agent: null`, as it always has; "no remediator" holds. Step 3's
>     `attempts: 3` is per-agent (item 9); the `sqlite3` line reads `invalid_output|db` (Phase 3
>     files the row on the `db` channel; `channels` on the record lists `log`). Step 4's gate runs
>     under `--llm stub` for free; the README number needs `--llm gemini`.
> 15. **`hallucination` is not a fixture.** The refuted-citation path is the fault injection on
>     `real_regression`; the fourth fixture is `dependency_break` (pydantic 1.10.13 → 2.9.2, an
>     import-time `PydanticImportError`, `open-fix-pr` at ≥ 0.85).
>
> **Fix round (audit `docs/progress/phase-4/review.md`, same day).** What the five findings
> changed at contract level; the rest is in `backlog.md`.
>
> 16. **A `fail` report never tallies a signature verdict**, whatever the post-penalty
>     confidence: the evidence gate refuses the run, and a refused verdict is a sighting, not a
>     verdict (the Phase 3 finding-4 principle, applied to the second gate). Item 2's "the
>     tally depends on the threshold" reads "on either gate" (finding 1).
> 17. **The webhook URL is scrubbed from httpx's own request log.** httpx writes every request
>     URL at INFO on the `httpx` logger; the notifier installs a filter there that rewrites its
>     URL to the redaction placeholder, so `HARNESS_LOG_LEVEL=INFO` cannot print the credential
>     (finding 2).
> 18. **A version token drops trailing punctuation** (`1.10.13->2.9.2`, `to 2.9.2.`) before it
>     is compared with a `DependencyChange` — the same reason the 0.92 line exists: formatting
>     must not refute a true claim (finding 3).
> 19. **"Diff against the baseline" lists the commit range**, oldest first (or says it is
>     unknown), so the sha the v3 prompt tells the model to quote for `commit_in_range` is one
>     the model was shown (finding 4).
> 20. **`scripts/eval.py` builds its store through `AppContext.__post_init__`**, so
>     `HARNESS_FAULT_INJECT=sqlite_locked` reaches it as it does in the API (finding 5).

### Evaluator

The Evaluator runs **between** Diagnostician and Remediator and validates every `Citation` in the
`Diagnosis` against the `FailureBundle` the Investigator actually produced. All checks are
deterministic — no second LLM call, because using a model to check a model just moves the
credulity.

| Claim kind | Check |
|---|---|
| `quote_exists` | Whitespace-collapsed substring search of `Citation.quote` in the log excerpt / diff patch named by `locator`. On miss, `difflib.SequenceMatcher.ratio() >= 0.92` against the best-matching line → `verified` with `detail="fuzzy"`; below → `refuted`. |
| `file_in_diff` | Path ∈ `{f.path for f in DiffSummary.files}` |
| `dependency_bump` | `(package, from_version, to_version)` ∈ `FailureBundle.dependency_changes`; `from_version=None` tolerated for a new dep |
| `test_in_log` | The test id appears on an anchor line of the log excerpt |
| `commit_in_range` | Sha ∈ the commit list between `base_sha` and `head_sha` |

Any claim whose supporting artifact is missing (e.g. `file_in_diff` when the diff could not be
fetched) is `unverifiable`, never `refuted` — absence of evidence is not evidence of hallucination.

Verdict: **`fail`** if any `refuted` or `verified/total < 0.5` → Remediator is skipped, run
escalates with reason `evidence_refuted` when a claim was refuted, `evidence_unverifiable` when
the share rule alone failed (Phase 5 amendment 5). **`warn`** if any `unverifiable` → the run proceeds but
every effect is downgraded one step (`allow` → `require_approval`, `require_approval` →
`require_approval`, `deny` stays `deny`). **`pass`** otherwise.

**Decision (deterministic claim checking, not an LLM judge).** It is fast, free, reproducible, and
its failures are explainable to a reviewer. This is also the most interesting component in the
project from an interview standpoint — "how do you keep the model honest" has a code answer here.
Next-simplest alternative: an LLM-as-judge pass — deferred; the fuzzy-match threshold covers the
paraphrase case that would motivate it.

**Decision (the fuzzy 0.92 threshold).** Models reliably normalize whitespace and truncate long
lines when quoting. A strict `in` check refutes correct diagnoses on formatting alone, which is
worse than the alternative failure. 0.92 is tight enough that a fabricated quote does not pass;
the eval set measures whether that holds.

### Recovery

`harness/recovery.py::retry_structured(call, schema, policy)` distinguishes failure classes and
retries only what retrying can fix:

| Failure | Behaviour |
|---|---|
| `pydantic.ValidationError` on the response | Retry, up to 3 attempts total. Attempt 2 appends `<previous_output>{raw}</previous_output><validation_errors>{json of e.errors()}</validation_errors>` and "Return only JSON conforming to the schema; fix exactly the listed errors." Attempt 3 additionally sets `temperature=0.0` and strips all optional fields from the requested schema. |
| Empty / non-JSON response | Same path as above |
| 429 / 503 / 504 from Gemini | Separate counter: 4 attempts, exponential backoff 0.5 → 8 s with full jitter, honouring `Retry-After` when present |
| 400 "request too large" | Re-run the Context Manager at `budget × 0.5`, retry once, record `context_downshift` in the trace |
| 401 / 403 from Gemini | No retry. Run fails with `escalation.reason = "config_error"`. The key is never echoed. |
| Timeout (60 s) | One retry at the same budget, then escalate `reason="llm_timeout"` |

After exhaustion: `AgentResult.status = "invalid_output"`, the orchestrator halts the pipeline and
writes an `escalation` row. Every attempt is its own child span, so the trace shows the retry.

**Decision (feed the validation error back to the model).** It is a materially higher fix rate than
a blind retry and costs one extra call. Next-simplest alternative: blind retry at higher
temperature. Rejected alternative: auto-repair the JSON with a coercion library — it silently
manufactures values, which is precisely what an evidence-grounded system must not do.

**Escalation channels:** always the `escalation` table and a structured log line; optionally an
outbound POST to `HARNESS_ESCALATION_WEBHOOK_URL` (a Slack Incoming Webhook URL works unchanged).
Delivery failures are recorded in `delivery_error` and never fail the run.
**Decision:** a generic outgoing webhook rather than a Slack SDK — no dependency, and any consumer
works. Alternative: `slack_sdk` — nicer formatting, one more secret to manage.

### Verify

```powershell
# 1. A fabricated citation is caught and blocks remediation
uv run pytest tests/unit/test_evaluator.py -q
#    test_fabricated_quote_refuted: build a Diagnosis citing "AssertionError: expected 42"
#      against a bundle whose log contains no such line → verdict "fail", confidence_delta -0.15
#    test_paraphrased_quote_verified_fuzzy: same quote with collapsed whitespace → "verified"
#    test_missing_artifact_is_unverifiable_not_refuted

# 2. End to end: refuted evidence escalates and the Remediator never runs
$env:HARNESS_FAULT_INJECT="diagnostician_fabricate_citation"
curl.exe -s -X POST localhost:8000/v1/replay/real_regression | `
  jq '{status:.status, verdict:.final.evaluation.verdict, reason:.escalation.reason, stages:[.stages[].agent]}'
# EXPECT {"status":"escalated","verdict":"fail","reason":"evidence_refuted",
#         "stages":["investigator","diagnostician"]}     <-- no "remediator"

# 3. Recovery recovers
$env:HARNESS_FAULT_INJECT="llm_bad_json:2"     # first two calls return junk
curl.exe -s -X POST localhost:8000/v1/replay/flaky_test | jq '{status:.status, attempts:.stages[1].attempts}'
# EXPECT {"status":"completed","attempts":3}

$env:HARNESS_FAULT_INJECT="llm_bad_json:9"     # never recovers
curl.exe -s -X POST localhost:8000/v1/replay/flaky_test | jq '{status:.status, reason:.escalation.reason}'
# EXPECT {"status":"escalated","reason":"invalid_output"}
sqlite3 ./data/harness.db "select reason, channel from escalation order by created_at desc limit 1;"
# EXPECT invalid_output|log

$env:HARNESS_FAULT_INJECT="llm_429:3"
curl.exe -s -X POST localhost:8000/v1/replay/flaky_test | jq -r '.status'      # EXPECT "completed"

# 4. The eval harness — the number that goes in the README
uv run python scripts/eval.py --runs 5 --concurrency 1
# EXPECT eval_report.json with:  category_accuracy == 1.00 (20/20),
#        forbidden_actions_executed == 0, escalation_rate reported,
#        p50/p95 latency, mean tokens, estimated cost per run.
# CI gate: scripts/eval.py exits 1 if accuracy < 1.0 or forbidden_actions_executed > 0.
```

**Deferred:** trace view, webhooks, PR writing, second adapter.

---

## Phase 5 — Observability trace view + real GitHub webhook

**Goal of the slice:** the decision trail is visible to someone who is not reading the JSON, and a
real repo can drive it.

### Built

Jinja2 trace view at `GET /runs/{run_id}/view`; `GET /v1/runs/{run_id}/trace`;
`POST /webhooks/github` with HMAC verification, repo allowlist, and full idempotency; the demo repo
seeded with four workflows; PR-writing tools implemented (`create_branch`,
`create_or_update_file`, `open_pull_request`) behind the existing `require_approval` rule;
`scripts/record_fixture.py`.

### Trace model and view

Spans are OTel-shaped (`name`, `parent_span_id`, `attributes`, `status`, timings) even though they
are stored in SQLite, so exporting to a real collector later is a writer swap rather than a
remodel. One span per: run, stage, agent attempt, LLM call, tool call, policy decision, memory
query, evaluator check.

The view is a single server-rendered page: a header (run id, status, total latency, total tokens,
estimated cost, degraded components), a waterfall of spans with durations, and per-stage cards
showing conclusion, confidence with each adjustment itemized, citations with their verify verdicts,
and the policy decision with the matching rule quoted from `policy.yaml`.

**Decision (Jinja2 + ~100 lines of CSS, no JS build).** The artifact being demonstrated is the
decision trail; a React SPA would add a toolchain, a build step, and a second thing to review
without making the trail clearer. Next-simplest alternative: JSON only + `jq` — sufficient for me,
useless for a screenshot in a portfolio README.

**Decision (SQLite as the trace store).** One storage engine for the whole service. Alternative:
OpenTelemetry → Grafana Tempo / Honeycomb — more impressive on paper, adds a hosted dependency and
a second place to look. Recorded as a stated migration path instead.

### Secrets never reach the trace — three independent mechanisms

1. **`SecretStr` in `Settings`** — accidental `repr`/f-string interpolation yields
   `SecretStr('**********')`.
2. **`SecretRegistry`** — at startup, every `Settings` field whose *name* matches
   `(token|key|secret|password|dsn|credential|webhook_url)` has its resolved value registered. The
   `Redactor` does a literal replacement of each registered value with `***REDACTED***` in every
   string written to a span, a log line, or an escalation payload.
3. **Regex scrub** applied after (1) and (2), for secrets that never passed through `Settings`
   (e.g. a token pasted into a CI log by a careless workflow):
   `ghp_[A-Za-z0-9]{36}`, `github_pat_[A-Za-z0-9_]{22,}`, `gh[oprsu]_[A-Za-z0-9]{36,}`,
   `AIza[0-9A-Za-z_-]{35}`, `(?i)bearer\s+[A-Za-z0-9._-]{20,}`,
   `-----BEGIN [A-Z ]*PRIVATE KEY-----`, `(?i)(api[_-]?key|token|password)\s*[:=]\s*\S{8,}`.

Structurally, the gateway strips `Authorization`, `Cookie`, and `X-Hub-Signature-256` from request
headers before they reach a span, and no span ever carries `os.environ`.

### Webhook

`POST /webhooks/github`: verify `X-Hub-Signature-256` with `hmac.compare_digest` against
`HARNESS_GITHUB_WEBHOOK_SECRET` (reject unsigned with 401); accept only
`X-GitHub-Event: workflow_run` with `action == "completed"` and `conclusion == "failure"` (anything
else → 204); check `repository.full_name` against `HARNESS_ALLOWED_REPOS` (else 403); compute the
idempotency key; return **202 immediately** and process in a background task, well inside GitHub's
10 s delivery timeout.

### Verify

```powershell
# 1. Trace view renders and shows the full decision trail
$rid = (curl.exe -s -X POST localhost:8000/v1/replay/real_regression | jq -r '.run_id')
Start-Process "http://localhost:8000/runs/$rid/view"
# EXPECT: waterfall with >= 12 spans; every stage shows confidence + itemized adjustments;
#         the policy card quotes rule "open-fix-pr" and effect "require_approval".
curl.exe -s "localhost:8000/v1/runs/$rid/trace" | jq '[.spans[].component] | unique'
# EXPECT ["agent","context_manager","evaluator","gateway","guardrails","llm","memory","orchestrator"]

# 2. No secret ever lands anywhere (run this before making the repo public)
uv run pytest tests/test_no_secret_leak.py -q
#    Sets HARNESS_GITHUB_TOKEN=ghp_SENTINELSENTINELSENTINELSENTINEL01,
#    HARNESS_GEMINI_API_KEY=AIzaSENTINELSENTINELSENTINELSENTINEL02, runs all four
#    fixtures (with a log fixture that itself contains a pasted token), then asserts
#    the sentinels appear ZERO times in: every trace_span row, every escalation row,
#    captured stdout/stderr, and the raw bytes of harness.db.

# 3. Webhook: signature enforcement
curl.exe -s -o nul -w "%{http_code}" -X POST localhost:8000/webhooks/github -H "X-GitHub-Event: workflow_run" -d '{}'
# EXPECT 401
uv run python scripts/replay.py --post-signed fixtures/scenarios/flaky_test/webhook.json
# EXPECT 202, then GET /v1/runs/<id> shows status completed

# 4. Idempotency: redelivery does NOT run twice
uv run python scripts/replay.py --post-signed fixtures/scenarios/flaky_test/webhook.json  # -> run_A
uv run python scripts/replay.py --post-signed fixtures/scenarios/flaky_test/webhook.json  # same payload
# EXPECT second response {"status":"deduplicated","run_id":"run_A","original_run_id":"run_A"}
sqlite3 ./data/harness.db "select count(*) from run where idempotency_key='<key>';"   # EXPECT 1
sqlite3 ./data/harness.db "select count(*) from observation where run_id='run_A';"    # EXPECT 1

# 5. Live end-to-end   (deploy to the Space; see Context)
gh workflow run flaky.yml -R <you>/harness-demo-repo
#   In GitHub → Settings → Webhooks → Recent Deliveries: EXPECT 202.
#   Then use "Redeliver" on that same delivery: EXPECT 202 and NO second run in /v1/runs.
curl.exe -s https://shakti-agent-harness.hf.space/v1/runs | jq '.[0] | {status, category: .final.diagnosis.category}'
```

> **Amendment (Phase 5, recorded 2026-09-14 — what was built, and how the Verify block reads
> against it).** Full reasoning in `docs/progress/phase-5/dispatch.md`.
>
> 1. **Three components had never written a span, and the trace model's `stage` span was
>    missing.** A replay trace carried `agent, evaluator, guardrails, llm, orchestrator`;
>    Verify step 1 expects eight. Built: a `stage` span per stage (component `orchestrator`,
>    opened around the gate check and the agent alike; `agent.run` now parents to it),
>    `gateway.invoke` spans from `ReplayToolGateway(recorder=...)` in the live gateway's shape,
>    `memory.lookup` / `memory.upsert_signature` / `memory.record_observation` /
>    `memory.update_observation_outcome` spans from `SqliteMemoryStore(recorder=...)` (the run,
>    approval and heartbeat bookkeeping is not traced), and `ContextManager.assemble_traced`
>    (A.3, additive) writing one `context.assemble` span with every section's
>    `TruncationReport`.
> 2. **The view renders the bodies the JSON routes serve.** `GET /runs/{run_id}/view` builds
>    its context (`src/api/trace_view.py`) from `_serialize_run_outcome` -- digested and
>    scrubbed, exactly `GET /v1/runs/{id}` -- and the scrubbed `TraceResponse`, so a secret
>    that cannot reach the JSON cannot reach the HTML. Citations join their verdicts by index
>    (one claim per citation, in order); the evaluate card shows the report's `reason`
>    verbatim; the decision card quotes the rule off the loaded `PolicyEngine`, as YAML, and
>    shows `downgraded_from`; the escalation shows `delivery_error`. Jinja2, autoescape on,
>    ~100 lines of CSS, no JS. An unknown run is `404 application/problem+json`.
> 3. **The webhook's acceptance depends on the deployment's gateway.** Live
>    (`HARNESS_GATEWAY=github`): `HARNESS_ALLOWED_REPOS`, else `403`. Replay: the delivery
>    must carry the same Appendix C key as a recorded scenario's `webhook.json` -- the
>    recorded scenarios *are* a replay deployment's allowlist -- else `403` saying so. That is
>    what lets steps 3-4 post a fixture's webhook at a replay server and get a run. The
>    signature is verified over the raw bytes before the body is parsed; a blank
>    `HARNESS_GITHUB_WEBHOOK_SECRET` verifies nothing (every delivery `401`, one warning). A
>    GUID-shaped `X-GitHub-Delivery` rides in `requested_by` as `webhook:github:<guid>`; no
>    header reaches a span, a log line or a problem document. Pure helpers in
>    `src/api/webhook.py`; the route reuses `POST /v1/runs`'s claim and background path.
> 4. **Every catalog write has a body** in both gateways: `create_branch`,
>    `create_or_update_file`, `open_pull_request`, `create_issue` beside `rerun_failed_jobs`,
>    each with Appendix C's rule (existing branch fetched and returned `cached=True`; the
>    current blob `sha` on the PUT and a `409` as a hard stop, `ToolError(kind="unknown",
>    http_status=409)` -- A.4 has no `conflict` kind and no caller branches on one; an open PR
>    for the head returned instead of a second; an open issue carrying
>    `<!-- harness:signature:{id} -->` commented on, the id riding on the call as
>    `signature_id`, set by the harness). A `422` "already exists" is read off `message` *and*
>    `errors[].message` -- GitHub puts the duplicate-PR phrase under the latter. Dry run
>    performs the pre-checks and answers `would_have` without `content_b64`. Approving a stored
>    fix-PR plan now executes three dry-run calls and completes the run.
> 5. **`evidence_unverifiable`** (A.1, additive; Phase 4 audit S2): a `fail` report with
>    `refuted == 0` escalates it; any refuted claim is still `evidence_refuted`.
> 6. **The pattern set is PLAN's**, plus the PEM header widened to the whole block when its
>    `END` line is present. The `(api[_-]?key|token|password)\s*[:=]\s*\S{8,}` shape can
>    redact a drafted file's `password=${VAR}` line at the boundary -- a false positive
>    accepted for the barrier it buys (backlog).
> 7. **`cold_start` has its own run id (`501234891`).** It was a copy of `flaky_test`'s
>    webhook, so by Appendix C the two were one delivery and the route matched the first
>    alphabetically. `fixtures/README.md`'s checklist now requires a distinct key per
>    scenario. The stub answers `cold_start` as it answers `flaky_test`.
> 8. **The token pasted into a fixture log** is `real_regression`'s (hand-written, not
>    regenerated by `gen_fixture_log.py`), a `curl -H "Authorization: token ghp_…"` line in
>    the setup noise, listed in `scripts/scrub_fixtures.py::PLANTED_SENTINELS` so the scrub
>    leaves it and the leak test imports it from one place.
> 9. **`scripts/record_fixture.py` makes no model call**: `Investigator.collect` (additive)
>    drives the deterministic collection over a recording wrapper around the live gateway;
>    `gateway_replay.fixture_slug_for` is the one slug rule both sides use. The recorded
>    `scenario.yaml` carries `commit`, `baseline_kind`, `cold_start`, and a commented block
>    for the label keys a person fills in.
> 10. **The Verify block, as this phase can honestly meet it.** Step 1's page and component
>     list are pinned in-process (`tests/integration/test_trace_view.py`); the literal step
>     needs three live calls. Step 2 is `tests/test_no_secret_leak.py`, which also scans every
>     served JSON and HTML body and the escalation webhook's own request. Step 3's `401` is
>     free; its signed delivery is three live calls. Step 4's second delivery is free. Step 5
>     needs the demo repository, a fine-grained PAT, the webhook, and a Space redeploy with
>     `HARNESS_GATEWAY=github` and an allowlist -- user-gated, and a change to the Space's
>     exposure. `verify.md` records which steps ran live and when.
>
> **Fix round (recorded 2026-09-14, after the independent audit -- `docs/progress/phase-5/
> review.md`, nine findings; coordinator-verified per the standing one-audit-per-phase rule,
> `backlog.md` "Audit provenance"):**
>
> 11. **The pattern set has two tiers, and an execution input is never rewritten silently**
>     (finding 1, HIGH). Item 6's "false positive at the boundary" was wrong about the
>     boundary: the assignment shape ran through the base64 scrub at rest, rewrote ordinary
>     source lines inside a stored approval's `content_b64` (`DB_PASSWORD = …`,
>     `Client(api_key=…)`), and `POST /v1/approvals/{id}` committed the rewritten file.
>     Now `Redactor(registry, patterns, heuristic_patterns=…)`: the credential *shapes*
>     (vendor prefixes, PEM) apply everywhere, a base64 body included; the assignment
>     *heuristics* (`deps.HEURISTIC_SECRET_PATTERNS`) apply to plain text only -- log
>     lines, span attributes, served bodies, fixture files. And `execute_plan` refuses any
>     tool call whose arguments carry the placeholder (`harness.observability.
>     carries_redaction`, through base64 too) as `ToolError(kind="invalid_args",
>     retryable=False)` -- a plan the scrub altered is not the plan a person approved, and
>     the run escalates `tool_failure` rather than committing `***REDACTED***`.
> 12. **Appendix D's chain is built** (finding 2, HIGH): `branch_green` → `default_green`
>     (the same `find_last_successful_run` on `repository.default_branch`, which
>     `parse_subject` now carries) → `head_commit_only` (`get_commit`; the commit's files
>     as the diff, its first parent as `base_sha`, `cold_start=True`, and the prompt block
>     Appendix D specifies rendered above the diff) → `none` (no parent, or `get_commit`
>     failed -- the latter also a degraded `diff`). `tests/unit/test_baseline.py` is the
>     suite Appendix D names. Fixture consequences: the replay slug for a workflow-runs
>     listing carries `-branch-<name>` (Appendix D asks the same path twice with a different
>     `branch=`; `fixtures/README.md`'s suffix rule), the five recordings are renamed
>     accordingly, and `cold_start` gains the head commit as a history root
>     (`parents: []`) so its expectation stays `baseline_kind: none`. The seed script's
>     branch-push scenarios (`demo/regression`, `demo/dependency`) now resolve to
>     `default_green` and can be the `real_regression` / `dependency_break` step 5 expects.
> 13. **Log lines are scrubbed at the record factory** (finding 4, MEDIUM):
>     `install_log_redaction(redactor)`, installed once by `AppContext.__post_init__`,
>     pre-formats every log record in the process and passes it through the `Redactor`
>     -- ours, `httpx`'s, `aiosqlite`'s alike. The leak test's fifth stage is a live-mode
>     write whose `403` echoes the token and escalates `tool_failure`; with the factory
>     off it fails on the two log lines the audit named. The formatted traceback of an
>     `exc_info` is produced later by the formatter and is not covered (backlog).
> 14. **`seed_demo_repo.sh --force` re-seeds on top of `main`** (finding 3, MEDIUM): the
>     generated tree becomes one commit whose parent is the fetched `main` (a fast-forward,
>     never `push --force` on `main`); only the two `demo/*` branches -- the script's own
>     -- are replaced. `--webhook-only` registers the webhook and touches no ref; the
>     printed next step uses it. The header states exactly this.
> 15. **Smaller:** the webhook's ignore log line carries a verdict *code*
>     (`not_workflow_run`, `not_completed`, `not_failure`), never the header or `action`
>     value (finding 5); `open_pull_request` reconciles the labels an existing PR lacks on
>     the pre-check path and, when the label call fails after the PR was created, names the
>     PR in the error (finding 6); `create_issue`'s marker search sends `labels=` and pages
>     to `ISSUE_SEARCH_MAX_PAGES` (finding 7); `scenario_yaml` offers `commit:` as a
>     commented hint -- it is the label `Diagnosis.suspected_commit_sha`, `null` for a
>     flaky or infra failure -- and notes a tail-capped log (finding 8); `1e400` in a
>     numeric field is `400`, not `500` (`OverflowError` caught beside `ValueError`, finding
>     9); `tests/unit/test_fixture_delivery_keys.py` pins one Appendix C key per recorded
>     scenario. A.4's `create_issue` row now lists `signature_id`; the A.2 gate sample and
>     the Phase 4 prose say `evidence_unverifiable` where item 5 changed them.
> 16. **Recorded, not changed:** `ReplayToolGateway.invoke` opens its `gateway.invoke` span
>     *before* the forbidden re-check, where dispatch decision 2 said after. The span is
>     local bookkeeping, not an outbound request -- A.4's "before anything else" is about
>     the wire -- and a refused call *should* leave a span (`tests/unit/
>     test_gateway_replay_writes.py` pins one). The live gateway does the same. Decision 2
>     is amended to "the span wraps the re-check; the refusal costs zero requests".

**Deferred:** the second adapter.

---

## Phase 6 — Second Tool Gateway adapter (stretch)

**Goal of the slice:** make the separation claim falsifiable rather than rhetorical.

**Built:**
- `tests/contract/test_tool_gateway_contract.py` — one abstract conformance suite, parametrized over
  **every** gateway implementation: catalog well-formedness, `side_effect` declared on every tool,
  forbidden tools rejected regardless of the decision passed in, errors returned rather than raised,
  timeouts surfacing as `ToolError(kind="timeout")`, idempotency keys honoured on write tools,
  `ToolResult.call_id == ToolCall.call_id`.
- `src/integrations/incident/` — `schemas.py` (`IncidentBundle`, `IncidentDiagnosis` with taxonomy
  `deploy_regression | dependency_outage | capacity | config_drift | noisy_alert`), `policy.yaml`
  (auto-allowed: acknowledge alert, pull recent deploys; approval: roll back a deploy, page a
  secondary; forbidden: silence a monitor, scale to zero), and `gateway_incident.py` implementing
  `ToolGateway` against a fake Sentry/PagerDuty with `NotImplementedError` bodies but a real,
  complete `catalog()`.
- `docs/ADAPTER_GUIDE.md` — the checklist for adding a domain, and a table of exactly what changed.

**The claim to be proven:** adding the second domain touches **zero lines under `src/harness/`**.

**Verify:**
```powershell
uv run pytest tests/contract -q
# EXPECT the same ~14 conformance tests pass for GitHubToolGateway, ReplayToolGateway,
#        and IncidentToolGateway — 42 passed.

git diff --stat <phase5-tag>..HEAD -- src/harness/
# EXPECT: empty output.  If it is not empty, the abstraction leaked; the diff names where.

uv run pytest tests/test_layering.py -q     # still passes with two integrations present
```

**Deferred:** any real incident-tooling integration.

---

# Appendix A — Interfaces (every contract in one place)

All models are `pydantic.BaseModel` with `model_config = ConfigDict(extra="forbid",
frozen=True)` unless noted. Types shown are exact.

```mermaid
flowchart LR
  WH[POST /webhooks/github] -->|RunRequest| ORCH[Orchestrator]
  ORCH -->|ContextRequest| CM[ContextManager]
  CM -->|ContextBundle| ORCH
  ORCH -->|AgentInput| INV[Investigator]
  INV -->|ToolCall| GW[ToolGateway]
  GW -->|ToolResult| INV
  INV -->|AgentResult FailureBundle| ORCH
  ORCH -->|MemoryQuery| MEM[(MemoryStore)]
  ORCH -->|AgentInput| DIAG[Diagnostician]
  DIAG -->|AgentResult Diagnosis| ORCH
  ORCH -->|Diagnosis + FailureBundle| EVAL[Evaluator]
  EVAL -->|EvaluationReport| ORCH
  ORCH -->|ActionContext| POL[PolicyEngine]
  POL -->|PolicyDecision| ORCH
  ORCH -->|AgentInput| REM[Remediator]
  REM -->|RemediationPlan| ORCH
  ORCH -->|ToolCall + PolicyDecision| GW
  ORCH -->|Span| TR[(TraceRecorder)]
  ORCH -->|RunOutcome| API
```

## A.1 Harness core — `src/harness/contracts.py`

```python
RunId = Annotated[str, StringConstraints(pattern=r"^run_[0-9A-HJKMNP-TV-Z]{26}$")]  # ULID

class RunRequest(BaseModel):
    integration: str                       # "cicd"
    subject: dict[str, JsonValue]          # opaque to the harness; the integration parses it
    idempotency_key: str = Field(min_length=8, max_length=128)
    mode: Literal["live", "replay"] = "live"
    replay_fixture: str | None = None
    requested_by: str = "system"

class Evidence(BaseModel):
    evidence_id: str                       # "ev_" + 12 hex
    source: Literal["log", "diff", "config", "memory", "tool"]
    locator: str                           # "log:job/2001#L512-531" | "diff:requirements.txt@+12"
    excerpt: str = Field(max_length=2000)
    sha256: str                            # of excerpt, normalized; used by the Evaluator

class TokenUsage(BaseModel):
    prompt: int = 0
    completion: int = 0
    thinking: int = 0
    total: int = 0
    estimated_cost_usd: float = 0.0

class AgentError(BaseModel):
    kind: Literal["invalid_output","llm_timeout","llm_rate_limited","llm_auth",
                  "llm_upstream","tool_error","internal"]
    message: str                           # redacted before storage
    attempts: int
    detail: dict[str, JsonValue] = {}

TOut = TypeVar("TOut", bound=BaseModel)

class AgentResult(BaseModel, Generic[TOut]):
    agent: str
    status: Literal["ok", "invalid_output", "tool_error", "timeout", "escalate"]
    output: TOut | None
    confidence: float | None = Field(None, ge=0.0, le=1.0)   # post-calibration
    evidence: list[Evidence] = []
    attempts: int = 1
    latency_ms: int
    tokens: TokenUsage
    prompt_sha256: str | None = None
    model: str | None = None
    error: AgentError | None = None

class StageRecord(BaseModel):
    stage: str                             # "investigate" | "diagnose" | "evaluate" | "remediate"
    agent: str | None
    status: str
    started_at: datetime
    duration_ms: int
    attempts: int
    tokens: TokenUsage
    summary: str                           # one-line, for the trace view

class EscalationRecord(BaseModel):
    escalation_id: str
    reason: Literal["low_confidence","evidence_refuted","evidence_unverifiable",  # Phase 5
                    "invalid_output","llm_timeout",           # amendment, additive: the
                    "llm_upstream","config_error","policy_denied","tool_failure",  # share-rule
                    "cold_start_restricted","rate_limited","unknown_category",     # fail (Phase 4
                    "run_timeout"]                            # audit S2); Phase 3 amendment
    message: str
    payload: dict[str, JsonValue]
    channels: list[Literal["log","db","webhook"]]
    delivered_at: datetime | None
    delivery_error: str | None = None      # Phase 4 amendment, additive: B.4's "recorded in
                                           # escalation.delivery_error"; never carries the URL

class RunOutcome(BaseModel):
    run_id: RunId
    integration: str
    status: Literal["completed","escalated","awaiting_approval","failed","deduplicated","in_progress"]
    original_run_id: RunId | None = None       # set only when status == "deduplicated"
    created_at: datetime
    completed_at: datetime | None
    duration_ms: int | None
    stages: list[StageRecord]
    degraded_components: list[str] = []
    total_tokens: TokenUsage
    final: dict[str, JsonValue]                # integration payload: bundle/diagnosis/evaluation/remediation
    escalation: EscalationRecord | None = None
    trace_url: str
```

## A.2 Orchestrator — `src/harness/orchestrator.py`

```python
class StageSpec(BaseModel):
    name: str
    agent_key: str
    output_model: type[BaseModel]          # not serialized
    required: bool = True
    gate: Callable[["RunState"], "GateDecision"] | None = None
    suspend: Callable[["RunState"], "Suspension | None"] | None = None   # runs AFTER the stage (Phase 2)

class GateDecision(BaseModel):
    proceed: bool
    reason: str
    escalate_as: str | None = None

class Suspension(BaseModel):               # Phase 2 amendment -- see the note below
    status: Literal["awaiting_approval", "escalated"]
    reason: str
    escalate_as: str | None = None         # an EscalationReason when status == "escalated";
                                           # missing or unknown is coerced to "unknown_category"
                                           # with a warning, never dropped
    payload: dict[str, JsonValue] = {}

class RunState(BaseModel):                 # mutable, extra="allow" — the only non-frozen model
    run_id: RunId
    request: RunRequest
    artifacts: dict[str, BaseModel]        # {"bundle": FailureBundle, "diagnosis": Diagnosis, ...}
    degraded: list[str]
    stages: list[StageRecord]

class Orchestrator:
    async def run(self, request: RunRequest) -> RunOutcome: ...
```

> **Amendment (Phase 2, 2026-09-11).** `StageSpec.suspend` and `Suspension` are derived, not
> transcribed: A.2 as frozen had no way for a stage's *output* to end the run early, and
> `AgentResult.status` (A.1) has no word for "valid, complete, and needs a person". The hook
> is the post-stage mirror of `gate`: the integration supplies a closure over `RunState`,
> the orchestrator sets the run status it names (escalating with `escalate_as` when asked),
> stops, and keeps the stage's artifact in `final`. Defaulted, so no existing caller changes.
> The CI/CD integration attaches it to the remediate stage: `awaiting_approval` for a plan
> that needs approval, `escalated`/`policy_denied` for one the policy refused.

The confidence short-circuit is a `StageSpec.gate` on the remediate stage:
```python
def remediation_gate(state: RunState) -> GateDecision:
    d = state.artifacts["diagnosis"]
    ev = state.artifacts.get("evaluation")
    if ev and ev.verdict == "fail":
        return GateDecision(proceed=False, reason=f"evaluator verdict fail: {ev.reason}",
                            # the report's reason travels; Phase 5 amendment 5: refuted
                            # claims are `evidence_refuted`, a share-rule failure alone is
                            # `evidence_unverifiable`
                            escalate_as="evidence_refuted" if ev.refuted > 0
                            else "evidence_unverifiable")
    if d.final_confidence < settings.escalation_threshold:      # 0.70
        return GateDecision(proceed=False,
                            reason=f"confidence {d.final_confidence:.2f} < {settings.escalation_threshold}",
                            escalate_as="low_confidence")
    if d.category == "unknown":
        return GateDecision(proceed=False, reason="unclassified", escalate_as="unknown_category")
    return GateDecision(proceed=True, reason="ok")
```

## A.3 Context Manager — `src/harness/context_manager.py`

```python
class ContextBudget(BaseModel):
    total_chars: int = 120_000
    anchor_window_lines: int = 20
    head_lines: int = 200
    tail_lines: int = 400
    reserve_chars: int = 8_000             # held back for prompt scaffolding + other sections

class ContextRequest(BaseModel):
    sections: list["Section"]
    budget: ContextBudget
    anchor_patterns: list[str]             # supplied by the INTEGRATION, not hardcoded here

class Section(BaseModel):
    key: str                               # "log", "diff", "config"
    content: str
    priority: int = Field(ge=0, le=10)     # 10 = trim last
    inviolable_ranges: list[tuple[int, int]] = []

class TruncationReport(BaseModel):
    original_chars: int
    kept_chars: int
    original_lines: int
    kept_lines: int
    anchors_found: int
    anchors_kept: int
    anchors_dropped: int
    elisions: list[tuple[int, int]]        # (start_line, end_line) of each elided range

class ContextBundle(BaseModel):
    text: str
    per_section: dict[str, str]
    truncation: dict[str, TruncationReport]
    estimated_tokens: int
    cold_start: bool = False

class ContextManager:
    def assemble(self, req: ContextRequest) -> ContextBundle: ...
    # Phase 5 amendment, additive: `ContextManager(recorder=...)` and `assemble_traced`, the
    # async twin that opens one `context.assemble` span (component `context_manager`) around
    # `assemble`. `assemble` stays synchronous and unrecorded.
    async def assemble_traced(self, req: ContextRequest) -> ContextBundle: ...
```

## A.4 Tool Gateway — `src/harness/gateway.py`

```python
class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, JsonValue]                     # JSON Schema
    side_effect: Literal["read", "write", "destructive"]
    idempotent: bool
    timeout_s: float = 30.0

class ToolCall(BaseModel):
    call_id: str                                           # "tc_" + 12 hex
    tool: str
    args: dict[str, JsonValue]
    idempotency_key: str | None = None                     # required when side_effect != "read"

class ToolError(BaseModel):
    kind: Literal["timeout","rate_limited","auth","not_found","malformed",
                  "forbidden_by_policy","upstream_5xx","invalid_args","unknown"]
    message: str                                           # redacted
    retryable: bool
    retry_after_s: float | None = None
    http_status: int | None = None

class ToolResult(BaseModel):
    call_id: str
    tool: str
    ok: bool
    data: dict[str, JsonValue] | None = None
    error: ToolError | None = None
    latency_ms: int
    attempts: int = 1
    cached: bool = False
    dry_run: bool = False

class ToolGateway(Protocol):
    integration: str
    def catalog(self) -> list[ToolSpec]: ...
    async def invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult: ...
    async def aclose(self) -> None: ...
```

**CI/CD catalog** (`GitHubToolGateway.catalog()`):

| Tool | side_effect | idempotent | Args |
|---|---|---|---|
| `list_workflow_run_jobs` | read | ✓ | `{run_id: int, attempt: int}` |
| `get_job_logs` | read | ✓ | `{job_id: int, max_bytes: int}` |
| `find_last_successful_run` | read | ✓ | `{workflow_id: int, branch: str, before: str}` |
| `compare_commits` | read | ✓ | `{base: str, head: str}` |
| `get_commit` | read | ✓ | `{sha: str}` |
| `get_file_contents` | read | ✓ | `{path: str, ref: str}` |
| `search_workflow_runs` | read | ✓ | `{workflow_id: int, branch, status, per_page}` |
| `rerun_failed_jobs` | write | ✓ | `{run_id: int, attempt?: int}` — `attempt` lets the gateway apply Appendix C's "already advanced" no-op |
| `create_branch` | write | ✓ | `{name: str, from_sha: str}` |
| `create_or_update_file` | write | ✓ | `{branch, path, content_b64, message, sha?}` |
| `open_pull_request` | write | ✓ | `{head, base, title, body, draft: true, labels}` |
| `create_issue` | write | ✓ | `{title, body, labels, signature_id?}` — `signature_id` set by the harness (Phase 5 amendment 4) |
| `merge_pull_request` | destructive | ✗ | registered **only** so the deny path is testable |

## A.5 Memory — `src/harness/memory.py`

```python
class SignatureKey(BaseModel):
    scope: str                             # "repo:owner/name"
    subject_key: str                       # integration-defined
    fingerprint: str

class SignatureRecord(BaseModel):
    signature_id: str
    scope: str
    subject_key: str
    fingerprint: str
    first_seen_at: datetime
    last_seen_at: datetime
    occurrences: int
    verdict_counts: dict[str, int]
    last_verdict: str | None
    last_run_id: RunId | None

class Observation(BaseModel):
    observation_id: str
    signature_id: str
    run_id: RunId
    occurred_at: datetime
    verdict: str
    confidence: float
    action_taken: str | None
    action_outcome: Literal["passed_on_retry","failed_again","pending"] | None
    commit_sha: str | None

class MemoryQuery(BaseModel):
    key: SignatureKey
    lookback_days: int = 30
    limit: int = 20

class MemoryHit(BaseModel):
    record: SignatureRecord | None                # None on a first-ever sighting
    recent: list[Observation]
    actions_in_window: dict[str, int]             # {"rerun_failed_jobs": 1} over 24 h
    unavailable: bool = False                     # True when the store is degraded

class MemoryStore(Protocol):
    async def lookup(self, q: MemoryQuery) -> MemoryHit: ...
    async def upsert_signature(self, key: SignatureKey, verdict: str, run_id: RunId) -> str: ...
    async def record_observation(self, obs: Observation) -> None: ...
    async def update_observation_outcome(self, observation_id: str, outcome: str) -> None: ...
    async def save_run(self, outcome: RunOutcome) -> None: ...
    async def claim_run(self, idempotency_key: str, integration: str) -> "RunClaim": ...
    async def heartbeat(self, run_id: RunId) -> None: ...

class RunClaim(BaseModel):
    acquired: bool
    run_id: RunId
    existing_status: str | None
    existing_outcome: RunOutcome | None
    took_over_from: RunId | None = None
```

> **Amendment (Phase 3, 2026-09-13).** Additive, recorded rather than decided silently
> (`docs/progress/phase-3/dispatch.md` decisions 4, 5, 7):
>
> - `MemoryStore` gains the read side the `run` table exists to serve — `get_run(run_id)`,
>   `list_runs(*, limit, status, cursor) -> (outcomes, next_cursor)` (keyset on `run_id`),
>   `list_escalations(*, limit) -> [(run_id, EscalationRecord)]` — and the approval side the
>   `approval` table exists to serve — `save_approval`, `get_approval`, `decide_approval` (single
>   use, `WHERE state='pending'`) over a harness model `ApprovalRecord{approval_id, run_id, state,
>   plan: dict, requested_at, expires_at, decided_at?, decided_by?, decision_note?, context: dict}`
>   whose `plan` and `context` are opaque JSON. The two in-process registries are deleted.
> - The `approval` table gains `context_json TEXT NOT NULL DEFAULT '{}'`: what the deciding
>   request needs to rebuild the run's gateway, an API-layer notion carried opaquely.
> - Three pure helpers: `signature_id_for(key)` (step 5's formula), `observation_id_for(
>   signature_id, run_id)` (one observation per signature per run, deterministic), and
>   `dominant_verdict(occurrences, verdict_counts, *, min_occurrences, min_share)` plus
>   `has_outcome(recent, outcome)` — the numeric half of the flakiness prior.
> - `SqliteMemoryStore.claim_run` mints the run id (`RunClaim.run_id`); `save_run` for a run
>   nobody claimed writes the row under `unclaimed:<run_id>`; a superseded row reads back through
>   `get_run` as `failed` with a `run_timeout` escalation (A.1 has no `superseded` status).
> - Every method raises `MemoryStoreError` once the B.3 ladder is exhausted; callers degrade.
> - `Orchestrator(memory=..., heartbeat_interval_s=15)` runs the Appendix C heartbeat for the
>   duration of `run()`; that is the orchestrator's only use of the store.
> - Fix round: `upsert_signature`'s `verdict` is `str | None` (`None` = a sighting without a
>   tallied verdict); `is_chain_member(key, candidate)` is the takeover-chain rule; the heartbeat
>   loop is the module-level `orchestrator.heartbeating(memory, run_id, interval_s)` context
>   manager so a caller can keep a claim alive *before* `run()`.

## A.6 Evaluator — `src/harness/evaluator.py`

```python
class Claim(BaseModel):
    claim_id: str
    kind: str                              # registered checker key
    payload: dict[str, JsonValue]

class ClaimVerdict(BaseModel):
    claim_id: str
    kind: str
    result: Literal["verified", "refuted", "unverifiable"]
    detail: str                            # "exact" | "fuzzy 0.94" | "path not in diff (12 files)"
    matched_locator: str | None = None

class EvaluationReport(BaseModel):
    verdicts: list[ClaimVerdict]
    verified: int
    refuted: int
    unverifiable: int
    verdict: Literal["pass", "warn", "fail", "skipped"]
    confidence_delta: float
    reason: str

class ClaimChecker(Protocol):
    kind: str
    def check(self, claim: Claim, artifacts: Mapping[str, BaseModel]) -> ClaimVerdict: ...

class Evaluator:
    def __init__(self, checkers: Sequence[ClaimChecker]) -> None: ...
    def evaluate(self, claims: Sequence[Claim], artifacts: Mapping[str, BaseModel]) -> EvaluationReport: ...
```

## A.7 Guardrails — `src/harness/guardrails.py`

```python
class Condition(BaseModel):
    eq: JsonValue | None = None
    ne: JsonValue | None = None
    in_: list[JsonValue] | None = Field(None, alias="in")
    nin: list[JsonValue] | None = None
    gte: float | None = None
    gt: float | None = None
    lte: float | None = None
    lt: float | None = None

class Rule(BaseModel):
    id: str
    tools: list[str]                       # exact names, "read:*", or "*"
    effect: Literal["allow", "require_approval", "deny"]
    when: dict[str, Condition] = {}        # dotted keys into ActionContext.facts
    obligations: list[str] = []

class PolicySpec(BaseModel):
    version: Literal[1]
    integration: str
    default_effect: Literal["deny"] = "deny"
    forbidden: list[str] = []
    rules: list[Rule]

class ActionContext(BaseModel):
    tool: str
    side_effect: Literal["read", "write", "destructive"]
    facts: dict[str, JsonValue]            # flat dotted: "diagnosis.category",
                                           # "diagnosis.final_confidence", "evaluation.verdict",
                                           # "memory.retries_for_signature_24h",
                                           # "context.cold_start", "run.side_effecting_actions_so_far"
class PolicyDecision(BaseModel):
    tool: str
    rule_id: str                           # "<default>" or "<forbidden>" when no rule matched
    effect: Literal["allow", "require_approval", "deny"]
    reason: str
    obligations: list[str] = []
    evaluated_at: datetime
    downgraded_from: str | None = None     # set when an Evaluator "warn" downgraded the effect

class PolicyEngine:
    def __init__(self, spec: PolicySpec) -> None: ...
    def decide(self, ctx: ActionContext) -> PolicyDecision: ...
```

Evaluation order: `forbidden` → hardcoded invariants → first matching rule in file order →
`default_effect`. First match wins, and the matching rule id is always reported.

## A.8 Recovery — `src/harness/recovery.py`

```python
class RetryPolicy(BaseModel):
    max_attempts: int = 3                  # structured-output failures
    transient_max_attempts: int = 4        # 429/503/504
    backoff_base_s: float = 0.5
    backoff_max_s: float = 8.0
    jitter: Literal["full", "none"] = "full"
    timeout_s: float = 60.0
    downshift_context_on_too_large: bool = True

class AttemptRecord(BaseModel):
    attempt: int
    outcome: Literal["ok","validation_error","transient","too_large","timeout","fatal"]
    error_summary: str | None
    latency_ms: int
    tokens: TokenUsage

class OutputBudget:                        # Phase 4 amendment (item 10), additive: the
    max_output_tokens: int                 # caller's `call` closure reads this; the loop
    def grow(self) -> int: ...             # grows it x1.5 on a MAX_TOKENS finish

async def retry_structured(
    call: Callable[[str], Awaitable["RawLlmResponse"]],
    prompt: str,
    schema: type[TOut],
    policy: RetryPolicy,
    recorder: TraceRecorder,
    *,
    output_budget: OutputBudget | None = None,   # Phase 4 amendment, additive
) -> tuple[TOut | None, list[AttemptRecord], AgentError | None]: ...
```

## A.9 Observability — `src/harness/observability.py`

```python
class Span(BaseModel):
    span_id: str
    parent_span_id: str | None
    run_id: RunId
    name: str
    component: Literal["orchestrator","context_manager","gateway","memory",
                       "evaluator","guardrails","agent","llm","api"]
    status: Literal["ok", "error"]
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None
    attributes: dict[str, JsonValue] = {}          # redacted at write time
    error: dict[str, JsonValue] | None = None

class TraceResponse(BaseModel):
    run_id: RunId
    spans: list[Span]
    totals: TokenUsage
    duration_ms: int
    degraded_components: list[str]

class TraceRecorder:
    @asynccontextmanager
    def span(self, name: str, component: str, **attrs) -> AsyncIterator["SpanHandle"]: ...

class Redactor:
    def __init__(self, registry: SecretRegistry, patterns: Sequence[re.Pattern]) -> None: ...
    def scrub(self, value: JsonValue) -> JsonValue: ...     # recursive over dict/list/str
```

## A.10 LLM client — `src/harness/llm.py`

```python
class LlmRequest(BaseModel):
    model: str
    prompt: str
    schema: dict[str, JsonValue] | None            # already Gemini-flattened
    temperature: float = 0.0
    max_output_tokens: int = 4096
    thinking_budget: int | None = None
    timeout_s: float = 60.0

class RawLlmResponse(BaseModel):
    text: str
    tokens: TokenUsage
    finish_reason: str
    model: str
    latency_ms: int

class LlmClient(Protocol):
    async def generate(self, req: LlmRequest) -> RawLlmResponse: ...

def to_gemini_schema(model: type[BaseModel]) -> dict[str, JsonValue]: ...
```

## A.11 CI/CD integration — `src/integrations/cicd/schemas.py`

```python
class JobRef(BaseModel):
    repo: str; workflow_name: str; workflow_id: int
    run_id: int; run_attempt: int; job_id: int; job_name: str
    head_sha: str; branch: str; event: str
    started_at: datetime; completed_at: datetime | None
    conclusion: str; runner_labels: list[str] = []

class LogExcerpt(BaseModel):
    job_id: int; total_lines: int; included_lines: int
    excerpt: str; anchor_line_numbers: list[int]
    truncation: TruncationReport

class FileChange(BaseModel):
    path: str
    status: Literal["added","modified","removed","renamed"]
    additions: int; deletions: int
    patch: str | None = None                       # None when GitHub omits it (binary/too large)

class DiffSummary(BaseModel):
    baseline_kind: Literal["branch_green","default_green","head_commit_only","none"]
    base_sha: str | None; head_sha: str
    commits_behind: int = 0
    files: list[FileChange] = []
    truncated: bool = False                        # GitHub caps compare at 300 files
    total_files: int = 0
    commit_shas: list[str] = []                    # Phase 4 amendment, additive: the range
                                                   # `commit_in_range` is checked against;
                                                   # empty = unknown, never "not in range"

class DependencyChange(BaseModel):
    ecosystem: Literal["pip","npm","go","maven","cargo","other"]
    manifest_path: str; package: str
    from_version: str | None; to_version: str | None
    source: Literal["manifest_diff","lockfile_diff"]

class PriorHistory(BaseModel):
    signature_id: str | None
    key: SignatureKey | None = None   # Phase 3 amendment: the lookup key, always computed by
                                      # the Investigator (pure), so later writers and the
                                      # approval-time re-query never recompute it
    occurrences: int = 0
    verdict_counts: dict[str, int] = {}
    last_verdict: str | None = None
    last_seen_at: datetime | None = None
    prior_hint: Literal["likely_flaky","likely_real","unknown"] = "unknown"
    retries_in_24h: int = 0     # conditional: unavailable=True fails closed at 999 (B.3),
                                # unconditionally — a supplied count does not override it,
                                # since a caller cannot both know the count and declare the
                                # history unreadable. Enforced on construction,
                                # model_validate and model_copy alike.
    last_retry_outcome: Literal["passed_on_retry","failed_again"] | None = None
                                # Phase 3 fix round (finding 3): the newest RESOLVED retry's
                                # result; failed_again withholds likely_flaky and the bonus
    sample_run_ids: list[str] = []
    unavailable: bool = False

class InvestigationNotes(BaseModel):          # the Investigator's LLM output
    observations: list[str] = Field(max_length=8)
    additional_tool_calls: list[ToolCall] = Field(max_length=3)   # read-only tools only
    narrative: str = Field(max_length=800)

class AdditionalToolCallOutcome(BaseModel):   # visibility only — carries no confidence signal
    tool: str
    outcome: Literal["obtained","refused","failed"]
    error: ToolError | None = None            # populated when outcome == "failed"
    reason: str = ""                          # why, when outcome == "refused" (no ToolError exists)

class FailureBundle(BaseModel):               # the Investigator's stage output
    job: JobRef
    logs: list[LogExcerpt]
    diff: DiffSummary
    dependency_changes: list[DependencyChange] = []
    prior_history: PriorHistory
    notes: InvestigationNotes | None = None
    cold_start: bool = False
    collected_at: datetime
    gateway_errors: list[ToolError] = []
    additional_tool_outcomes: list[AdditionalToolCallOutcome] = []

class Citation(BaseModel):
    claim_kind: Literal["quote_exists","file_in_diff","dependency_bump",
                        "test_in_log","commit_in_range"]
    locator: str                              # "log:job/2001" | "diff:requirements.txt"
    quote: str = Field(max_length=500)
    note: str = Field(default="", max_length=200)

# `Adjustment` is IMPORTED, not redeclared here — it is the same class
# `harness.confidence.calibrate()` returns, so a `Diagnosis.confidence_adjustments`
# list built from that function's output validates without a foreign-model coercion
# error. Two structurally identical Pydantic models are still two types.
from src.harness.confidence import Adjustment   # name: str; delta: float; reason: str

class Diagnosis(BaseModel):                   # the Diagnostician's stage output
    reasoning: str = Field(max_length=1200)   # FIRST via propertyOrdering — the model reasons before it concludes
    category: Literal["flaky_test","real_regression","dependency_break",
                      "infra_transient","config_issue","unknown"]
    summary: str = Field(max_length=280)
    self_confidence: float = Field(ge=0.0, le=1.0)
    citations: list[Citation] = Field(max_length=6)
    suspected_commit_sha: str | None = None
    suspected_test_ids: list[str] = []
    suspected_package: str | None = None
    suggested_action: Literal["retry","open_fix_pr","open_revert_pr","file_ticket","escalate"]
    # --- added by the harness after the model returns, not requested from the model ---
    final_confidence: float = 0.0
    confidence_adjustments: list[Adjustment] = []

class FilePatch(BaseModel):
    path: str; new_content: str; rationale: str

class PrDraft(BaseModel):
    branch: str                               # deterministic: "agent/fix/{signature_id[:8]}"
                                              # (Phase 2, before signatures exist: head_sha[:8])
    base: str; title: str; body: str
    files: list[FilePatch] = Field(max_length=5)
    labels: list[str] = ["agent-generated"]
    draft: bool = True

class TicketDraft(BaseModel):
    title: str; body: str; labels: list[str] = ["agent-triage"]

class RemediationPlan(BaseModel):             # the Remediator's LLM output
    rationale: str = Field(max_length=800)    # FIRST via propertyOrdering — the model reasons before it concludes
    action: Literal["retry_job","open_fix_pr","open_revert_pr","file_ticket","no_action"]
    tool_calls: list[ToolCall] = Field(max_length=5)      # PROPOSED, never pre-executed
    pr_draft: PrDraft | None = None
    ticket_draft: TicketDraft | None = None

class ApprovalRequest(BaseModel):
    approval_id: str; run_id: RunId; state: Literal["pending","approved","rejected","expired"]
    plan: RemediationPlan; decisions: list[PolicyDecision]
    requested_at: datetime; expires_at: datetime

class RemediationResult(BaseModel):           # the remediate stage output
    plan: RemediationPlan
    decisions: list[PolicyDecision]
    executed: list[ToolResult] = []
    pending_approval: ApprovalRequest | None = None
    status: Literal["executed","awaiting_approval","denied","rejected","no_action"]
                                              # `rejected` (Phase 2): a person refused the plan;
                                              # `denied`: the policy did
```

## A.12 HTTP API

| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/v1/runs` | `RunRequest` | `202 {run_id, status}` / `501` live mode when `HARNESS_GATEWAY=replay` / `403` live mode for a repo not in `HARNESS_ALLOWED_REPOS` / `422` replay mode with no `replay_fixture` |
| GET | `/v1/runs/{run_id}` | — | `200 RunOutcome` / `404` |
| GET | `/v1/runs` | `?status&integration&limit&cursor` | `200 {items: [RunSummary], next_cursor}` |
| GET | `/v1/runs/{run_id}/trace` | — | `200 TraceResponse` |
| GET | `/runs/{run_id}/view` | — | `200 text/html` |
| POST | `/v1/replay/{scenario}` | `?fresh=bool&sync=bool` | `200 RunOutcome` (synchronous by default — the demo path) |
| POST | `/v1/approvals/{approval_id}` | `{decision: "approve"\|"reject", actor: str, note?: str}` | `200 {state, executed: [ToolResult], decisions: [PolicyDecision], approval_id, run_id}` / `404` / `409` if already decided / `410` if expired -- the `409`/`410` problem document carries `state` as an extension member |
| GET | `/v1/escalations` | `?limit` | `200 [EscalationRecord + run_id]` -- each item is the record's dump plus the `run_id` it belongs to |
| POST | `/webhooks/github` | GitHub `workflow_run` payload + `X-Hub-Signature-256` | `202 {run_id, status}` / `204` ignored / `401` bad sig / `403` repo not allowlisted |
| GET | `/healthz` | — | `200 {status, db, version}` |
| GET | `/readyz` | — | `200`/`503 {db_writable, gemini_key_present, policy_loaded}` |

Errors use RFC 9457 `application/problem+json`: `{type, title, status, detail, instance, run_id?}`,
plus harness-authored extension members where a row above names one (`state` on the approval
route). `detail` passes through the `Redactor`.

> **Amendment (Phase 2).** Two additive divergences, recorded rather than decided silently:
> `GET /v1/escalations` items carry `run_id` because `EscalationRecord` has no run id and
> `extra="forbid"`, and a list of escalations nobody can trace to a run is not useful; and the
> approval response carries `decisions` because the route re-evaluates policy before executing,
> and a re-evaluation that refuses must be visible in the response that reports it.

> **Amendment (Phase 3).** `GET /readyz` additionally reports `migrations_applied` (B.3: "readyz
> fails until they succeed"). `POST /v1/replay/{scenario}?fresh` has its meaning: `fresh=true`
> (the default) claims under a nonce-suffixed key so every replay re-runs; `fresh=false` claims the
> webhook's real key and a repeat answers Appendix C's `deduplicated` outcome. `GET /v1/runs`
> honours `?cursor` with keyset pagination on `run_id`. A store that cannot be reached on a read
> route answers `503` with `Retry-After`; on the run routes the request degrades and runs
> unclaimed instead.

`200 RunOutcome` responses are the model dump with one documented exception: raw external content carried in `final` is replaced by its length and sha256 digest at the HTTP boundary — today `final.<artifact>.logs[].excerpt` → `excerpt_length` + `excerpt_sha256` and `final.<artifact>.diff.files[].patch` → `patch_length` + `patch_sha256`. `final` is opaque to the harness, so this substitution lives in the API layer and must be extended by hand when an integration adds a raw-content field. The whole body also passes through the `Redactor`.

---

# Appendix B — Failure-path matrix (every external call)

## B.1 Gemini API

| Condition | Detection | Behaviour | Trace / outcome |
|---|---|---|---|
| Timeout (>60 s) | `httpx.TimeoutException` | 1 retry at same budget | then `AgentError(kind="llm_timeout")`, run `escalated`, reason `llm_timeout` |
| 429 rate limited | status 429 | up to 4 attempts, exp backoff 0.5→8 s full jitter, honour `Retry-After` | exhausted → escalate `rate_limited`; each attempt is a child span |
| 503 / 504 | status | same as 429 | same |
| 401 / 403 auth | status | **no retry** | run `failed`, escalate `config_error`, message is literally `"Gemini authentication failed; check HARNESS_GEMINI_API_KEY"` — the key value never appears |
| 400 request too large | status + message match | re-assemble context at `budget × 0.5`, retry once | `context_downshift` attribute on the span |
| Response not JSON | `json.JSONDecodeError` | Recovery loop (3 attempts, error fed back) | exhausted → `invalid_output` |
| JSON but schema-invalid | `ValidationError` | Recovery loop with `e.errors()` attached | exhausted → `invalid_output` |
| `finish_reason == "MAX_TOKENS"` | field | treated as schema-invalid; retry with `max_output_tokens × 1.5` (once) | recorded |
| Safety block / empty candidates | deterministic refusal (`SAFETY`, `RECITATION`, `BLOCKLIST`) or no candidates | no retry (deterministic) | escalate `invalid_output`, `detail.finish_reason` recorded |
| Network unreachable | `httpx.ConnectError` | 3 attempts, exp backoff | escalate `llm_upstream` |

The deterministic-refusal list is the set that passes one admitting test: *is the refusal a
function of the content we sent, such that asking again cannot change the answer?* See
`recovery._TERMINAL_FINISH_REASONS` for that test applied to every member of the SDK's
`FinishReason` enum, including the enumerated exclusions and why each was left retryable —
notably the image reasons (unreachable: this loop never requests an image modality),
`MALFORMED_FUNCTION_CALL` (a defective sample, which is the one case retrying reliably fixes),
and `PROHIBITED_CONTENT` / `SPII` — `finish_reason` is a candidate-level field describing why
*generation* stopped, so a content refusal there is a verdict on the sample, not on the input;
a prompt-level block arrives instead as empty candidates and is covered by the other half of
this row.
Pointing at the constant rather than restating the list keeps this row from going stale the
next time the SDK pin moves.

## B.2 GitHub API

| Condition | Detection | Behaviour |
|---|---|---|
| Connect timeout (10 s) / read timeout (30 s) | `httpx.TimeoutException` | 2 retries → `ToolError(kind="timeout", retryable=True)` |
| Primary rate limit | 403 + `x-ratelimit-remaining: 0` | sleep to `x-ratelimit-reset`, capped at 60 s, then 1 retry. Longer than 60 s → `ToolError(kind="rate_limited", retry_after_s=…)`; if the tool was required, run escalates `rate_limited` and the idempotency key is left claimable so a webhook redelivery can pick it up |
| Secondary / abuse limit | 403 + `retry-after` header | honour `Retry-After`, max 2 retries |
| 401 bad credentials | status | **no retry**; `ToolError(kind="auth")`; run `failed`, escalate `config_error`. Token never logged |
| 403 insufficient scope | status + message | no retry; `ToolError(kind="auth")` with the *required scope name* in the message |
| 404 on a read tool | status | `ToolError(kind="not_found")` returned to the agent as data (e.g. "no green baseline") — **not** a run failure |
| 404 on a write tool | status | run `failed`, escalate `tool_failure` (indicates a bug or a deleted resource) |
| 409 conflict on write (branch exists) | status | treated as **success**: fetch and return the existing resource (see Appendix C) |
| 422 validation (PR already exists) | status + message | treated as success; return the existing PR |
| 5xx | status | 3 retries, exp backoff 0.5→8 s |
| Malformed / non-JSON body | parse failure | `ToolError(kind="malformed")`, no retry, first 500 redacted chars kept as evidence |
| Log endpoint 302 → zip | redirect | follow once, stream, cap at 20 MB keeping the **last** 20 MB; set `truncation.head_dropped=True` |
| Log zip corrupt | `zipfile.BadZipFile` | `ToolError(kind="malformed")`; the run proceeds on diff + memory alone with `degraded_components += ["logs"]` and the `gateway_degraded` confidence penalty |
| Compare returns >300 files | `total_files > len(files)` | `DiffSummary.truncated=True`; keep patches only for dependency manifests, config files, and files named in the log anchors; keep bare paths for the rest |

## B.3 SQLite

| Condition | Behaviour |
|---|---|
| `database is locked` / `busy` | `busy_timeout=5000` first; then 3 app retries at 100/200/400 ms; then degrade — run continues, `degraded_components += ["memory"]`, `MemoryHit.unavailable=True`, no `memory_agreement` bonus, and the retry-cap fact defaults to a **conservative** `retries_for_signature_24h = 999` so the flaky-retry rule fails closed |
| File missing | migrations create it at startup; `readyz` fails until they succeed |
| `DatabaseError: file is not a database` (corrupt) | log fatal, rename to `harness.db.corrupt.<ts>`, recreate, escalate `config_error`. Memory is regenerable; this is acceptable |
| Disk full (`OperationalError: disk I/O error`) | writes degrade as above; `readyz` returns 503; `healthz` reports `db: "degraded"` |
| Migration failure at startup | process exits non-zero — fail fast rather than serve a half-migrated schema |
| Two writers (should not happen, single container) | WAL + process-wide `asyncio.Lock`; the `run.idempotency_key` UNIQUE constraint is the real safety net |

## B.4 Outbound escalation webhook

Timeout 5 s, 2 retries, then record `escalation.delivery_error` and continue. **A delivery failure
never fails the run** — the `escalation` row is the durable record; the webhook is a convenience.

---

# Appendix C — Idempotency

**The key.** Derived from the *domain object*, not the delivery:

```python
idempotency_key = "cicd:" + sha256(
    f"{repo_full_name}|{workflow_run.id}|{workflow_run.run_attempt}"
).hexdigest()[:32]
```

GitHub's `X-GitHub-Delivery` GUID is deliberately **not** used: two different deliveries can
describe the same attempt (webhook fan-out, a manual redelivery, a `check_run` and a `workflow_run`
event for the same failure), and all of them must collapse to one run. `run_attempt` is included so
a genuine GitHub-side re-run is correctly treated as new work.

**The claim protocol** (`MemoryStore.claim_run`, one SQL statement):

```sql
INSERT INTO run (run_id, idempotency_key, integration, status, created_at, heartbeat_at)
VALUES (?, ?, ?, 'in_progress', ?, ?)
ON CONFLICT(idempotency_key) DO NOTHING;
```

| `rowcount` | Existing state | Response |
|---|---|---|
| 1 | — | claim acquired, run proceeds |
| 0 | `completed` / `escalated` / `failed` | `200 RunOutcome` with `status="deduplicated"` and `original_run_id` set to the first run. **No agents run, no tokens spent, no actions taken.** |
| 0 | `awaiting_approval` | `200` with the existing outcome and the same `approval_id` — a redelivery must not create a second approval |
| 0 | `in_progress`, heartbeat < 120 s old | `202 {"status":"in_progress","run_id":…}` |
| 0 | `in_progress`, heartbeat > 120 s old | **takeover**: `UPDATE run SET status='superseded'`; insert a new row with `idempotency_key = <key>#<n>`, `superseded_run_id` set, `attempt = attempt+1`. Prevents a container crash from permanently poisoning a key. |

The orchestrator writes `heartbeat_at` every 15 s while a run is active.

**Action-level idempotency** (the layer that actually matters — the claim row protects against
concurrent duplicates, this protects against a retry after a partial success):

- `rerun_failed_jobs` — GitHub returns 403 "cannot re-run; the run is already in progress". That is
  treated as **success** (`ToolResult.ok=True, cached=True`). Before calling, the gateway checks
  whether `run_attempt` has already advanced past the one in the bundle; if so, it no-ops.
- `create_branch` — deterministic name `agent/fix/{signature_id[:8]}`. 422 "Reference already
  exists" → fetch and return the existing ref, `cached=True`.
- `create_or_update_file` — passes the current blob `sha`; a 409 means someone else wrote it, which
  is a hard stop (do not clobber).
- `open_pull_request` — first `GET /pulls?head={branch}&state=open`; if one exists, return it. 422
  "A pull request already exists" → same. Never two PRs for one signature.
- `create_issue` — searches open issues for the marker `<!-- harness:signature:{signature_id} -->`
  embedded in the body; if found, comments on it instead of opening a duplicate.
- Every write `ToolCall` carries `idempotency_key = f"{run_id}:{tool}:{sha256(args)[:8]}"`, and the
  gateway keeps an in-process dict of completed write calls per run so a retry inside one run
  cannot double-fire.

**Approvals** are single-use: `POST /v1/approvals/{id}` on an already-decided approval returns
`409`, on an expired one `410`.

**Verified by:** the Phase 5 verification step 4, plus
`tests/integration/test_idempotency.py::test_concurrent_duplicate_deliveries` which fires 5
identical signed webhooks concurrently with `asyncio.gather` and asserts exactly one `run` row, one
`observation` row, and one `rerun_failed_jobs` call recorded by `respx`.

---

# Appendix D — Cold start (no prior green run)

**Baseline resolution, in order** (`integrations/cicd/baseline.py`):

1. Most recent `workflow_run` with `conclusion=success`, same `workflow_id`, same `branch`, created
   before this one → `baseline_kind="branch_green"`, diff = `compare/{green_sha}...{head_sha}`.
2. Else the same on the repo's **default branch** → `baseline_kind="default_green"`.
3. Else, if the head commit has a parent → `GET /commits/{head_sha}` and use only that commit's
   files → `baseline_kind="head_commit_only"`.
4. Else (initial commit, brand-new repo, force-pushed history) → `baseline_kind="none"`,
   `DiffSummary.files = []`.

Cases 3 and 4 both set `FailureBundle.cold_start = True`.

**Decision (branch → default → head-commit, not merge-base).** A proper merge-base against the
default branch is more correct, but computing it requires either a clone or several extra API
round-trips, and this service is deliberately API-only with no working copy. The fallback chain
gets the right answer in the overwhelmingly common case (a PR branch or a push to main) and
degrades honestly, with `baseline_kind` recorded so nobody has to guess which one was used.
Next-simplest alternative: always diff the head commit only — simpler, and it would miss regressions
introduced two commits back on a branch, which is the single most common real case.

**What cold start changes, concretely:**

| Effect | Detail |
|---|---|
| Prompt | The Diagnostician receives an explicit block: *"No green baseline exists for this workflow/branch, so 'what changed' is unknown or partial. Do NOT classify `real_regression` on diff evidence alone. Prefer `config_issue`, `dependency_break`, or `unknown`. Cap `self_confidence` at 0.70."* |
| Confidence | `cold_start` adjustment −0.05 |
| Policy | `context.cold_start: {eq: false}` is a condition on `retry-suspected-flaky`, so **auto-retry is disabled on a cold start** — the harness will not take an autonomous action against a repo it has never seen succeed. `file-ticket` remains allowed; `open-fix-pr` remains `require_approval`. |
| Memory | A first-ever signature yields `PriorHistory(occurrences=0, prior_hint="unknown")`. The prompt states: *"No prior history exists. Absence of history is NOT evidence that this failure is real; treat it as no information."* |
| Trace | `cold_start=true` and `baseline_kind` are span attributes on the investigate stage and shown as a banner in the trace view |

**Verified by:**
```powershell
uv run pytest tests/unit/test_baseline.py -q
#   test_resolves_branch_green / test_falls_back_to_default_green /
#   test_head_commit_only_when_no_success / test_none_on_initial_commit
curl.exe -s -X POST localhost:8000/v1/replay/cold_start | `
  jq '{kind:.final.bundle.diff.baseline_kind, cold:.final.bundle.cold_start, cat:.final.diagnosis.category, effect:.final.remediation.decisions[0].effect}'
# EXPECT baseline_kind "none", cold true, category != "real_regression",
#        and effect "deny" for rerun_failed_jobs (rule retry-suspected-flaky did not match)
```
(A fifth fixture, `cold_start`, is added in Phase 3 — it is a copy of `flaky_test` with the
`find_last_successful_run` response replaced by an empty list.)

---

# Appendix E — Secrets and config

**Single source of truth:** `src/settings.py`. No other module reads `os.environ`; a unit test greps
for `os.environ` / `os.getenv` outside `settings.py` and fails if found.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8",
        env_prefix="HARNESS_", extra="forbid", frozen=True,
    )
    # --- secrets (SecretStr; never rendered) ---
    gemini_api_key: SecretStr
    github_token: SecretStr
    github_webhook_secret: SecretStr
    escalation_webhook_url: SecretStr | None = None

    # --- non-secret config ---
    database_path: Path = Path("./data/harness.db")
    gemini_model: str = "gemini-3.6-flash"
    model_investigator: str | None = None          # falls back to gemini_model
    model_diagnostician: str | None = None
    model_remediator: str | None = None
    escalation_threshold: float = Field(0.70, ge=0.0, le=1.0)
    log_char_budget: int = 120_000
    gemini_timeout_s: float = 60.0
    github_timeout_s: float = 30.0
    github_api_base: AnyHttpUrl = "https://api.github.com"
    allowed_repos: list[str] = []                  # "owner/name"; empty = replay-only
    gateway: Literal["github", "replay"] = "replay"
    dry_run: bool = True                           # DEFAULTS TO TRUE — writes are opt-in
    max_concurrent_runs: int = 4
    approval_ttl_h: int = 24
    fault_inject: str | None = None                # test-only; refused when env != "dev"
    env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
```

**Where each value lives:**

| Value | Local dev | Docker | Fly.io | HF Space (live) |
|---|---|---|---|---|
| `HARNESS_GEMINI_API_KEY` | `.env` (gitignored) | `--env-file .env` | `fly secrets set` (encrypted at rest, injected as env) | Space **Secret**, never a Variable |
| `HARNESS_GITHUB_TOKEN` | `.env` | `--env-file .env` | `fly secrets set` | Space Secret |
| `HARNESS_GITHUB_WEBHOOK_SECRET` | `.env` | `--env-file .env` | `fly secrets set` | Space Secret |
| `HARNESS_DATABASE_PATH` | `./data/harness.db` | volume mount `./data:/app/data` | `/data/harness.db` on a 1 GB Fly volume, `[mounts]` in `fly.toml` | `./data/harness.db` on **ephemeral** disk -- resets on restart or sleep (risk 2) |
| Everything else | `.env` / defaults | `fly.toml [env]` | `fly.toml [env]` (non-secret, committed) | Space Variables, or the defaults in `settings.py` |

On the Space, `app.py` must still not read `os.environ` itself -- `settings.py` stays the only place
env is read, and `tests/unit/test_no_env_access.py` enforces it.

`.env` is in `.gitignore` **and** `.dockerignore` — no secret is ever baked into an image layer.
`.env.example` is committed with placeholders (`HARNESS_GEMINI_API_KEY=AIza...replace-me`).
`extra="forbid"` turns a typo'd env var into a startup crash rather than a silently ignored setting.

**Confirmation that no secret reaches Observability.** Four barriers, listed in Phase 5, and one
test that proves it end to end: `tests/test_no_secret_leak.py` injects sentinel token values,
runs every fixture — including one whose log fixture deliberately contains a pasted
`ghp_…` token, simulating a careless workflow — and then asserts zero occurrences of each sentinel
across `trace_span`, `escalation`, captured stdout/stderr, and the raw bytes of the SQLite file.
This test is a CI gate and must be green before the repo is made public.

**Decision (fine-grained PAT for MVP, GitHub App as the documented path).** A fine-grained personal
access token scoped to the *single* demo repository, with exactly: Actions read, Contents
read+write, Pull requests write, Issues write, Metadata read. Never a classic token with blanket
`repo` scope. Rationale: a PAT is one step to create and revoke and is entirely adequate for a
one-repo demo. A GitHub App is the correct answer for anything multi-repo (per-installation tokens,
higher rate limits, no personal identity attached to bot actions) and is documented in
`docs/ADAPTER_GUIDE.md` as the migration — the gateway takes an auth strategy object, so it is a
constructor change. See Open Risk 3.

**Decision (`dry_run` defaults to `True`).** A fresh clone, a misconfigured env, or a forgotten flag
results in the system *planning* actions and executing none. Turning on writes requires deliberately
setting `HARNESS_DRY_RUN=false`. In dry-run, write tools return `ToolResult(ok=True, dry_run=True,
data={"would_have": args})`, so the whole pipeline including the trace is exercisable safely.

---

# Open risks

Each has a recommended default already baked into the plan; these are the places where the default
is a judgement call rather than a settled answer.

**1. Gemini's `response_schema` is an OpenAPI subset, and the subset moves.**
Nested `$defs`, discriminated unions, and some `format` keywords are handled inconsistently across
model versions, and a schema that works today can be rejected after a model update.
*Default:* `to_gemini_schema()` flattens and strips aggressively; all contracts are designed flat;
Pydantic validation runs regardless; and if `response_schema` is rejected at runtime, the client
falls back to `response_mime_type="application/json"` with the schema pasted into the prompt and
lets the Recovery loop handle the quality drop. A contract test asserts every agent schema survives
`to_gemini_schema()` at import time, so a breakage shows up as a failing test rather than a 400 in
production.

**2. SQLite is a single point of failure for Memory — and on the live target there is no disk at
all.** On Fly, volumes are per-machine: `fly scale count 2` would give two machines two *different*
databases, silently splitting memory and breaking idempotency, so the default is `fly scale count 1`
with `min_machines_running = 1`. **On the Hugging Face Space that actually serves this, the problem
is sharper: a Space has no persistent disk at all, so `data/harness.db` resets whenever the Space
restarts or sleeps** (`docs/deploy-huggingface.md`, "Storage is ephemeral"). This paragraph used to
end "Render's free tier has **no** persistent disk, which is why Fly is the recommended first
target" — true when Fly was the target, and exactly backwards now that it is not.

For Phases 1 and 2 the cost is bounded: the span trace and the in-process run registry do not
survive a restart, and neither is load-bearing for a replay demo. **Phase 3 is where this stops
being an annoyance and becomes a design constraint** — cross-run memory is the entire point of that
slice, and on the Space it would be amnesiac between restarts, which would make its Verify step 2
("4th flaky run shows `occurrences=3`") unreachable on the live target. Decide it at the start of
Phase 3, not at its Verify step. *Migration path:* swap `SqliteMemoryStore` for
`PostgresMemoryStore` (Supabase, or Fly Postgres) — the `MemoryStore` protocol exists precisely so
this is one class and no call-site changes. Losing memory is otherwise regenerable and acceptable
for this project.

**3. Credentials on a public portfolio repo.**
*Default:* the harness repo is public; the demo repo is separate; the PAT is fine-grained and scoped
to the demo repo only; `test_no_secret_leak.py` is a merge gate; `scripts/scrub_fixtures.py` runs
over any recorded fixture before commit; and the PAT is rotated once immediately before the repo is
made public. **This is the one item worth a deliberate pause before Phase 5** — creating the PAT is
reversible, but a token committed to a public repo's history is not. If you would rather not hold a
write-capable token at all, the fallback is to leave `HARNESS_DRY_RUN=true` permanently and demo
writes only through the replay gateway; the trace looks identical.

**4. The 0.70 threshold and the confidence adjustments are asserted, not measured.**
Four scenarios cannot calibrate a decision boundary; the adjustment deltas (+0.10, −0.15, …) are
reasoned guesses. *Default:* keep 0.70, make every threshold config rather than a constant, log
`self_confidence`, every adjustment, and `final_confidence` on every run, and revisit after ≥50 real
runs by plotting accuracy against the threshold. Say this out loud in the README — "here is how I
would calibrate it and why I haven't yet" reads as engineering maturity; a confidently unjustified
0.70 reads as the opposite.

**5. Log volume and anchor selection can drop the true root cause.**
A 200 MB log with the real error early and thousands of cascading errors late will keep the wrong
anchors (the plan keeps the *last* N). *Default:* 20 MB download cap keeping the tail, explicit
`anchors_dropped` in the trace, and `head_lines=200` so setup failures are still visible. Known
limitation, documented. Mitigation if it bites: score anchors by severity keyword rather than
position, and keep the first *and* last k.

**6. Gemini free-tier rate limits will throttle the eval loop.**
`scripts/eval.py --runs 5` over 5 fixtures × 3 agents = 75 calls will hit free-tier RPM limits.
*Default:* `--concurrency 1` with a token-bucket limiter in `GeminiClient`, and use a paid key for
demo recordings. Budget roughly $0.01–0.05 per run on Flash.

**7. Runaway auto-retry on a misclassified flaky test.**
If a genuinely broken test is once labelled flaky and memory reinforces the prior, the system could
retry indefinitely and burn CI minutes. *Default:* three independent brakes — max 2 retries per
signature per 24 h (policy), max 1 side-effecting action per run (hardcoded, not configurable), and
the requirement that the Diagnostician cite *this run's* evidence rather than relying on the prior.
An `action_outcome == "failed_again"` observation also pushes the flaky share below the 0.6
threshold, so the prior self-corrects.

**8. In-process background tasks lose in-flight runs on restart.**
Fly redeploys and OOM restarts will kill running triage. *Default:* runs are short (<60 s), the
stale-heartbeat takeover rule prevents a poisoned idempotency key, and GitHub retries failed webhook
deliveries. Accept it. If it becomes visible, move to arq + Redis — the orchestrator is already
invoked through a single entry point, so the change is confined to `api/`.

**9. There is no actual human on the other end of "escalate".**
For a portfolio project the "human channel" is the `escalation` table, `GET /v1/escalations`, and an
optional Slack webhook. *Default:* be explicit about this in the README rather than implying an
on-call rotation. The demo shows the escalation being *raised and recorded*, which is the part that
matters architecturally.

**10. Prompt/model drift silently degrades quality.**
A Gemini model update can change classification behaviour with no code change. *Default:* pin the
model id in config, hash the prompt into every trace, and keep `scripts/eval.py` as a CI gate that
fails on <100 % accuracy over the canned set. That turns drift into a red build instead of a quiet
regression.

**11. Scale-to-zero could delay webhook handling — and a sleeping Space wakes slower than Fly.**
A cold Fly machine takes ~2–5 s to wake, inside GitHub's 10 s delivery timeout but not comfortably;
*default* there: `min_machines_running = 1` (~$2/month). A sleeping Hugging Face Space takes
substantially longer, so on the live target exceeding the 10 s timeout is the expected case rather
than a near miss. Phase 5 owns this. *Default there:* accept the cold start and rely on GitHub's
redelivery — which is what makes Phase 5's idempotency work load-bearing rather than decorative.

**12. Only run this against repositories you own.**
Automated re-runs and PRs against someone else's repo are a fast way to get a token banned.
*Default:* `HARNESS_ALLOWED_REPOS` is an explicit allowlist, empty by default, and the webhook
returns 403 for anything not on it.

---

## Execution order at a glance

| Phase | Status | Ships | Gate to move on |
|---|---|---|---|
| 0 | **done** — `phase-0-green` | Scaffold, healthz, layering test | `pytest` + `docker compose up` green |
| 1 | **done** — `phase-1-green` | Investigator + Diagnostician, 1 fixture, deployed, **plus Recovery** | Correct category + ≥1 citation from the public URL |
| 2 | **built** -- see the Phase 2 status block | Remediator (retry) + Guardrails + approvals, 2 fixtures, live gateway | Regression blocked pending approval; flaky *denied* by the fail-closed cap with the clause named (allow path pinned offline); 90-case deny test green |
| 3 | **built** -- see the Phase 3 amendment | Memory | 4th flaky run shows `occurrences=3`, `likely_flaky`, retry cap bites on the 3rd |
| 4 | **built** -- see the Phase 4 amendment | Evaluator + eval harness (Recovery shipped in 1) | Fabricated citation → escalate, no remediation; `eval.py` 5/5 (stub), live number in `verify.md` |
| 5 | **built** -- see the Phase 5 amendment | Trace view + real webhook | Redelivery dedupes (Appendix C's five-concurrent test green); secret-leak test green; live run from the demo repo pending the user-gated deploy (`verify.md`) |
| 6 | | Second adapter sketch | `git diff --stat -- src/harness/` is empty; contract suite green ×3 |

Phase 2's gate is amended by that phase's own amendment 3: with the retry-count stub failing closed,
"flaky auto-retries" is not demonstrable until Memory is real. Either inject a stub count under the
cap for that one scenario, or move the auto-retry half of the gate to Phase 3 and gate Phase 2 on
the deny instead. The 90-case deny test is unaffected and remains the load-bearing half.
