# Deploying to Hugging Face Spaces

Phase 1's target was Fly.io. Fly now demands payment information before it will create an
app, and a Docker Space was not available on this account either — so the "reachable over
the public internet" check is satisfied with a **Gradio Space**, which runs a long-lived
container.

Live at <https://shakti-agent-harness.hf.space>. The Space sits on `zero-a10g` hardware,
not by choice — see §2 below; that is the single most surprising thing in this document and
it shapes the entrypoint.

`fly.toml` and the `Dockerfile` are both kept, unchanged in substance. Any container host
still serves `src/api/main.py` directly; the Space is an additional target, not a
replacement.

## How a Gradio Space runs this

A Gradio Space runs `python app.py` and serves whatever answers on port 7860. `app.py`
launches gradio, then mounts the **real FastAPI app, unchanged**, at the root of gradio's
own app:

```python
demo.launch(
    server_name="0.0.0.0", server_port=7860, ssr_mode=False, prevent_thread_lock=True
)
demo.app.mount("/", api)
demo.block_thread()
```

Mount order is the whole trick: gradio's own routes are registered by the time `launch()`
returns, so they keep priority, and gradio defines no catch-all — `/healthz`,
`/v1/replay/{scenario}`, `/v1/runs` and `/v1/runs/{id}/trace` all fall through to the API
with their paths intact. `/openapi.json` is the single route gradio claims; the harness's
own schema stays canonical on the Docker path.

Three things about this shape are load-bearing, and none of them are obvious.

### 1. `ssr_mode=False`

Leaving it to resolve itself is what broke the first otherwise-working deploy. The original
entrypoint paired `gr.mount_gradio_app(api, demo, path="/")` with `uvicorn.run(port=7860)`.
`mount_gradio_app` reads `GRADIO_SSR_MODE`, which a Space sets to `true`, and on that path
it spawns a **Node server at import time** on the first free port from 7860 up — the port
`uvicorn.run()` on the next line was about to ask for. So the module took 7860 from itself:

```
INFO:     Application startup complete.
ERROR:    [Errno 98] error while attempting to bind on address ('0.0.0.0', 7860):
          [errno 98] address already in use
```

The loser is PID 1 because the winner is PID 1's own child — there is no second container
and no platform sidecar involved. Turning SSR off costs nothing: gradio starts that Node
server *without* passing it a `python_port`, so it could not proxy back to this app even
when it wins the race. Client-side rendering keeps one server on the one port the platform
routes to, which is all this needs.

**It does not reproduce on Windows**, which is what made it expensive to find. Two things
hide it: `GRADIO_SSR_MODE` is unset locally, and even with it set, Windows lets a second
socket bind a port already in use under `SO_REUSEADDR` — so uvicorn *succeeds*, and the two
servers silently split incoming connections instead of one of them dying. Look for the
spawn rather than waiting for the crash; on the old mount-based entrypoint,

```bash
GRADIO_SSR_MODE=true python -c "import app; print(getattr(app.demo, 'node_port', None))"
```

printed `7860` — a Node server already sitting on uvicorn's port, at import time.

### 2. `demo.launch()`, and a `@spaces.GPU` stub

