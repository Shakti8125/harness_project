# Phase 1 — carried backlog

Written 2026-09-10, after the urgent-bundle round (`fe7ba2d` and its follow-up).

This is the documented remainder referred to by the `phase-1-green` decision: the findings
that are known, ranked, and deliberately not fixed in Phase 1. It is not a list of things
nobody looked at. Every item below was verified still open by the re-audit against the
committed tree, with the check it was verified by recorded next to it.

The authorities remain `docs/progress/phase-1/review.md` (the original 12-finding audit)
and `docs/progress/phase-1/review-2.md` (the fix-round re-audit). This file indexes what
survived them; it does not restate their reasoning.

> **Counting note.** `review-2.md`'s own PHASE 1 verdict line says the round "adds three
> more" findings while the document lists five. Five is right — that line counts only the
> two defects newly *introduced* by the fixes. The document is committed verbatim and is
> not edited to correct it. Work from five.

## Closed by the urgent bundle

| Finding | Was | Now |
|---|---|---|
| `review-2` 1 | high | Closed for the stated cause. `MAX_RETRY_AFTER_S` 20 s, `RETRY_DELAY_BUDGET_S` bounding total sleep per `retry_structured` call, and the Investigator short-circuit means a rate-limited run is one call, ~20 s. One residual, below. |
| `review-2` 2 | medium | Closed. The whole serialised body passes through the `Redactor`, which closes the citation/observation path *and* the "next raw-content field reopens it" class. |
| `review-2` 3 | medium | Closed on both entry points — the `uvicorn` lifespan and the Space's hand-call. |
| `review-2` 5 | low | Closed. `version: 1` names one prompt again. |

## Open — ranked

### Medium

| # | Source | One line | Verified open by |
|---|---|---|---|
| 4 | `review.md` | `Diagnosis` asks the model for `final_confidence` / `confidence_adjustments`, which A.11 marks harness-added. Overwritten today; wrong one consumer away. | `to_gemini_schema(Diagnosis)["propertyOrdering"]` still ends with both fields |
| 5 | `review.md` | No `RequestValidationError` handler — 422s return FastAPI's default JSON with the request body echoed, not RFC 9457. | zero `exception_handler` / `RequestValidationError` in `src/api/main.py` |
| 6 | `review.md` | `problem()`'s `detail` does not pass through the `Redactor`, which A.12 requires. | the only `redactor` reference in `main.py` is in `_serialize_run_outcome`; `problem()` has none |

**Findings 5 and 6 are the general case of a defect this round fixed a specific instance
of.** The missing-template `500 text/plain` was one instance of the gap finding 5
describes; eager validation removed that instance, not the gap. Any other unhandled route
exception still produces a bare 500 with no RFC 9457 body, no `run_id` and no trace entry.

**Finding 6 has a documentation hazard attached.** PLAN.md:1556 states that `detail` passes
through the `Redactor` — that sentence is aspirational, not descriptive. The A.12 amendment
added this round sits immediately below it at :1559 and *is* descriptive. Two adjacent
paragraphs now assert `Redactor` coverage, one true and one not. Whoever closes finding 6
must not read the new paragraph as evidence the old one already holds.

### Low

| # | Source | One line |
|---|---|---|
| 4 | `review-2.md` | Optional tool calls unobservable: `executed` populated and never read, `result.data` discarded, `additional_errors` only logged. Bites in Phase 2 when `GitHubToolGateway` makes those calls real. |
| 7 | `review.md` | `GET /v1/runs/{id}` and `.../trace` report different `degraded_components` for the same run (`orchestrator.py` writes `"degraded"`; `observability.py` reads `"degraded_component"`). |
| 8 | `review.md` | `_ALLOWED_SCHEMA_KEYS` drops `maxLength`/`minLength`, so `Field(max_length=…)` is invisible to the model but enforced by Pydantic — burns repair attempts. |
| 9 | `review.md` | B.1's "empty candidates" half of the safety row is not terminal; `finish_reason = "UNKNOWN"` is not in `_TERMINAL_FINISH_REASONS`, so it retries the full 3 attempts. |
| 11 | `review.md` | `PriorHistory` degraded path leaves `retries_in_24h` at `0` instead of B.3's fail-closed `999`. Phase 2 wires the retry cap to it. |
| 12 | `review.md` | `gateway_replay.py`'s `forbidden: tuple = ()` makes the authoritative safety re-check opt-in at construction. |

