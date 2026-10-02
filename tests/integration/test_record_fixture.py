# ruff: noqa: E501
"""`scripts/record_fixture.py` and `scripts/scrub_fixtures.py` (PLAN.md Phase 5), offline.

The recorder is driven against a mocked GitHub that serves `real_regression`'s own files --
with one token pasted into the job log -- into a temp fixtures root, and the recording is
then replayed through `ReplayToolGateway` + `Investigator.collect` and compared with the
committed scenario. What the recording writes is what the replay reads, or the format spec
is wrong.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx
import yaml

from src.api.deps import FIXTURES_ROOT, SECRET_PATTERNS, AppContext, build_secret_registry
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import RunRequest
from src.harness.observability import REDACTION_PLACEHOLDER, Redactor, TraceRecorder
from src.harness.orchestrator import RunState, new_run_id
from src.integrations.cicd.agents.investigator import Investigator, parse_subject
from src.integrations.cicd.wiring import INTEGRATION
from src.settings import get_settings
from tests.stubs import ScenarioStubLlm

REPO = "octo-org/harness-demo-repo"
API = f"https://api.github.com/repos/{REPO}"
SCENARIO = FIXTURES_ROOT / "real_regression"
RUN_ID = 501234567
JOB_ID = 601234567
BASE = "8d4d0a89231f66f3b3910ad16e033041c898512d"
HEAD = "e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df"
ECHOED_TOKEN = "ghp_RECORDEDLEAK0000000000000000000000000000"[:40]


def load_script(name: str) -> ModuleType:
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(name, repo_root / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(name: str) -> Any:
    return json.loads((SCENARIO / "api" / name).read_text(encoding="utf-8"))


def minimal_repository(repository: dict[str, Any]) -> dict[str, Any]:
    """The run API's `repository`: GitHub's minimal repository, with no `default_branch`."""
    return {k: v for k, v in repository.items() if k in ("id", "node_id", "name", "full_name", "private", "owner", "html_url", "url")}


def mock_github(router: respx.MockRouter) -> None:
    webhook = json.loads((SCENARIO / "webhook.json").read_text(encoding="utf-8"))
    router.get(f"{API}/actions/runs/{RUN_ID}").mock(
        return_value=httpx.Response(200, json={**webhook["workflow_run"], "repository": minimal_repository(webhook["repository"])})
    )
    router.get(API).mock(return_value=httpx.Response(200, json=webhook["repository"]))
    router.get(f"{API}/actions/runs/{RUN_ID}/attempts/1/jobs").mock(
        return_value=httpx.Response(200, json=_fixture(f"GET_repos-octo-org-harness-demo-repo-actions-runs-{RUN_ID}-attempts-1-jobs.json"))
    )
    log = (SCENARIO / "logs" / f"job_{JOB_ID}.txt").read_text(encoding="utf-8")
    log = log.replace("Getting Git version info", f"Getting Git version info (token {ECHOED_TOKEN})", 1)
    router.get(f"{API}/actions/jobs/{JOB_ID}/logs").mock(
        return_value=httpx.Response(200, content=log.encode(), headers={"content-type": "text/plain"})
    )
    router.get(f"{API}/actions/workflows/9001/runs").mock(
        return_value=httpx.Response(200, json=_fixture("GET_repos-octo-org-harness-demo-repo-actions-workflows-9001-runs-branch-main.json"))
    )
    router.get(f"{API}/compare/{BASE}...{HEAD}").mock(
        return_value=httpx.Response(200, json=_fixture(f"GET_repos-octo-org-harness-demo-repo-compare-{BASE}-{HEAD}.json"))
    )


@pytest.fixture
def context(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppContext:
    monkeypatch.setenv("HARNESS_ALLOWED_REPOS", json.dumps([REPO]))
    monkeypatch.setenv("HARNESS_GITHUB_TOKEN", "ghp_" + "t" * 36)
    get_settings.cache_clear()
    settings = get_settings()
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS))
    return AppContext(
        settings=settings,
        recorder=recorder,
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=settings.log_char_budget)),
        llm=ScenarioStubLlm(),
        run_semaphore=asyncio.Semaphore(2),
    )


async def _collect(context: AppContext, scenario_dir: Path) -> Any:
    subject = json.loads((scenario_dir / "webhook.json").read_text(encoding="utf-8"))
    gateway = context.build_replay_gateway(scenario_dir, REPO)
    investigator = Investigator(
        gateway=gateway, context_manager=context.context_manager, llm=context.llm,
        model="stub", recorder=context.recorder, memory=None,
    )
    request = RunRequest(integration=INTEGRATION, subject=subject, idempotency_key="cicd:collect-test", mode="replay")
    return await investigator.collect(RunState(run_id=new_run_id(), request=request, artifacts={}, degraded=[], stages=[]))


@respx.mock
async def test_recording_replays_like_the_committed_scenario(
    context: AppContext, tmp_path: Path, respx_mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_github(respx_mock)
    record_fixture = load_script("record_fixture")
    out_root = tmp_path / "scenarios"

    code = await record_fixture.record(REPO, RUN_ID, "recorded_regression", out_root, force=False, context=context)
    assert code == 0, capsys.readouterr()
    recorded = out_root / "recorded_regression"

    # The layout `fixtures/README.md` specifies, file for file.
    assert (recorded / "webhook.json").is_file()
    assert (recorded / "scenario.yaml").is_file()
    assert sorted(p.name for p in (recorded / "api").iterdir()) == sorted(p.name for p in (SCENARIO / "api").iterdir())
    assert (recorded / "logs" / f"job_{JOB_ID}.txt").is_file()
    assert not (recorded / "api" / f"GET_repos-octo-org-harness-demo-repo-actions-jobs-{JOB_ID}-logs.json").exists()

    # Scrubbed on the way in: the token GitHub's log echoed is not in the working tree.
    log_text = (recorded / "logs" / f"job_{JOB_ID}.txt").read_text(encoding="utf-8")
    assert ECHOED_TOKEN not in log_text
    assert f"(token {REDACTION_PLACEHOLDER})" in log_text

    # The label file carries only what the recording determines.
    label = yaml.safe_load((recorded / "scenario.yaml").read_text(encoding="utf-8"))
    assert label["name"] == "recorded_regression"
    # Phase 5 audit finding 8: `commit` is `Diagnosis.suspected_commit_sha`, a label the
    # eval scores -- a recorded flaky or infra scenario with the head sha filled in would
    # fail the gate on `commit: None != <head>`. It is offered as a commented hint, only.
    assert label["expected"] == {"baseline_kind": "branch_green", "cold_start": False}
    yaml_text = (recorded / "scenario.yaml").read_text(encoding="utf-8")
    assert "# category:" in yaml_text
    assert f"# commit: {HEAD}" in yaml_text

    # And it replays: the same job, the same diff, the same anchors as the committed one.
    ours = await _collect(context, recorded)
    theirs = await _collect(context, SCENARIO)
    assert ours.job == theirs.job
    assert ours.diff == theirs.diff
    assert ours.cold_start == theirs.cold_start is False
    assert [e.anchor_line_numbers for e in ours.logs] == [e.anchor_line_numbers for e in theirs.logs]
    assert ours.gateway_errors == []

    printed = capsys.readouterr().out
    assert "recorded recorded_regression" in printed and "baseline branch_green" in printed


@pytest.mark.parametrize("script", ["record_fixture", "replay"])
@respx.mock
async def test_a_live_subject_carries_the_default_branch_the_run_api_omits(
    script: str, respx_mock: respx.MockRouter
) -> None:
    """Session B, Stage 4a: `replay.py --live` on a fresh `demo/regression` run escalated
    `unknown_category`, because the run API's minimal `repository` has no `default_branch`
    and Appendix D's `default_green` step never ran."""
    mock_github(respx_mock)
    fetch = load_script(script).fetch_workflow_run
    subject = await fetch(REPO, RUN_ID, "ghp_" + "t" * 36, "https://api.github.com")
    assert subject["repository"]["default_branch"] == "main"
    assert parse_subject(subject)["default_branch"] == "main"
    assert parse_subject(subject)["repo"] == REPO


