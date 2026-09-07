# Deploying to Hugging Face Spaces

Phase 1's target was Fly.io. Fly now demands payment information before it will create an
app, and a Docker Space was not available on this account either — so the "reachable over
the public internet" check is satisfied with a **Gradio Space**, which is free and runs a
long-lived container.

`fly.toml` and the `Dockerfile` are both kept, unchanged in substance. Any container host
still serves `src/api/main.py` directly; the Space is an additional target, not a
replacement.

## How a Gradio Space runs this

A Gradio Space runs `python app.py` and serves whatever answers on port 7860. `app.py`
mounts the **real FastAPI app, unchanged**, and attaches a small Gradio UI at `/`:

```python
app = gr.mount_gradio_app(api, demo, path="/")
```

Mount order matters and is the whole trick: every API route is registered before the mount,
so `/healthz`, `/v1/replay/{scenario}`, `/v1/runs` and `/v1/runs/{id}/trace` keep their
paths and only unmatched paths fall through to the UI. Verified locally against the Space
entrypoint itself:

```
$ uv run --with gradio python app.py
$ curl -s localhost:7860/healthz              → {"status":"ok","db":"ok","version":"0.1.0"}
$ curl -s -o /dev/null -w '%{content_type}' localhost:7860/   → text/html   (the UI)
$ curl -s -o /dev/null -w '%{http_code}' localhost:7860/v1/runs → 200       (still the API)
$ curl -s -X POST localhost:7860/v1/replay/real_regression | jq .status
```

The UI calls the API in-process through an ASGI transport rather than reaching around it
into the orchestrator, so the demo exercises the real request path: if the UI works, the
API worked. It is **not** PLAN.md's Phase 5 trace view (`/runs/{id}/view` + Jinja) — that
is a richer artifact over a durable trace, and this is a launcher with a form on it.

## Files this added

| File | Why |
|---|---|
| `app.py` | Space entrypoint. Not imported by the app, the image or the tests. |
| `requirements.txt` | Spaces install with pip, not uv. **Generated** — see below. |
| `README.md` front matter | `sdk: gradio`, `sdk_version: 6.26.0`, `app_file: app.py`. |

`requirements.txt` is derived, never hand-edited:

```bash
uv export --no-dev --no-emit-project --no-hashes --no-annotate \
  --format requirements-txt -o requirements.txt
```

`pyproject.toml` + `uv.lock` stay the source of truth. **`gradio` is deliberately not in
it** — a Gradio Space installs gradio itself at the `sdk_version` pinned in the README, and
listing it in both places lets pip resolve a different version over the top, which is a
common way to break a Space build.

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

**4. Verify.**

```bash
curl -s https://<your-username>-agent-harness.hf.space/healthz
# {"status":"ok","db":"ok","version":"0.1.0"}

curl -s -X POST https://<your-username>-agent-harness.hf.space/v1/replay/real_regression \
  | jq -r '.final.diagnosis.category'
# real_regression
```

## Two things to know before making it public

**Storage is ephemeral.** A free Space has no persistent disk, so `data/harness.db` resets
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
