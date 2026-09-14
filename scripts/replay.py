# ruff: noqa: E501
"""Run one scenario -- recorded or live -- from the command line and print what happened.

    uv run python scripts/replay.py real_regression
    uv run python scripts/replay.py --live --repo <owner>/<name> --run-id <workflow_run_id>
    uv run python scripts/replay.py --post-signed fixtures/scenarios/flaky_test/webhook.json [--wait]

`--post-signed` (PLAN.md Phase 5, Verify steps 3-4) is the webhook client: it signs the
file's bytes with `HARNESS_GITHUB_WEBHOOK_SECRET` exactly as GitHub would, posts them to
`--url` (default `http://127.0.0.1:8000`) with the three GitHub headers, and prints the
status and the body's `run_id` / `status` / `original_run_id`. `--wait` then polls
`GET /v1/runs/{id}` until the run leaves `in_progress`. A second post of the same file
prints the `deduplicated` line Appendix C promises.

Replay mode drives the same pipeline the API's `POST /v1/replay/{scenario}` drives, minus
HTTP. Live mode (PLAN.md Phase 2, Verify step 5) fetches the named workflow run from the
GitHub API to build the subject, then runs the pipeline over `GitHubToolGateway`. Both
honour every setting the service does -- in particular `HARNESS_DRY_RUN`, which defaults
to true, so a live run reads everything and writes nothing unless you say otherwise --
and both spend real model calls (two or three per run on the free tier's 20/day).

Prints the diagnosis, the remediation decision, and a summary of every gateway span in the
trace, which is what step 5 asks to be checked: in a dry run every gateway span with a
write tool reports `dry_run=True`, and nothing destructive appears at all.

`--json` prints the *internal* `RunOutcome` -- raw log excerpts, patches and drafted file
content included -- not the digested, scrubbed body the HTTP boundary serves. It is a local
operator's view of the run, on the operator's own terminal; do not paste it anywhere public.
A pending `approval_id` printed here is decidable only by the API process that would have
raised it, which this script is not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.deps import FIXTURES_ROOT, get_app_context  # noqa: E402
from src.api.main import idempotency_key_for  # noqa: E402
from src.api.webhook import DELIVERY_HEADER, EVENT_HEADER, SIGNATURE_HEADER, sign  # noqa: E402
from src.harness.contracts import RunOutcome, RunRequest  # noqa: E402
from src.harness.memory import MemoryStoreError  # noqa: E402
from src.integrations.cicd.agents.investigator import parse_subject  # noqa: E402
from src.integrations.cicd.wiring import INTEGRATION  # noqa: E402


async def fetch_workflow_run(repo: str, run_id: int, token: str, api_base: str) -> dict[str, Any]:
    """The `workflow_run` object, in the shape the webhook would have delivered it."""
    async with httpx.AsyncClient(
        base_url=api_base.rstrip("/"),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    ) as client:
        response = await client.get(f"/repos/{repo}/actions/runs/{run_id}")
        response.raise_for_status()
        run = response.json()
    return {
        "action": "completed",
        "workflow_run": run,
        "repository": run.get("repository") or {"full_name": repo},
    }


def print_outcome(outcome: RunOutcome) -> None:
    diagnosis = outcome.final.get("diagnosis")
    remediation = outcome.final.get("remediation")
    print(f"run_id:      {outcome.run_id}")
    print(f"status:      {outcome.status}")
    if outcome.escalation is not None:
        print(f"escalation:  {outcome.escalation.reason}: {outcome.escalation.message}")
    if isinstance(diagnosis, dict):
        print(
            f"diagnosis:   {diagnosis.get('category')} "
            f"(final_confidence {diagnosis.get('final_confidence'):.2f}, "
            f"{len(diagnosis.get('citations', []))} citations, "
            f"suggested {diagnosis.get('suggested_action')})"
        )
        print(f"             {diagnosis.get('summary')}")
    if isinstance(remediation, dict):
        decisions = remediation.get("decisions", [])
        first = decisions[0] if decisions else None
        print(
            f"remediation: action={remediation.get('plan', {}).get('action')} "
            f"status={remediation.get('status')}"
            + (f" rule={first.get('rule_id')} effect={first.get('effect')}" if first else "")
        )
        for executed in remediation.get("executed", []):
            print(
                f"  executed {executed.get('tool')}: ok={executed.get('ok')} "
                f"dry_run={executed.get('dry_run')}"
            )
        pending = remediation.get("pending_approval")
        if pending:
            print(f"  pending approval {pending.get('approval_id')} (expires {pending.get('expires_at')})")
    print(f"trace:       {outcome.trace_url}")


async def print_gateway_spans(run_id: str) -> None:
    trace = await get_app_context().recorder.read_trace(run_id)
    if trace is None:
        print("gateway spans: (no trace recorded)")
        return
    spans = [s for s in trace.spans if s.component == "gateway"]
    print(f"gateway spans: {len(spans)}")
    for span in spans:
        attrs = span.attributes
        print(
            f"  {span.name:22} tool={attrs.get('tool')!s:26} side_effect={attrs.get('side_effect')!s:6}"
            f" ok={attrs.get('ok')!s:5} dry_run={attrs.get('dry_run')!s:5} rule={attrs.get('rule_id')}"
        )
    writes = [s for s in spans if s.attributes.get("side_effect") != "read"]
    if not writes:
        print("  every gateway span is a read")


TERMINAL_STATUSES = frozenset({"completed", "escalated", "awaiting_approval", "failed", "deduplicated"})


async def post_signed(path: Path, url: str, secret: str, *, wait: bool, timeout_s: float) -> int:
    """Sign `path`'s bytes and deliver them like GitHub would; print what came back."""
    body = path.read_bytes()
    headers = {
        "Content-Type": "application/json",
        EVENT_HEADER: "workflow_run",
        DELIVERY_HEADER: str(uuid.uuid4()),
        SIGNATURE_HEADER: sign(secret, body),
        "User-Agent": "GitHub-Hookshot/replay.py",
    }
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=timeout_s) as client:
        response = await client.post("/webhooks/github", content=body, headers=headers)
        payload: dict[str, Any] = {}
        if response.headers.get("content-type", "").startswith(("application/json", "application/problem+json")):
            try:
                payload = response.json()
            except ValueError:
                payload = {}
        summary = {k: payload.get(k) for k in ("run_id", "status", "original_run_id") if k in payload}
        if "detail" in payload:
            summary["detail"] = payload["detail"]
        print(response.status_code, json.dumps(summary, separators=(",", ":")))
        run_id = payload.get("run_id")
        if not wait or not isinstance(run_id, str) or payload.get("status") in TERMINAL_STATUSES:
            return 0 if response.status_code < 400 else 1
        while True:
            await asyncio.sleep(1.0)
            polled = await client.get(f"/v1/runs/{run_id}")
            state = polled.json() if polled.status_code == 200 else {}
            if state.get("status") in TERMINAL_STATUSES:
                diagnosis = (state.get("final") or {}).get("diagnosis") or {}
                print(
                    f"{run_id}: {state.get('status')}"
                    + (f" ({diagnosis.get('category')} {diagnosis.get('final_confidence'):.2f})" if diagnosis else "")
                )
                return 0
            print(f"{run_id}: {state.get('status', polled.status_code)} ...", file=sys.stderr)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", nargs="?", help="a directory under fixtures/scenarios/")
    parser.add_argument("--live", action="store_true", help="run against the GitHub API")
    parser.add_argument("--repo", help="owner/name (live mode)")
    parser.add_argument("--run-id", type=int, help="the failing workflow_run id (live mode)")
    parser.add_argument("--json", action="store_true", help="print the full RunOutcome as JSON")
    parser.add_argument("--post-signed", type=Path, metavar="WEBHOOK_JSON", help="sign and POST a webhook body to a running server")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="the server for --post-signed")
    parser.add_argument("--wait", action="store_true", help="with --post-signed: poll the run until it finishes")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds for --post-signed")
    args = parser.parse_args()

    if args.post_signed is not None:
        if not args.post_signed.is_file():
            parser.error(f"no such file: {args.post_signed}")
        from src.settings import get_settings

        secret = get_settings().github_webhook_secret.get_secret_value()
        return await post_signed(args.post_signed, args.url, secret, wait=args.wait, timeout_s=args.timeout)

    context = get_app_context()
    await context.initialize()
    settings = context.settings

    if args.live:
        if not args.repo or not args.run_id:
            parser.error("--live requires --repo and --run-id")
        if settings.gateway != "github":
            parser.error("live mode requires HARNESS_GATEWAY=github")
        if args.repo not in settings.allowed_repos:
            parser.error(f"{args.repo!r} is not in HARNESS_ALLOWED_REPOS")
        subject = await fetch_workflow_run(
            args.repo, args.run_id, settings.github_token.get_secret_value(),
            str(settings.github_api_base),
        )
        gateway = context.build_live_gateway(args.repo)
        mode = "live"
        fixture = None
        print(f"live run against {args.repo} run {args.run_id} (dry_run={settings.dry_run})")
    else:
        if not args.scenario:
            parser.error("name a scenario, or pass --live")
        scenario_dir = FIXTURES_ROOT / args.scenario
        if not (scenario_dir / "webhook.json").is_file():
            parser.error(f"no scenario named {args.scenario!r} under {FIXTURES_ROOT}")
        subject = json.loads((scenario_dir / "webhook.json").read_text(encoding="utf-8"))
        gateway = context.build_replay_gateway(scenario_dir, parse_subject(subject)["repo"])
        mode = "replay"
        fixture = args.scenario
        print(f"replaying {args.scenario} (dry_run={settings.dry_run})")

    request = RunRequest(
        integration=INTEGRATION,
        subject=subject,
        idempotency_key=idempotency_key_for(subject),
        mode=mode,  # type: ignore[arg-type]
        replay_fixture=fixture,
        requested_by="scripts/replay.py",
    )
    # Claimed under a nonce-suffixed key, like the API's `fresh=true` replay: an operator
    # re-running a scenario from the command line wants a new run, not a `deduplicated`
    # answer. The row still records which webhook it came from, and the outcome is saved
    # so `GET /v1/runs/{id}` and memory both know about it. Both store calls degrade the
    # way the API's do (PLAN.md Phase 3 amendment 8): a store that cannot be reached
    # costs the row, never the run or its printed outcome (audit finding 9).
    run_id = None
    try:
        claim = await context.store.claim_run(
            f"{request.idempotency_key}#fresh:{secrets.token_hex(4)}", INTEGRATION
        )
        run_id = claim.run_id
    except MemoryStoreError:
        print("memory store unavailable: running unclaimed", file=sys.stderr)
    try:
        outcome = await context.build_orchestrator_for(gateway, run_id=run_id).run(request)
    finally:
        await gateway.aclose()

    if args.json:
        print(json.dumps(outcome.model_dump(mode="json"), indent=2))
    else:
        print_outcome(outcome)
    try:
        await context.store.save_run(outcome)
    except MemoryStoreError:
        print(f"memory store unavailable: outcome {outcome.run_id} not recorded", file=sys.stderr)
    await print_gateway_spans(outcome.run_id)
    return 0 if outcome.status in ("completed", "awaiting_approval", "escalated") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