This Space is pinned to **`zero-a10g`** hardware and cannot leave it. `POST /hardware` with
`cpu-basic` returns `402 Payment Required` ("Without a PRO subscription, you can't downgrade
this Space"), `whoami` reports `isPro: false`, and free CPU is not offered on this account.
So a CPU-only application runs on GPU hardware, and that hardware's supervisor kills
anything that never claims a GPU:

```
Exit code: 3. Reason: No @spaces.GPU function detected during startup
```

Satisfying it takes two things, and only together:

- **one decorated function** — `spaces.zero.startup()` returns early and reports nothing
  while its `decorated_cache` is empty;
- **a `Blocks.launch()` call** — `spaces.zero.gradio.one_launch` patches `launch` and
  nothing else, and that patch is the only thing that ever fires the report.

That is why the entrypoint calls `demo.launch()` rather than driving uvicorn itself, and
why `app.py` carries a `_zerogpu_handshake` stub that is never called. Neither is wanted;
both are the platform's price. Off a ZeroGPU Space the whole block disappears: `spaces` is
not installed (the import is guarded), and where it is installed,
`spaces.zero.decorator._GPU` returns the function untouched unless `SPACES_ZERO_GPU` is
set — verified, with `decorated_cache` empty and `launch` unpatched.

### 3. `recorder.initialize()`, explicitly

`src/api/main.py`'s lifespan is what creates the `spans` table, and **a sub-app mounted with
`Mount` never receives Starlette lifespan events**. Mounting `api` under gradio therefore
skips it. Losing it would not raise: `TraceRecorder._persist` catches and logs write
failures by design, because tracing must never fail a run. The only symptom would be an
empty `GET /v1/runs/{id}/trace`, found much later. So `main()` runs it directly —
`get_app_context()` is `lru_cache`d, so it is the same recorder the routes use, and the
recorder holds no connection between calls, which makes a throwaway event loop safe.

### Verifying the entrypoint locally

Use a venv resolved the way the Space resolves one, and set the variable the Space sets —
the project venv will not do, because it holds `uv.lock`'s pins rather than the Space's:

```bash
uv venv /tmp/spacevenv --python 3.12
uv pip install --python /tmp/spacevenv -r requirements.txt "gradio[oauth,mcp]==6.26.0"
GRADIO_SSR_MODE=true /tmp/spacevenv/bin/python app.py
```

```
$ curl -s localhost:7860/healthz          → {"status":"ok","db":"ok","version":"0.1.0"}
$ curl -s -o /dev/null -w '%{http_code} %{content_type}' localhost:7860/
                                          → 200 text/html; charset=utf-8   (the UI)
$ curl -s -o /dev/null -w '%{http_code}' localhost:7860/config    → 200    (gradio's own)
$ curl -s localhost:7860/v1/runs          → {"items":[],"next_cursor":null}   (the API)
```

Check `netstat`/`ss` for exactly **one** listener on 7860 while doing this. Two means SSR
is still spawning Node, and on Linux that is fatal rather than merely confusing.

The UI calls the API in-process through an ASGI transport rather than reaching around it
into the orchestrator, so the demo exercises the real request path: if the UI works, the
API worked. It is **not** PLAN.md's Phase 5 trace view (`/runs/{id}/view` + Jinja) — that
is a richer artifact over a durable trace, and this is a launcher with a form on it.

## Files this added

| File | Why |
|---|---|
| `app.py` | Space entrypoint. Not imported by the app, the image or the tests. |
| `requirements.txt` | Spaces install with pip, not uv. **Deliberately unpinned** — see below. |
| `README.md` front matter | `sdk: gradio`, `sdk_version: 6.26.0`, `app_file: app.py`. |

`requirements.txt` lists only the **direct** dependencies from `pyproject.toml`, at the same
constraints, unpinned. Do **not** regenerate it with `uv export`: a full lock export cannot
co-resolve with the gradio a Space installs over the top, and the build dies before it ever
runs anything —

```
gradio 6.26.0 depends on pydantic>=2.11.10,<=2.12.5
you require pydantic==2.13.5
-> No solution found when resolving dependencies
```

`pyproject.toml` + `uv.lock` stay the source of truth for the project, the Docker image and
CI. The price of unpinning, stated rather than discovered later: the Space runs a slightly
different transitive set than the lock. Today that is **pydantic 2.12.5** and **starlette
1.6.0**. Run the suite against whatever it actually resolves before publishing, and again
whenever `requirements.txt` or `sdk_version` changes:

```bash
uv run --with "pydantic==2.12.5" --with "starlette==1.6.0" pytest -q
```

**`gradio` and `uvicorn` are deliberately absent** — a Gradio Space installs both itself,
gradio at the `sdk_version` pinned in the README, and listing them here lets pip resolve a
second, conflicting copy over the top.

## Steps

**1. Create the Space.** <https://huggingface.co/new-space> → SDK **Gradio**, blank
template, visibility **Public**, name `agent-harness`.

**2. Set the three required secrets.** Space → Settings → *Variables and secrets* → **New
secret**:

| Name | Notes |
|---|---|
| `HARNESS_GEMINI_API_KEY` | required — the app refuses to boot without it |
| `HARNESS_GITHUB_TOKEN` | required by `Settings`; unused while `HARNESS_GATEWAY=replay` |
| `HARNESS_GITHUB_WEBHOOK_SECRET` | required by `Settings`; unused until the webhook phase |

All three are non-optional in `src/settings.py`, and the process fails fast at boot rather
than on first request — so a missing one appears immediately in the Space log, naming the
field.

Nothing else needs setting. `HARNESS_GATEWAY=replay` and `HARNESS_DRY_RUN=true` are the
defaults, and `HARNESS_DATABASE_PATH` defaults to `./data/harness.db`, which the Space's
working directory makes writable.

**3. Push.** Spaces default to `main`; this repo is on `master`.

```bash
git remote add space https://huggingface.co/spaces/<your-username>/agent-harness
git push space master:main
```

The password prompt wants a write-scoped **access token**
(<https://huggingface.co/settings/tokens>), not the account password. `hf auth login`
beforehand avoids the prompt.

**4. Verify.** Poll `https://huggingface.co/api/spaces/<user>/agent-harness` until
`runtime.stage` is `RUNNING` — `BUILDING` → `APP_STARTING` → `RUNNING` takes about a
minute — then, against the live Space:

```bash
curl -s https://<your-username>-agent-harness.hf.space/healthz
# {"status":"ok","db":"ok","version":"0.1.0"}

curl -s -X POST https://<your-username>-agent-harness.hf.space/v1/replay/real_regression \
  | jq -r '.status, .final.diagnosis.category, .final.diagnosis.suggested_action'
# completed
# real_regression
# open_fix_pr          (~57s: two model calls)

curl -s https://<your-username>-agent-harness.hf.space/v1/runs/<run_id>/trace | jq '.spans | length'
# 7                    (any non-zero count is the real invariant; 0 would mean
#                       the explicit recorder.initialize() was lost)
```

The trace check is not decoration. It is the one assertion that catches a mounted sub-app
silently losing its lifespan, which fails quietly by design — see §3 above.

Space logs are not readable from the public endpoint (it 401s); go through the API with the
token `hf auth login` stored:

```python
import httpx
from huggingface_hub.utils import build_hf_headers
httpx.get("https://huggingface.co/api/spaces/<user>/agent-harness/logs/build",
          headers=build_hf_headers(), timeout=25.0).text      # or .../logs/run
```

`logs/run` is a live SSE stream while the Space is `RUNNING`, so a plain `get` blocks until
it times out; that is expected, and the useful content arrives first.

## Two things to know before making it public

**Storage is ephemeral.** A Space has no persistent disk, so `data/harness.db` resets
whenever the Space restarts or sleeps; `fly.toml` provisioned a 1 GB volume for this. What
it costs: the span trace and the in-process run registry do not survive a restart. It does
not affect the demo — `POST /v1/replay/{scenario}` returns the whole `RunOutcome` in the
same response that produced it, and every scenario replays from files in the repo. Revisit
when the memory store lands in Phase 3, the first component that genuinely needs durability.

**There is no authentication on any endpoint,** and the free model tier allows **20 requests
per day**. Each replay costs two, so anyone who finds the URL can exhaust the day's quota in
about ten requests. Nothing worse is exposed: `HARNESS_DRY_RUN=true` and
`HARNESS_GATEWAY=replay` mean no repository is ever written to, and every response comes
from recorded fixtures. A private Space removes the risk entirely but also removes the
public reachability the check is about. Real auth is not Phase 1 work and is not in PLAN.md
— it belongs with the webhook phase, the first time an outside caller is meant to reach
this service at all.
