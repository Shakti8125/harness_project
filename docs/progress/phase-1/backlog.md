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
| `review` 5 | medium | Closed. `RequestValidationError`, `StarletteHTTPException` and a catch-all `Exception` handler all return RFC 9457. The 422 is built from `loc`/`msg` only and never echoes the submitted input. |
| `review` 6 | medium | Closed. `problem()` scrubs its whole body through the same `Redactor` instance `_serialize_run_outcome` uses, so PLAN.md:1556 is now descriptive rather than aspirational. |

## Open — ranked

### Medium

| # | Source | One line | Verified open by |
|---|---|---|---|
| 4 | `review.md` | `Diagnosis` asks the model for `final_confidence` / `confidence_adjustments`, which A.11 marks harness-added. Overwritten today; wrong one consumer away. | `to_gemini_schema(Diagnosis)["propertyOrdering"]` still ends with both fields |

Finding 4 is the only medium left open, and it is the one with no consumer today: the
harness overwrites both fields before anything reads them. It becomes real the moment a
second consumer reads `Diagnosis` without going through that overwrite.

**Findings 5 and 6 were closed before the tag** rather than carried, because together they
were the *general* case of a defect the urgent bundle fixed one *instance* of. The
missing-template `500 text/plain` was one instance of finding 5's gap; eager validation
removed the instance, and the catch-all handler removed the class. Note the sequencing that
made this worth doing in one round: finding 5's catch-all is precisely a path where an
exception string is in scope, which is what turned finding 6 from a discipline guarantee
(`detail` is authored at each call site) into a structural one (`detail` is scrubbed
regardless of who authored it).

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
- **One test skips because `gradio` is deliberately absent from the lockfile.**
  `test_app_py_main_hand_calls_validate_prompt_templates_before_launch` is the only thing
  pinning that `app.py`'s `main()` performs the template hand-call before `demo.launch()` —
  i.e. the assertion that closes the mount-lifespan finding. It is guarded by
  `pytest.importorskip("gradio")` and was verified non-vacuous under an ephemeral
  `uv run --with gradio` overlay, which did not touch the project venv or lockfile. Running
  it unconditionally means adding `gradio`/`spaces` to the dev group, which trades away the
  isolation `requirements.txt` argues for ("gradio … is absent on purpose — the Space
  installs it itself"). A real trade, owned by whoever owns `pyproject.toml`; deliberately
  not decided inside a bug-fix round.
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
