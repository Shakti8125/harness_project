"""Hugging Face Space entrypoint.

**Not part of the application.** The deployable artifact is `src/api/main.py`, served by
the `Dockerfile` on any container host. This module exists because the Space types
available on a free account run `python app.py` rather than a container, so the app needs
a launcher that speaks their conventions: listen on port 7860, and expose a Gradio
`Blocks` so the SDK has something to render.

The FastAPI app is mounted **unchanged**, at the root of gradio's own app — every route
keeps its path, so `/healthz`, `/v1/replay/{scenario}` and `/v1/runs/{id}/trace` behave
here exactly as they do under `docker compose`. The Gradio UI is a thin client of that
same HTTP surface, calling it in-process through an ASGI transport rather than reaching
around it into the orchestrator. That is deliberate: the demo exercises the real request
path, so if the UI works, the API worked.

This is **not** PLAN.md's Phase 5 trace view (`/runs/{id}/view` + Jinja). That is a
different, richer artifact against a durable trace. This is a launcher with a form on it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import gradio as gr
import httpx

from src.api.deps import get_app_context
from src.api.main import app as api
from src.integrations.cicd.rendering import validate_prompt_templates

try:
    import spaces
except ImportError:  # Not a ZeroGPU Space. See the handshake block at the bottom.
    spaces = None

SCENARIOS = ["real_regression", "flaky_test", "infra_timeout", "cold_start"]

#: The port a Space serves on. A literal, not an environment read: `tests/unit/
#: test_no_env_access.py` walks the whole repo and `src/settings.py` is the only module
#: allowed to touch the environment. That rule is worth more than the flexibility, and the
#: platform fixes this value anyway.
SPACE_PORT = 7860


async def _call_api(path: str, method: str = "POST") -> dict[str, Any]:
    """One in-process request against the mounted app, through the real route."""
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://harness", timeout=300.0
    ) as client:
        response = await client.request(method, path)
    return {"_status": response.status_code, **response.json()}


async def run_scenario(scenario: str) -> tuple[str, str, str]:
    """Replay one scenario and render the parts a human actually reads."""
    outcome = await _call_api(f"/v1/replay/{scenario}")

    if outcome.get("_status") != 200:
        return (
            f"### Request failed ({outcome.get('_status')})\n\n"
            f"{outcome.get('detail', 'no detail')}",
            "",
            json.dumps(outcome, indent=2),
        )

    diagnosis = (outcome.get("final") or {}).get("diagnosis")
    status = outcome.get("status")

    if not diagnosis:
        escalation = outcome.get("escalation") or {}
        headline = (
            f"### Run {status}\n\n"
            f"**{escalation.get('reason', 'no diagnosis produced')}** — "
            f"{escalation.get('message', '')}\n\n"
            "No diagnosis was produced. The stage table below shows where it stopped."
        )
        return headline, _render_stages(outcome), json.dumps(outcome, indent=2)

    citations = "\n".join(
        f"- `{c['claim_kind']}` · {c['locator']}\n  > {c['quote']}"
        for c in diagnosis.get("citations", [])
    )
    adjustments = diagnosis.get("confidence_adjustments") or []
    adjustment_text = (
        "\n".join(
            f"- `{a['name']}` **{a['delta']:+.2f}** — {a['reason']}" for a in adjustments
        )
        or "_none fired — the self-reported score stood unchanged_"
    )

    headline = f"""### {diagnosis['category']} · confidence {diagnosis['final_confidence']:.2f}

{diagnosis['summary']}

**Suggested action:** `{diagnosis['suggested_action']}`
**Suspected commit:** `{diagnosis.get('suspected_commit_sha') or '—'}`
**Run status:** `{status}` · **trace:** `{outcome.get('trace_url')}`

#### Reasoning
{diagnosis['reasoning']}

#### Citations ({len(diagnosis.get('citations', []))})
{citations or '_none_'}

#### Confidence calibration
Model self-reported **{diagnosis['self_confidence']:.2f}**, harness adjusted to \
**{diagnosis['final_confidence']:.2f}**:

{adjustment_text}
"""
    return headline, _render_stages(outcome), json.dumps(outcome, indent=2)


def _render_stages(outcome: dict[str, Any]) -> str:
    rows = [
        "| stage | agent | status | attempts | tokens | ms |",
        "|---|---|---|---|---|---|",
    ]
    rows.extend(
        f"| {s['stage']} | {s.get('agent') or '—'} | {s['status']} | {s['attempts']} "
        f"| {s['tokens']['total']} | {s['duration_ms']} |"
        for s in outcome.get("stages", [])
    )
    degraded = outcome.get("degraded_components") or []
    if degraded:
        rows.append("")
        rows.append(f"**Degraded:** {', '.join(degraded)}")
    return "\n".join(rows)


with gr.Blocks(title="Agent Harness") as demo:
    gr.Markdown(
        """# Agent Harness

A failed CI run goes in; a validated, evidence-cited diagnosis comes out. Everything
below replays from recorded fixtures — no repository is contacted, and writes are off.

