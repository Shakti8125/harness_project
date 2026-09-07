"""Hugging Face Space entrypoint.

**Not part of the application.** The deployable artifact is `src/api/main.py`, served by
the `Dockerfile` on any container host. This module exists because the Space types
available on a free account run `python app.py` rather than a container, so the app needs
a launcher that speaks their conventions: listen on port 7860, and expose a Gradio
`Blocks` so the SDK has something to render.

The FastAPI app is mounted **unchanged** — every route keeps its path, so `/healthz`,
`/v1/replay/{scenario}` and `/v1/runs/{id}/trace` behave here exactly as they do under
`docker compose`. The Gradio UI is a thin client of that same HTTP surface, calling it
in-process through an ASGI transport rather than reaching around it into the orchestrator.
That is deliberate: the demo exercises the real request path, so if the UI works, the API
worked.

This is **not** PLAN.md's Phase 5 trace view (`/runs/{id}/view` + Jinja). That is a
different, richer artifact against a durable trace. This is a launcher with a form on it.
"""

from __future__ import annotations

import json
from typing import Any

import gradio as gr
import httpx
import uvicorn

from src.api.main import app as api

SCENARIOS = ["real_regression"]

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
        "_Two model calls per run. The free API tier allows ~10 runs per day, "
        "after which runs escalate as `rate_limited` rather than failing._"
    )
    with gr.Tab("Diagnosis"):
        headline = gr.Markdown()
    with gr.Tab("Stages"):
        stages = gr.Markdown()
    with gr.Tab("Raw RunOutcome"):
        raw = gr.Code(language="json")

    run.click(run_scenario, inputs=scenario, outputs=[headline, stages, raw])

# Mounted last and at the root, so every FastAPI route registered above keeps priority and
# only unmatched paths fall through to the UI.
app = gr.mount_gradio_app(api, demo, path="/")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=SPACE_PORT)  # noqa: S104
