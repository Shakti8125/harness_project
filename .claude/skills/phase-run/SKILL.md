---
name: phase-run
description: Run one phase of the Agent Harness build (PLAN.md phases 0-6) by dispatching the specialist agents in parallel over disjoint write territories, then gating on test-verifier and phase-reviewer. Use when starting or resuming a phase.
---

# Run a phase

Usage: `/phase-run <0-6>`. If no number is given, read `docs/progress/` to find the last
phase with a PASS verdict and offer the next one.

`PLAN.md` at the repo root is normative for everything below. Read the target phase section
plus the appendices it references **before** dispatching, and put the relevant excerpts into
each agent's brief — the agents start cold and cannot see this conversation.

## Why these agents can run at once

They are partitioned by **disjoint write territory**, not by phase, so two agents can never
touch the same file:

| Agent | Writes | Never writes |
|---|---|---|
| `harness-core` | `src/harness/**` | anything else |
| `cicd-integration` | `src/integrations/**` | anything else |
| `api-surface` | `src/api/**`, `src/settings.py`, `pyproject.toml`, Docker/fly/dotfiles | anything else |
| `fixtures-eval` | `fixtures/**`, `scripts/**` | anything else |
| `test-verifier` | `tests/**` | source, ever |
| `phase-reviewer` | nothing (read-only) | everything |

The coordination mechanism is **PLAN.md Appendix A**, frozen in Phase 0. Because every
signature is fixed in advance, an agent can code against an interface whose implementation
another agent is writing at the same moment. That is the whole trick; if the contracts are
not frozen, none of this parallelism is safe.

## Wave structure

Every phase runs the same three waves.

- **Wave 1 — build (parallel).** Dispatch the builder agents for this phase in a single
  message so they run concurrently. Give each one: the phase's goal, its slice of the work,
  the exact PLAN.md sections it must follow, and its handoff expectations.
- **Wave 2 — gate (serial).** `test-verifier` writes that phase's named tests and runs the
  literal Verify block. Verdict PASS / FAIL / BLOCKED.
- **Wave 3 — audit (serial).** Only if Wave 2 is PASS: `phase-reviewer` audits contract
  fidelity, layer leakage, failure paths and secrets. Verdict SHIP / FIX FIRST.

On FAIL or FIX FIRST, re-dispatch **only** the owning agent with the finding quoted verbatim,
then re-run Wave 2. Do not start the next phase until Wave 3 says SHIP.

## Per-phase dispatch

### Phase 0 — Scaffold, and the contract freeze
The single most important phase: it makes every later phase parallelisable.

| Agent | Brief |
|---|---|
| `api-surface` | pyproject with the full frozen dep set, Dockerfile, compose, fly.toml, dotfiles, `settings.py` from Appendix E, `main.py` with `/healthz` + `/readyz` |
| `harness-core` | **All** of Appendix A.1-A.10 as models and Protocols — real fields, `NotImplementedError` bodies. Nothing else. |
| `cicd-integration` | All of Appendix A.11, plus `policy.yaml` with the Phase 2 content already in place |
| `fixtures-eval` | Fixture directory format + `real_regression` skeleton |
| `test-verifier` | `test_layering.py`, `test_no_env_access.py`, conftest with the temp-DB fixture |

Gate: `uv sync`; ruff; `mypy --strict src/harness`; `pytest -q`; `docker compose up -d --build`; `curl /healthz`.

### Phase 1 — Investigator + Diagnostician, deployed
| Agent | Brief |
|---|---|
| `harness-core` | `context_manager` (the 7-step algorithm, anchors inviolable), `llm` incl. `to_gemini_schema`, `agent` base, `orchestrator` with two stages and the confidence gate, `observability` write path, `confidence` |
| `cicd-integration` | Investigator (deterministic collection + one LLM call), Diagnostician, both prompts, `gateway_replay` |
| `api-surface` | `/v1/runs`, `/v1/runs/{id}`, `/v1/replay/{scenario}` (synchronous), `deps.py`, first `fly deploy` |
| `fixtures-eval` | `real_regression` complete — scenario.yaml, webhook.json, api/, a log long enough to force trimming |
| `test-verifier` | `test_context_manager.py` incl. `test_error_lines_never_trimmed`; e2e replay test |