The REST API is the real artifact and is live on this same URL:
`GET /healthz` · `POST /v1/replay/{scenario}` · `GET /v1/runs/{id}` ·
`GET /v1/runs/{id}/trace`
"""
    )
    with gr.Row():
        scenario = gr.Dropdown(
            SCENARIOS, value=SCENARIOS[0], label="Scenario", scale=3
        )
        run = gr.Button("Diagnose", variant="primary", scale=1)
    gr.Markdown(
        "_Three model calls per run. The free API tier allows ~6 runs per day, "
        "after which runs escalate as `rate_limited` rather than failing. Memory is "
        "real but this Space has no persistent disk: replay `flaky_test` four times in "
        "one sitting to see the fourth run recognise the first three._"
    )
    with gr.Tab("Diagnosis"):
        headline = gr.Markdown()
    with gr.Tab("Stages"):
        stages = gr.Markdown()
    with gr.Tab("Raw RunOutcome"):
        raw = gr.Code(language="json")

    run.click(run_scenario, inputs=scenario, outputs=[headline, stages, raw])


# This Space is pinned to `zero-a10g` hardware and cannot leave it: downgrading to
# `cpu-basic` is a PRO feature (`402 Payment Required`), and free CPU is not offered on
# this account at all. So a CPU-only application runs on GPU hardware, whose supervisor
# kills anything that never claims a GPU:
#
#     Exit code: 3. Reason: No @spaces.GPU function detected during startup
#
# Satisfying it takes two things, and only together. One decorated function, because
# `spaces.zero.startup()` reports nothing when its `decorated_cache` is empty. And a
# `Blocks.launch()` call, because `spaces.zero.gradio.one_launch` patches `launch` and
# nothing else — that patch is the only thing that ever fires the report. Hence this stub
# and the `demo.launch()` in `main()`; either alone leaves the Space dead.
#
# Anywhere else this costs nothing. `spaces` is not installed off a Space, so the guarded
# import above leaves `spaces is None`; and even where it is installed,
# `spaces.zero.decorator._GPU` returns the function untouched unless `SPACES_ZERO_GPU` is
# set, so nothing is registered and nothing is reported.
if spaces is not None:

    @spaces.GPU(duration=1)
    def _zerogpu_handshake() -> None:
        """Declared so ZeroGPU's supervisor can see a GPU function, and never called.

        There is no GPU work anywhere in this project, and this does not pretend
        otherwise — it is the smallest honest thing that satisfies a platform
        precondition the application did not ask for.
        """


def main() -> None:
    """Serve the API and the UI together, on the one port the platform routes to."""
    # `src/api/main.py`'s lifespan is what applies the schema migrations (Phase 3: the
    # `trace_span`, `run`, `approval`, `escalation` and memory tables), and a sub-app
    # mounted with `Mount` never receives Starlette lifespan events — so mounting `api`
    # under gradio below would silently skip it. The symptom would not be an error:
    # `TraceRecorder._persist` catches and logs by design, because tracing must never fail
    # a run. It would be an empty `GET /v1/runs/{id}/trace`, discovered much later. So run
    # it here, explicitly, through the one startup routine the lifespan also calls
    # (`AppContext.initialize`) — the third hand-call the Phase 3 handoff predicted, folded
    # into the previous one so a fourth cannot be forgotten. `get_app_context()` is
    # `lru_cache`d, so this is the same store and recorder the routes use, and neither
    # holds a connection between calls — a throwaway event loop is safe.
    #
    # `validate_prompt_templates()` belongs here for the same reason and needs the same
    # duplication: `main.py`'s lifespan (see its docstring) runs it for the Docker
    # deployment, but a `Mount`ed sub-app never receives that lifespan, so the Space needs
    # its own call. Ordered first, before the recorder work, so a packaging fault (a
    # missing or malformed `prompts/*.md`) is reported before anything else runs — matching
    # the ordering rationale in `main.py`'s lifespan docstring — and left to raise, because
    # a Space that cannot load its prompts must fail loudly at boot rather than 500 on the
    # first request with no RFC 9457 body, no `run_id`, and nothing in the trace.
    validate_prompt_templates()
    asyncio.run(get_app_context().initialize())

    # `ssr_mode=False` is load-bearing, not a preference. Left to resolve itself, gradio
    # reads `GRADIO_SSR_MODE` — which a Space sets to `true` — and spawns a Node server on
    # the first free port from 7860 up. An earlier revision paired `mount_gradio_app` with
    # `uvicorn.run(port=7860)` and lost the port to its own Node child:
    #
    #     INFO:     Application startup complete.
    #     ERROR:    [Errno 98] error while attempting to bind on address ('0.0.0.0', 7860):
    #               [errno 98] address already in use
    #
    # Client-side rendering keeps one server on the one port, which is all this needs.
    demo.launch(
        server_name="0.0.0.0",  # noqa: S104
        server_port=SPACE_PORT,
        ssr_mode=False,
        prevent_thread_lock=True,
    )

    # Mounted at the root, and necessarily *after* launch, because `demo.app` does not
    # exist until `launch()` builds it. Gradio's own routes are registered by then so they
    # keep priority, and gradio defines no catch-all, so `/healthz`, `/v1/*` and everything
    # else fall through to the API with their paths intact. The single collision is
    # `/openapi.json`, which gradio claims; the harness's own schema stays canonical on the
    # Docker path.
    demo.app.mount("/", api)

    demo.block_thread()


if __name__ == "__main__":
    main()