async def test_recording_refuses_a_repository_not_allowlisted(context: AppContext, tmp_path: Path) -> None:
    record_fixture = load_script("record_fixture")
    code = await record_fixture.record("someone-else/repo", RUN_ID, "x", tmp_path, force=False, context=context)
    assert code == 2
    assert not (tmp_path / "x").exists()


async def test_recording_refuses_to_overwrite_without_force(context: AppContext, tmp_path: Path) -> None:
    record_fixture = load_script("record_fixture")
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "webhook.json").write_text("{}", encoding="utf-8")
    code = await record_fixture.record(REPO, RUN_ID, "taken", tmp_path, force=False, context=context)
    assert code == 2
    assert (tmp_path / "taken" / "webhook.json").read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------------------------------
# scrub_fixtures.py
# ---------------------------------------------------------------------------


def test_scrub_rewrites_credentials_and_keeps_the_planted_sentinel(tmp_path: Path) -> None:
    scrub = load_script("scrub_fixtures")
    planted = scrub.PLANTED_SENTINELS[0]
    key_block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nAAAA\n-----END RSA PRIVATE KEY-----"
    text = (
        f"planted {planted} stays\n"
        f"echoed ghp_{'a' * 36} goes\n"
        f"{key_block}\n"
        "api_key=supersecretvalue1234 goes\n"
        "token: *** (already masked, too short to match)\n"
    )
    out = scrub.scrub_text(text, scrub.build_redactor())
    assert f"planted {planted} stays" in out
    assert "ghp_" + "a" * 36 not in out and f"echoed {REDACTION_PLACEHOLDER} goes" in out
    assert "PRIVATE KEY" not in out and "MIIEow" not in out
    assert "supersecretvalue1234" not in out
    assert "token: *** (already masked" in out


def test_scrub_check_reports_a_dirty_tree_and_passes_the_committed_one(tmp_path: Path) -> None:
    scrub = load_script("scrub_fixtures")
    redactor = scrub.build_redactor()
    dirty = tmp_path / "scenarios" / "leaky"
    dirty.mkdir(parents=True)
    (dirty / "webhook.json").write_text('{"token": "ghp_' + "b" * 36 + '"}', encoding="utf-8")
    (dirty / "notes.bin").write_bytes(b"ghp_" + b"c" * 36)  # not a text suffix: untouched

    changed = scrub.scrub_tree(tmp_path / "scenarios", redactor, check=True)
    assert changed == [dirty / "webhook.json"]
    assert "ghp_" + "b" * 36 in (dirty / "webhook.json").read_text(encoding="utf-8"), "--check rewrites nothing"

    changed = scrub.scrub_tree(tmp_path / "scenarios", redactor, check=False)
    assert changed == [dirty / "webhook.json"]
    assert (dirty / "webhook.json").read_text(encoding="utf-8") == f'{{"token": "{REDACTION_PLACEHOLDER}"}}'
    assert scrub.scrub_tree(tmp_path / "scenarios", redactor, check=True) == []

    # The committed fixtures are clean -- the pre-commit gate `fixtures/README.md` names.
    assert scrub.scrub_tree(FIXTURES_ROOT, redactor, check=True) == []
