# ruff: noqa: E501
"""Record a failing workflow run from a real repository as a replayable scenario.

    uv run python scripts/record_fixture.py --repo <owner>/<name> --run-id <workflow_run_id> --name <scenario>

Writes `fixtures/scenarios/<name>/` in the exact layout `fixtures/README.md` specifies --
`webhook.json`, `api/GET_<slug>.json` per read call, `logs/job_<id>.txt`, and a
`scenario.yaml` carrying only the keys the recording determines -- so the scenario replays
through `ReplayToolGateway` and scores through `scripts/eval.py` once a person fills in the
label.

**No model call.** The Investigator's deterministic collection (`Investigator.collect`) is
driven over a recording wrapper around the live `GitHubToolGateway`: every read the
replay will need is made exactly once, its raw response is kept under the slug the replay
gateway resolves (`gateway_replay.fixture_slug_for`), and the job log goes to `logs/`, never
to `api/`. The model's optional `additional_tool_calls` are not recorded -- at replay time a
missing `api/` file is `not_found`, which the Investigator already treats as data.

**Scrubbed before it is written.** Every recorded text passes through the trace's
`Redactor` (`scripts/scrub_fixtures.py`'s rules, planted sentinels excepted), so a token a
careless workflow echoed into its log does not reach the working tree. Run
`scrub_fixtures.py --check` before committing anyway; it is the gate, this is a courtesy.

Requires `HARNESS_GITHUB_TOKEN` and the repository in `HARNESS_ALLOWED_REPOS` -- the same two
opt-ins `replay.py --live` requires, because this script makes the same live calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from pydantic import JsonValue

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.scrub_fixtures import build_redactor, scrub_text  # noqa: E402
from src.api.deps import FIXTURES_ROOT, AppContext, get_app_context  # noqa: E402
from src.harness.contracts import RunRequest  # noqa: E402
from src.harness.gateway import ToolCall, ToolGateway, ToolResult, ToolSpec  # noqa: E402
from src.harness.guardrails import PolicyDecision  # noqa: E402
from src.harness.observability import Redactor  # noqa: E402
from src.harness.orchestrator import RunState, new_run_id  # noqa: E402
from src.integrations.cicd.agents.investigator import Investigator, parse_subject  # noqa: E402
from src.integrations.cicd.catalog import READ_TOOLS  # noqa: E402
from src.integrations.cicd.gateway_replay import fixture_slug_for  # noqa: E402
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


class RecordingToolGateway:
    """A `ToolGateway` that delegates every call and keeps every read's answer.

    Reads are written to `api/GET_<slug>.json` (the body as the API returned it) or, for
    `get_job_logs`, to `logs/job_<id>.txt` (the raw text). Writes are delegated untouched
    -- the inner gateway's dry-run rule still applies -- and never recorded, per
    `fixtures/README.md`. A failed read is not recorded either: the replay would read
    the absence as the same `not_found` the live call answered.
    """

    integration = INTEGRATION

    def __init__(self, inner: ToolGateway, repo: str, out: Path, redactor: Redactor) -> None:
        self.inner = inner
        self.repo = repo
        self.out = out
        self.redactor = redactor
        self.recorded: list[Path] = []
        self.tail_capped_logs: list[int] = []

    def catalog(self) -> list[ToolSpec]:
        return self.inner.catalog()

    async def invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult:
        result = await self.inner.invoke(call, decision)
        if call.tool not in READ_TOOLS or not result.ok or result.data is None:
            return result
        args = dict(call.args)
        if call.tool == "get_job_logs":
            content = result.data.get("content")
            if isinstance(content, str):
                self._write(
                    self.out / "logs" / f"job_{int(args['job_id'])}.txt", content
                )
            if result.data.get("head_dropped"):
                # The live gateway keeps the LAST max_bytes; the recording is that tail,
                # and a replay of it reads `truncated: false` because the file is short
                # (Phase 5 audit finding 8). Recorded as a fact about the fixture.
                self.tail_capped_logs.append(int(args["job_id"]))
            return result
        slug = fixture_slug_for(self.repo, call.tool, args)
        if slug is not None:
            body: JsonValue = dict(result.data)
            self._write(
                self.out / "api" / f"GET_{slug}.json", json.dumps(body, indent=2) + "\n"
            )
        return result

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(scrub_text(text, self.redactor), encoding="utf-8", newline="\n")
        self.recorded.append(path)

    async def aclose(self) -> None:
        await self.inner.aclose()


def scenario_yaml(name: str, subject: dict[str, Any], collected: Any) -> str:
    """`scenario.yaml` with only the keys the recording determines; the label is a
    person's call and is left as a commented block (`fixtures/README.md`: only assert
    what the scenario genuinely determines).

    `commit` is part of the label: it is `Diagnosis.suspected_commit_sha`, which the eval
    scores, and a flaky or infra failure's honest value is `null` (Phase 5 audit finding
    8). The head sha is offered as a commented hint for the person filling it in.
    """
    parsed = parse_subject(subject)
    head = parsed["head_sha"]
    baseline_kind = collected.diff.baseline_kind
    cold_start = collected.cold_start
    return (
        f"name: {name}\n"
        "description: >\n"
        f"  Recorded by scripts/record_fixture.py from {parsed['repo']} workflow run\n"
        f"  {parsed['run_id']} (attempt {parsed['run_attempt']}), job {collected.job.job_name!r}.\n"
        "  REPLACE this with what a human triaging the failure would conclude.\n"
        "expected:\n"
        "  # --- filled in by a person; a missing key is not scored (fixtures/README.md) ---\n"
        "  # category: flaky_test | real_regression | dependency_break | infra_transient | config_issue | unknown\n"
        "  # min_confidence: 0.75\n"
        "  # cites_any_of:\n"
        '  #   - "..."\n'
        "  # action: retry | open_fix_pr | open_revert_pr | file_ticket | escalate\n"
        "  # effect: allow | require_approval | deny\n"
        f"  # commit: {head}   # the head sha -- keep ONLY if the diagnosis should blame it; null for flaky/infra\n"
        "  # --- determined by the recording ---\n"
        f"  baseline_kind: {baseline_kind}\n"
        f"  cold_start: {'true' if cold_start else 'false'}\n"
    )


async def record(
    repo: str, run_id: int, name: str, out_root: Path, *, force: bool, context: AppContext | None = None
) -> int:
    context = context or get_app_context()
    settings = context.settings
    if repo not in settings.allowed_repos:
        print(f"{repo!r} is not in HARNESS_ALLOWED_REPOS", file=sys.stderr)
        return 2
    out = out_root / name
    if out.exists() and any(out.iterdir()) and not force:
        print(f"{out} exists and is not empty; pass --force to overwrite", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    redactor = build_redactor()

    subject = await fetch_workflow_run(
        repo, run_id, settings.github_token.get_secret_value(), str(settings.github_api_base)
    )
    (out / "webhook.json").write_text(
        scrub_text(json.dumps(subject, indent=2) + "\n", redactor), encoding="utf-8", newline="\n"
    )

    gateway = RecordingToolGateway(context.build_live_gateway(repo), repo, out, redactor)
    investigator = Investigator(
        gateway=gateway,
        context_manager=context.context_manager,
        llm=context.llm,  # never called: `collect` stops before the model
        model=settings.model_investigator or settings.gemini_model,
        recorder=context.recorder,  # unbound: nothing is written to the trace
        memory=None,
    )
    request = RunRequest(
        integration=INTEGRATION,
        subject=subject,
        idempotency_key="cicd:record:" + name,
        mode="live",
        requested_by="scripts/record_fixture.py",
    )
    state = RunState(run_id=new_run_id(), request=request, artifacts={}, degraded=[], stages=[])
    try:
        collected = await investigator.collect(state)
    finally:
        await gateway.aclose()

    label = scenario_yaml(name, subject, collected)
    if gateway.tail_capped_logs:
        label += (
            "# NOTE: the recorded log(s) for job(s) "
            f"{gateway.tail_capped_logs} are the LAST {investigator.max_log_bytes} bytes the live\n"
            "# gateway kept, not the whole artifact; a replay reads them as untruncated.\n"
        )
    (out / "scenario.yaml").write_text(label, encoding="utf-8", newline="\n")
    print(f"recorded {name} -> {out}")
    for path in [out / "webhook.json", *gateway.recorded, out / "scenario.yaml"]:
        print(f"  {path.relative_to(out_root)}")
    print(
        f"  baseline {collected.diff.baseline_kind}, cold_start {collected.cold_start}, "
        f"{len(collected.diff.files)} file(s) in the diff, "
        f"{sum(e.total_lines for e in collected.logs)} log line(s)"
        + (f", gateway errors: {[e.kind for e in collected.gateway_errors]}" if collected.gateway_errors else "")
    )
    print("next: fill in the label in scenario.yaml, then `uv run python scripts/scrub_fixtures.py --check`")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--run-id", required=True, type=int, help="the failing workflow_run id")
    parser.add_argument("--name", required=True, help="the scenario directory name to create")
    parser.add_argument("--out", type=Path, default=FIXTURES_ROOT, help="fixtures root (default: fixtures/scenarios)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing scenario directory")
    args = parser.parse_args()
    if not args.name.replace("_", "").replace("-", "").isalnum():
        parser.error("--name must be a plain directory name (letters, digits, _ and -)")
    return asyncio.run(record(args.repo, args.run_id, args.name, args.out, force=args.force))


if __name__ == "__main__":
    raise SystemExit(main())