### Residuals and hazards — real, but not findings against a stated contract

- **The retry bound is on sleeps, not on call durations.** `gemini_timeout_s = 60.0` with a
  hard ceiling of 9 attempts means a provider that fails *slowly* rather than fast can
  still hold a request for minutes. This was never what `review-2.md` finding 1 was about —
  a 429 returns fast, so it is not the daily end state — but the broader "a public
  unauthenticated URL can hold a connection" concern is only partly retired. The honest
  bound today is on the 429 path specifically.
- **A rate-limited run now serves `final == {}`.** Ending the run on upstream failure is
  correct and is what the high finding asked for, but the collected `FailureBundle` and the
  `investigator_notes` degraded entry no longer reach the served `RunOutcome`;
  `AgentResult.evidence` is read by nothing downstream. With a 20-request/day quota this is
  the demo's normal daily output. Adjacent to `review-2.md` finding 4; the fix is a bundle
  field, not an adjustment. The misleading comment that obscured this was corrected.
- **`pyproject.toml` declares `"mypy"` unpinned** while `uv.lock` pins 2.3.1, so type-check
  answers differ by resolution path — this already happened once, producing an `arg-type`
  false positive at `orchestrator.py:312` under 1.14.1 that 2.3.1 does not report. Ranked
  low because `uv.lock` is the pin of record, the `Dockerfile` uses `uv sync --frozen`, and
  no CI resolves independently. If pinned, pin the dev group as a group; singling out mypy
  is arbitrary.
- **The 20 s wall-clock test** in `tests/unit/test_retry_delay_budget.py` takes the suite
  from ~6 s to ~28 s. It buys real-time proof against the shipped constants, which the
  mocked tests beside it cannot. The same property survives scaling both constants to 0.5 s
  via monkeypatch — the shipped values stay pinned by an assertion the test already carries.
  Revisit the first time someone skips the suite because of it.

## Settled — do not reopen

Carried from `handoff-fixround2.md` §6, still binding:

- The `prompt.render` span ships as-is; `AgentPrompt.prompt_version` is harness-core's call,
  queued behind Phase 2. Clean-run span count is **7**, not 5.
- The dormant prompt clauses land with Phase 3, worded around a real prior, together with
  `review.md` finding 11.
- A refused write-tool request carries no confidence signal in either direction. No PLAN.md
  adjustment row covers it; inventing one would be scope creep.
- Header-before-body precedence for the retry delay is correct as built; the duplicated
  `EscalationReason` lists stay duplicated with their drift-guard test;
  `retry_after_seconds` stays public as a test seam.
- Verify step 4 (escalation-threshold override) was substituted with offline coverage and
  does not need re-running before the phase closes.

## Constraints that outlive this phase

- **Live Gemini quota is 20 requests/day, free tier.** One replay = 2. Both the fix-round
  re-audit and the urgent-bundle re-audit reached their verdicts spending none.
- **`git add` new files explicitly and check `git diff --cached --name-status`** before
  committing anything that moves a module to a data file. A `git commit -a` nearly shipped
  a broken Space when the `prompts/*.md` files were untracked beside deleted `.py` ones.
- **`app.py` must not read `os.environ`** — `tests/unit/test_no_env_access.py` scans it.
- **A mounted sub-app gets no lifespan events.** `demo.app.mount("/", api)` means anything
  that must happen at Space startup needs a hand-call in `app.py:main()`, not just a
  lifespan hook. This has now caught two separate pieces of startup work; assume it will
  catch the third.
- **The Space is pinned to `zero-a10g`** and cannot leave it. See `docs/deploy-huggingface.md` §2.
- **Never print or pass the HF token on a command line.**
