"""Wave-3 audit finding 3, at the real HTTP boundary: `POST /v1/replay/{scenario}`.

`test_serialize_run_outcome.py` pins the transformation as a pure function; this file
drives the actual FastAPI route (the real orchestrator, the real replay gateway, the real
`ContextManager` over the real fixture log) with a stubbed LLM -- same shape as
`test_replay_e2e.py`, so zero live model calls and zero quota spent -- and asserts on
`response.json()` / `response.text` exactly as a caller would see them.

Includes the regression the reviewer's report named explicitly but could not yet test
(no fixture carries a credential today): a `ghp_`-shaped token planted into a *copy* of
the log fixture (never the real one under `fixtures/`, which stays untouched — this test
writes only under `tmp_path`) must not appear anywhere in the served response text, on
either the log-excerpt path or the diff-patch path.

`review-2.md` finding 2, reproduced as a durable test below: the two named fields
(`logs[].excerpt`, `diff.files[].patch`) are not the only surface a planted credential can
ride out on. `prompts/diagnostician.md:16` instructs the model to quote evidence
*verbatim*, so a realistic model response copies the planted line into
`final.diagnosis.citations[].quote` and, symmetrically, into
`final.bundle.notes.observations[]` on the Investigator side. `test_replay_e2e.StubLlm`
cannot exercise this because its citation text is fixed and never touches the injected
line -- `QuotingStubLlm` below does what the prompt actually asks the model to do: it
pulls the exact line carrying the token out of the prompt text it was handed and quotes
it back, the same way a compliant model would.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.deps import SECRET_PATTERNS, AppContext, build_secret_registry
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import RunOutcome, TokenUsage
from src.harness.llm import LlmClient, LlmRequest, RawLlmResponse
from src.harness.observability import Redactor, TraceRecorder
from src.settings import get_settings
from tests.integration.test_replay_e2e import StubLlm
from tests.stubs import remediation_plan

SECRET_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # 36 chars after ghp_


def _line_containing(text: str, needle: str) -> str:
    """The one line of `text` that carries `needle`, stripped -- what a model asked to
    quote verbatim would copy out of the evidence it was shown."""
    for line in text.splitlines():
        if needle in line:
            return line.strip()
    raise AssertionError(f"{needle!r} not found in the prompt text handed to the stub")


class QuotingStubLlm:
    """A model that actually does what `prompts/diagnostician.md:16` asks: quotes the
    evidence it was given verbatim rather than returning a fixed, pre-baked citation.

    Dispatches on the prompt preamble like `test_replay_e2e.StubLlm`; the only
    difference is that both the Investigator's `observations` and the Diagnostician's
    `citations[].quote` are built by finding the planted token inside the prompt text
    itself and copying the whole line, exactly as a compliant model would when told to
    quote verbatim rather than paraphrase.
    """

    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        if "You are the Investigator" in req.prompt:
            quoted_line = _line_containing(req.prompt, SECRET_TOKEN)
            payload: object = {
                "observations": [f"the log contains: {quoted_line}"],
                "additional_tool_calls": [],
                "narrative": (
                    "A pricing test regresses on a diff that touches the discount "
                    "helper; the job log also contains an unrelated pasted line."
                ),
            }
        elif "You are the Remediator" in req.prompt:
            payload = remediation_plan()
        elif "You are the Diagnostician" in req.prompt:
            quoted_line = _line_containing(req.prompt, SECRET_TOKEN)
            payload = {
                "reasoning": (
                    "The log shows assert 91 == 90 from discount(100, 10), and the diff "
                    "contains exactly one changed file, src/pricing/discount.py."
                ),
                "category": "real_regression",
                "summary": "An off-by-one in discount() returns 91 instead of 90.",
                "self_confidence": 0.9,
                "citations": [
                    {
                        "claim_kind": "quote_exists",
                        "locator": "log:job/601234567",
                        "quote": quoted_line,
                        "note": "verbatim quote of the evidence, per instructions",
                    },
                ],
                "suspected_commit_sha": None,
                "suspected_test_ids": [],
                "suspected_package": None,
                "suggested_action": "open_fix_pr",
            }
        else:  # pragma: no cover - a new agent would have to opt in here
            raise AssertionError("unrecognised prompt reached the stub model")
        return RawLlmResponse(
            text=json.dumps(payload),
            tokens=TokenUsage(prompt=1200, completion=300, total=1500),
            finish_reason="STOP",
            model=req.model,
            latency_ms=12,
        )


@pytest.fixture
def client(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    settings = get_settings()
    recorder = TraceRecorder(
        db_path=tmp_db_path,
        redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
    )
    context = AppContext(
        settings=settings,
        recorder=recorder,
        context_manager=ContextManager(
            default_budget=ContextBudget(total_chars=settings.log_char_budget)
        ),
        llm=StubLlm(),
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    return TestClient(api_main.app)


def test_excerpt_and_patch_are_absent_from_the_real_response(client: TestClient) -> None:
    response = client.post("/v1/replay/real_regression")
    assert response.status_code == 200
    body = response.json()

    log_entry = body["final"]["bundle"]["logs"][0]
    assert "excerpt" not in log_entry
    assert isinstance(log_entry["excerpt_length"], int) and log_entry["excerpt_length"] > 0
    assert len(log_entry["excerpt_sha256"]) == 64

    for file_entry in body["final"]["bundle"]["diff"]["files"]:
        assert "patch" not in file_entry
        assert isinstance(file_entry["path"], str)
        assert isinstance(file_entry["status"], str)
        assert isinstance(file_entry["additions"], int)
        assert isinstance(file_entry["deletions"], int)

    # The harness's actual designed evidence surface must survive byte-for-byte.
    citations = body["final"]["diagnosis"]["citations"]
    assert citations
    assert all(citation["quote"] for citation in citations)

    # Unaffected by this change, but shares the response body worth re-checking here.
    assert body["status"] == "awaiting_approval"
    assert body["final"]["diagnosis"]["category"] == "real_regression"
    assert body["final"]["diagnosis"]["suggested_action"] == "open_fix_pr"
    assert body["run_id"]
    assert body["trace_url"]
    assert body["stages"]


def test_response_is_materially_smaller_than_an_unscrubbed_dump(client: TestClient) -> None:
    response = client.post("/v1/replay/real_regression")
    assert len(response.content) < 20_000


@pytest.fixture
def scenario_with_a_pasted_token(tmp_path: Path, repo_root: Path) -> Path:
    """A private copy of `real_regression`, never the real fixture, with a `ghp_`-shaped
    token planted right beside the anchored assertion line so it survives the context
    budget's trim (PLAN.md:862-866's Phase-5 fixture, brought forward)."""
    source = repo_root / "fixtures" / "scenarios" / "real_regression"
    poisoned = tmp_path / "real_regression_with_a_secret"
    shutil.copytree(source, poisoned)

    log_path = poisoned / "logs" / "job_601234567.txt"
    text = log_path.read_text(encoding="utf-8")
    marker = "E       assert 91 == 90"
    assert marker in text, "fixture layout changed; the injection anchor moved"
    injected = text.replace(
        marker,
        f"{marker}\nleaked credential in CI output: {SECRET_TOKEN}",
        1,
    )
    log_path.write_text(injected, encoding="utf-8")
    return poisoned


async def _run_orchestrator(
    scenario_dir: Path, llm: LlmClient, idempotency_key: str
) -> RunOutcome:
    from src.harness.contracts import RunRequest
    from src.harness.guardrails import PolicyEngine
    from src.integrations.cicd.gateway_replay import ReplayToolGateway
    from src.integrations.cicd.wiring import build_orchestrator, load_policy_spec

    recorder = TraceRecorder(
        db_path=Path("unused.db"),
        redactor=Redactor(build_secret_registry(get_settings()), SECRET_PATTERNS),
    )
    gateway = ReplayToolGateway(
        scenario_dir=scenario_dir, repo="octo-org/harness-demo-repo", forbidden=()
    )
    orchestrator = build_orchestrator(
        gateway=gateway,
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=120_000)),
        llm=llm,
        recorder=recorder,
        escalation_threshold=0.70,
        investigator_model="stub-model",
        diagnostician_model="stub-model",
        remediator_model="stub-model",
        engine=PolicyEngine(load_policy_spec()),
    )
    webhook = json.loads((scenario_dir / "webhook.json").read_text(encoding="utf-8"))
    request = RunRequest(
        integration="cicd",
        subject=webhook,
        idempotency_key=idempotency_key,
        mode="replay",
        replay_fixture="real_regression",
        requested_by="test",
    )
    return await orchestrator.run(request)


async def _run_against(
    scenario_dir: Path, llm: LlmClient | None = None
) -> dict[str, object]:
    from src.api.main import _serialize_run_outcome

    outcome = await _run_orchestrator(
        scenario_dir, llm or StubLlm(), "cicd:test-secret-leak-regression"
    )
    return _serialize_run_outcome(outcome)


async def test_a_pasted_token_in_the_log_never_reaches_the_served_response(
    scenario_with_a_pasted_token: Path,
) -> None:
    body = await _run_against(scenario_with_a_pasted_token)
    served_text = json.dumps(body)

    # Confirm the premise: the token really was in what the Investigator read, so a
    # missing assertion below is the scrub working, not the token never having been
    # collected in the first place.
    assert SECRET_TOKEN not in served_text

    log_entry = body["final"]["bundle"]["logs"][0]
    assert "excerpt" not in log_entry
    assert "excerpt_length" in log_entry and "excerpt_sha256" in log_entry


async def test_the_premise_the_token_was_actually_in_scope_pre_scrub(
    scenario_with_a_pasted_token: Path,
) -> None:
    """Without this, the test above could pass vacuously (token never collected, budgeted
    out, or otherwise absent from the pipeline's evidence for reasons unrelated to the
    scrub). Confirms the raw, pre-serialisation `RunOutcome` really does carry it."""
    from src.harness.contracts import RunRequest
    from src.harness.guardrails import PolicyEngine
    from src.integrations.cicd.gateway_replay import ReplayToolGateway
    from src.integrations.cicd.wiring import build_orchestrator, load_policy_spec

    settings = get_settings()
    recorder = TraceRecorder(
        db_path=Path("unused.db"),
        redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
    )
    gateway = ReplayToolGateway(
        scenario_dir=scenario_with_a_pasted_token,
        repo="octo-org/harness-demo-repo",
        forbidden=(),
    )
    orchestrator = build_orchestrator(
        gateway=gateway,
        context_manager=ContextManager(default_budget=ContextBudget(total_chars=120_000)),
        llm=StubLlm(),
        recorder=recorder,
        escalation_threshold=0.70,
        investigator_model="stub-model",
        diagnostician_model="stub-model",
        remediator_model="stub-model",
        engine=PolicyEngine(load_policy_spec()),
    )
    webhook = json.loads(
        (scenario_with_a_pasted_token / "webhook.json").read_text(encoding="utf-8")
    )
    request = RunRequest(
        integration="cicd",
        subject=webhook,
        idempotency_key="cicd:test-secret-leak-premise",
        mode="replay",
        replay_fixture="real_regression",
        requested_by="test",
    )
    outcome = await orchestrator.run(request)
    unscrubbed = json.dumps(outcome.model_dump(mode="json"))

    assert SECRET_TOKEN in unscrubbed, (
        "the injected token never reached the raw RunOutcome -- the scrub test above "
        "would be vacuous"
    )


async def test_the_verbatim_quote_premise_the_token_reaches_citations_and_observations(
    scenario_with_a_pasted_token: Path,
) -> None:
    """Without this, the leak test below could pass vacuously. Confirms the raw,
    pre-serialisation `RunOutcome` really does carry the token specifically through
    `diagnosis.citations[].quote` and `bundle.notes.observations[]` -- the two fields
    the digest substitution in `_serialize_run_outcome` never touches -- and not merely
    through `logs[].excerpt`, which a different mechanism already handles.
    """
    outcome = await _run_orchestrator(
        scenario_with_a_pasted_token, QuotingStubLlm(), "cicd:test-verbatim-quote-premise"
    )
    dumped = outcome.model_dump(mode="json")
    assert outcome.final is not None

    citations = dumped["final"]["diagnosis"]["citations"]
    assert citations, "the stub must have produced at least one citation"
    assert any(SECRET_TOKEN in citation["quote"] for citation in citations), (
        "the premise: the Diagnostician's citation must actually carry the planted "
        "token, or the leak test below would be vacuous"
    )

    observations = dumped["final"]["bundle"]["notes"]["observations"]
    assert observations, "the stub must have produced at least one observation"
    assert any(SECRET_TOKEN in observation for observation in observations), (
        "the premise: the Investigator's observation must actually carry the planted "
        "token, or the leak test below would be vacuous"
    )


async def test_a_verbatim_quoted_citation_never_reaches_the_served_response(
    scenario_with_a_pasted_token: Path,
) -> None:
    """The regression `review-2.md` finding 2 reproduced offline: a citation that quotes
    the planted line verbatim, exactly as `prompts/diagnostician.md:16` instructs, must
    still be scrubbed of the token at the HTTP boundary. The two digest substitutions in
    `_serialize_run_outcome` do not touch `citations[].quote` at all -- only the
    full-body `Redactor.scrub` pass can close this, which is what this test pins.
    """
    body = await _run_against(scenario_with_a_pasted_token, llm=QuotingStubLlm())
    served_text = json.dumps(body)

    assert SECRET_TOKEN not in served_text

    citations = body["final"]["diagnosis"]["citations"]
    assert citations
    assert SECRET_TOKEN not in json.dumps(citations)

    observations = body["final"]["bundle"]["notes"]["observations"]
    assert observations
    assert SECRET_TOKEN not in json.dumps(observations)