### Phase 2 — Remediator (retry only) + Guardrails
| Agent | Brief |
|---|---|
| `harness-core` | `guardrails.py` (forbidden → invariants → first match → default), third orchestrator stage, approval-suspend path |
| `cicd-integration` | Remediator, `policy.yaml` live, `gateway_github` read tools + `rerun_failed_jobs`; PR tools registered but raising |
| `api-surface` | `POST /v1/approvals/{id}` with 409/410, `GET /v1/escalations` |
| `fixtures-eval` | `flaky_test`, `infra_timeout` (empty diff — deliberate) |
| `test-verifier` | the 90-case deny matrix, the forged-decision test, max-one-action test |

### Phase 3 — Memory
| Agent | Brief |
|---|---|
| `harness-core` | `memory.py` + `migrations/001_init.sql`, WAL/busy_timeout/lock, the degrade path failing **closed** at 999 |
| `cicd-integration` | `fingerprint.py`, Investigator populating `PriorHistory`, Remediator writing observations, the prior-not-verdict prompt clause |
| `fixtures-eval` | `cold_start` fixture, `sqlite_locked` fault injection |
| `api-surface` | idle this phase unless `claim_run` wiring is needed — say so rather than inventing work |
| `test-verifier` | fingerprint stability, repeat-flakiness across 4 runs, retry cap biting on the 3rd, memory-outage degrade |

### Phase 4 — Evaluator + Recovery
| Agent | Brief |
|---|---|
| `harness-core` | `evaluator.py`, `recovery.py` with the six failure classes distinguished, escalation channel |
| `cicd-integration` | `claim_checkers.py` — all five kinds, fuzzy threshold 0.92, missing-artifact → unverifiable not refuted |
| `fixtures-eval` | `dependency_break`, `hallucination`, `scripts/eval.py` as a CI gate, the `llm_bad_json` / `llm_429` injections |
| `api-surface` | surface `evaluation` in the run outcome payload |
| `test-verifier` | fabricated-citation refuted, remediator never invoked, recovery recovers at 2 and escalates at 9 |

### Phase 5 — Observability view + real webhook
| Agent | Brief |
|---|---|
| `harness-core` | full `TraceRecorder`, `Redactor`, `SecretRegistry`, the regex scrub list |
| `api-surface` | `POST /webhooks/github` with HMAC + allowlist + 202, `/v1/runs/{id}/trace`, `/runs/{id}/view` + Jinja template |
| `cicd-integration` | PR-writing tools behind the existing require_approval rule, with their idempotency behaviour |
| `fixtures-eval` | `record_fixture.py`, `scrub_fixtures.py`, `seed_demo_repo.sh` |
| `test-verifier` | `test_no_secret_leak.py`, concurrent-duplicate-delivery idempotency test |

### Phase 6 — Second adapter (stretch)
| Agent | Brief |
|---|---|
| `cicd-integration` | `src/integrations/incident/` — schemas, policy.yaml, a complete `catalog()` over `NotImplementedError` bodies |
| `test-verifier` | `contract/test_tool_gateway_contract.py` parametrized over all three gateways |
| `phase-reviewer` | run `git diff --stat -- src/harness/` against the Phase 5 tag; **any output at all is a finding** |

`harness-core` must be idle in Phase 6. That is the point of the phase.

## Rules

- Dispatch Wave 1 agents in **one message** so they run concurrently. Sequential dispatch
  wastes the design.
- Never give two agents overlapping write territory, even "just this once".
- Agent reports are not shown to the user — relay what matters, especially any
  **contract deviation**, which blocks the phase regardless of test results.
- If an agent reports a needed change outside its territory, dispatch the owner; do not
  patch it yourself.
- Tag git at the end of each shipped phase: `phase-<N>-green`.
