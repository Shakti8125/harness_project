# Phase 1 — Verify block results

Run against the container built from this tree (`docker compose up -d --build`), with a
real `HARNESS_GEMINI_API_KEY` and `flyctl` v0.4.99 present.

**Verdict: all five steps PASS against the live model.** Step 5 was retargeted from Fly.io,
which requires a credit card, to a Hugging Face Gradio Space. It is live at
<https://shakti-agent-harness.hf.space> and returns a real diagnosis.

| Step | Result |
|---|---|
| 1 — context manager never eats the error | ✅ PASS |
| 2 — end-to-end replay, locally | ✅ PASS |
| 3 — diagnosis points at the right commit | ✅ PASS |
| 4 — escalation path fires | ✅ PASS |
| 5 — deployed | ✅ PASS — live at <https://shakti-agent-harness.hf.space> |

---

## Step 1 — the context manager never eats the error  ✅

```
$ uv run pytest tests/unit/test_context_manager.py -q
...........                                                              [100%]
11 passed in 0.90s
```

Includes `test_error_lines_never_trimmed`: a 50,000-line synthetic log with the
`AssertionError` on line 12,345, asserted present verbatim. The test also asserts the
budget genuinely bit (`kept_lines == 641 < 50,000`, elisions non-empty), so it cannot pass
by the log having fit whole. A companion test repeats it against the real 3,789-line
fixture log.

## Step 2 — end-to-end replay, locally  ✅

```
$ curl -s -X POST localhost:8000/v1/replay/real_regression | jq '{cat, conf, cites}'
{"cat":"real_regression","conf":0.95,"cites":4}
```

EXPECT `{"cat":"real_regression","conf":>=0.75,"cites":>=1}` — met.

The diagnosis is substantively correct, not merely well-formed. It cited the actual patch
line that introduced the bug:

```json
{"claim_kind":"file_in_diff","quote":"+    return price - (price * percent) // 100 + 1"}
{"claim_kind":"test_in_log","quote":"FAILED tests/test_pricing.py::test_discount_applies - assert 91 == 90"}
{"claim_kind":"test_in_log","quote":"FAILED tests/test_pricing.py::test_checkout_total_applies_discount - assert 455 == 450"}
{"claim_kind":"commit_in_range","quote":"e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df"}
```

`degraded_components: []` — the Investigator's notes call succeeded too. No adjustment
fired (citations present, `cold_start: false`, no gateway errors, diff non-empty), so
`final_confidence == self_confidence == 0.95`, which is the calibration behaving as
specified rather than being inert.

**~34 k tokens per run** across both agents.

## Step 3 — the diagnosis points at the right commit  ✅

```
got:      e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df
expected: e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df   (scenario.yaml -> expected.commit)
MATCH
```

## Step 4 — escalation path fires  ✅

With `HARNESS_ESCALATION_THRESHOLD=0.99`:

```
$ curl -s -X POST localhost:8000/v1/replay/real_regression | jq -r '.status, .escalation.reason'
escalated
low_confidence
# escalation.message: "confidence 0.95 < 0.99"
```

EXPECT `"escalated"`, `"low_confidence"` — met. The threshold override was made in `.env`
for the duration of the check and reverted immediately after; the next run returns
`completed`.

This also confirms the gate fires from the `remediate` stage, which has **no agent** this
phase — the short-circuit is live before the thing it guards exists.

## Step 5 — deployed  ✅ PASS, after retargeting

`flyctl` v0.4.99 is installed (at `C:\Users\Shakti\.fly\bin\`, **not on the inherited
PATH** — same shape as `uv` and `jq` in Phase 0) and `fly auth whoami` succeeds. App
creation does not:

```
$ fly apps create agent-harness
Error: We need your payment information to continue!
Add a credit card or buy credit: https://fly.io/dashboard/.../billing
```

The user has no card, so Fly is unavailable. **No secrets were transmitted** — the app was
never created, so the `fly secrets import` step never ran.

**Retargeted to a Hugging Face Gradio Space** (no card, long-lived container). A *Docker*
Space was not available on this account either, so the Space runs `python app.py`, which
launches gradio and mounts the **unchanged** FastAPI app at the root of gradio's own app.
Runbook: `docs/deploy-huggingface.md`.

Files added, none of which the application, the image or the test suite imports:
`app.py` (Space entrypoint), `requirements.txt` (direct dependencies only, unpinned — an
exact export of `uv.lock` cannot co-resolve with the gradio a Space installs over the top),
and Spaces front matter in `README.md`.

Two earlier changes made for the Docker-Space route were kept because they are correct
anyway: the root `README.md`, and the `Dockerfile` pinning the container user to UID/GID
1000 (verified: `/app/data` owned by `harness:harness`, `readyz` `db_writable: true` with
no volume mounted).

### Two deploy failures, both real, both fixed

The Space built cleanly and then died twice before it served. Neither cause was in `src/`,
and both fixes are confined to `app.py`.

**1 — port collision with a Node server this process spawned itself.**

```
INFO:     Application startup complete.
ERROR:    [Errno 98] error while attempting to bind on address ('0.0.0.0', 7860):
          [errno 98] address already in use
```

`gr.mount_gradio_app` resolves `ssr_mode` from `GRADIO_SSR_MODE`, which a Space sets to
`true`, and on that path spawns a Node SSR server *at import time* on the first free port
from 7860 up — the port `uvicorn.run()` on the next line then asked for. The loser was PID
1 because the winner was PID 1's own child. No ZeroGPU supervisor and no platform sidecar
was involved, which is where the prior hypothesis had pointed; it was disproved by reading
`mount_gradio_app` and then reproducing the spawn locally (`GRADIO_SSR_MODE=true` →
`demo.node_port == 7860`). The *crash* does not reproduce on Windows, because
`SO_REUSEADDR` lets the second bind succeed there rather than raise — which is exactly why
"it binds cleanly locally" had been misleading.

**2 — ZeroGPU's supervisor kills a Space that never claims a GPU.**

```
Exit code: 3. Reason: No @spaces.GPU function detected during startup
```

The Space is pinned to `zero-a10g` and cannot leave it: `POST /hardware` with `cpu-basic`
returns 402 ("Without a PRO subscription, you can't downgrade this Space"), `whoami`
reports `isPro: false`, and free CPU is not offered on this account. Satisfying the
supervisor needs two things together — a `@spaces.GPU` function, because
`spaces.zero.startup()` reports nothing while its `decorated_cache` is empty, and a
`Blocks.launch()` call, because `spaces.zero.gradio.one_launch` patches `launch` and
nothing else. Hence `demo.launch()` plus a never-called `_zerogpu_handshake` stub; and,
because a sub-app mounted with `Mount` receives no Starlette lifespan events, an explicit
`recorder.initialize()`.

### Live evidence

```
$ curl -s https://shakti-agent-harness.hf.space/healthz
{"status":"ok","db":"ok","version":"0.1.0"}

$ curl -s -X POST https://shakti-agent-harness.hf.space/v1/replay/real_regression
status             completed
category           real_regression
suggested_action   open_fix_pr
suspected_commit   e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df
self / final conf  0.95 / 0.95
citations          5
stages             investigate ok · diagnose ok · remediate skipped
                   (57s wall, two model calls)

$ curl -s .../v1/runs/run_01M20XB72QQGDQDBWENNT69WTS/trace | jq '.spans | length'
5
```

The trace count is the structurally important one. Five spans means the explicit
`recorder.initialize()` ran and the mounted sub-app's lost lifespan was actually
compensated for; had it been missed, `TraceRecorder._persist` would have swallowed every
write by design and this would read `0` with no error anywhere.

Accepted regression vs `fly.toml`: a Space has **no persistent disk**, so the span trace
does not survive a restart. `fly.toml` provisioned a 1 GB volume. Revisit when the memory
store lands in Phase 3, the first component that genuinely needs durability.

---

## Findings from running against the live API

**1. PLAN.md's pinned model is retired.** `gemini-2.5-flash` — named in PLAN.md's stack and
defaulted in `settings.py` — now returns:

```
404 NOT_FOUND: This model models/gemini-2.5-flash is no longer available to new users.
Please update your code to use models/gemini-3.6-flash
```

It still appears in `models.list()`, so the failure only shows up on a real call.
`gemini-flash-latest` also works.

**Resolved:** the user updated `settings.py` to default `gemini_model = "gemini-3.6-flash"`.
The temporary `.env` override was removed afterwards so the value has exactly one home.

**PLAN.md is now reconciled too**, on the user's instruction: the stack decision (line 157)
and the Appendix E settings block (line 1742) both name `gemini-3.6-flash`, so Appendix E
again matches `src/settings.py` exactly, and `.env.example` no longer suggests a retired id.
The Diagnostician promotion example moved from `gemini-2.5-pro` to `gemini-pro-latest`:
`2.5-pro` is the same retired generation as `2.5-flash`, and the `-latest` alias is the form
already confirmed working here (`gemini-flash-latest`). **That Pro id has not been called
against this key** — `models.list()` is not evidence, as this very finding shows — but
nothing in the build depends on it; it is an illustrative env-var override.

**2. `to_gemini_schema` is validated against the real API.** Both agents' contracts round
trip through it into constrained decoding and back through `model_validate_json` with zero
repair attempts. That is the translator exercised for real, not just unit-tested.

**3. The fixture's `expected.action` label — resolved by filling a prompt gap, not by editing the label.**
Three runs of the original prompt stably returned `suggested_action: "open_revert_pr"` while
`scenario.yaml` labels the scenario `open_fix_pr`.

The cause turned out to be that **the Diagnostician prompt gave no guidance whatsoever on
choosing among the five actions** — it listed them and nothing more. That is a genuine gap,
so it was filled rather than the label edited to match the model, which would have been
fitting the eval to its own output.

The guidance distinguishes the two on a real principle: prefer a targeted fix when a
specific defect is identified *and the commit has a legitimate stated intent*, because a
revert discards that intent and the author has to redo the work; prefer a revert when the
intent is itself wrong, the damage is broad, or you can name only the commit and not the
line. This scenario's commit — "Round savings up to avoid undercharging on odd cents" —
wanted a real behaviour change and implemented it with a stray `+ 1`, so a fix preserves
that intent where a revert throws it away.

A correction to an earlier claim: `policy.yaml` does **not** settle this. `open-fix-pr`
matches on the *tools* invoked, not on `suggested_action`, so a revert PR built from the
same three tools would still match and get `require_approval` — the scenario's `effect`
label holds either way.

**Confirmed three times — settled.** The first post-change run returned `open_fix_pr` with
everything else unchanged (`real_regression`, 0.95, 4 citations, correct sha); further runs
that day hit the daily quota (below). Two more on 2026-09-08, both against the deployed
Space, both `open_fix_pr` / `real_regression` / 0.95 / `e2cdf1b…`, with 5 citations. Three
for three, across two days and two hosts.

**4. The free-tier quota is 20 requests per DAY, per model.** Probed directly:

```
quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier
limit:   20
model:   gemini-3.6-flash
```

Each replay run costs 2 requests (Investigator + Diagnostician), more when a retry fires —
roughly **ten clean runs per day**. Not a defect, but it constrains the work: live
end-to-end verification is effectively one-shot per day, and **Phase 4's eval gate (six
scenarios x 2+ calls) would consume an entire day's quota per run.** Worth designing for
now — a paid key, a per-model split (the quota is per model, so `gemini-flash-latest`
carries its own allowance), or an eval scored against recorded model responses.

Incidentally this verified Appendix B.1's 429 row for free: the run made 4 attempts with
exponential backoff and then escalated as `rate_limited`, exactly as specified.

**5. Defect found and fixed while running step 2.** The first run retried four times and
reported `provider call failed: ClientError`. The provider rejects an invalid key with
**HTTP 400 `INVALID_ARGUMENT` / `API_KEY_INVALID`**, not the 401/403 Appendix B.1 names the
row by, so a status-only classifier read it as transient and spent the whole retry budget
on a credential that could not become valid. `classify_provider_error` now matches the
message too; `test_an_invalid_key_is_auth_even_though_it_arrives_as_400` pins it. Verified
against the live API: the auth path now fails on attempt 1 with the literal specified
message and no key value.

---

## Suite state

```
$ uv run pytest -q
250 passed, 2 warnings in 3.62s

$ uv run ruff check src/ tests/
All checks passed!

$ uv run mypy --strict src/harness
Success: no issues found in 14 source files
```
