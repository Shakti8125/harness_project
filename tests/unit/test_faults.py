"""`HARNESS_FAULT_INJECT`: one parser, one guard, and the LLM faults that never reach Gemini.

The property that matters most is the last one: a fault-injected run must not spend
quota, so the wrapped client's call count is asserted alongside every attempt count.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from src.api.deps import KNOWN_FAULTS, AppContext, build_fault
from src.harness.contracts import TokenUsage
from src.harness.errors import ConfigurationError
from src.harness.faults import (
    BAD_JSON_TEXT,
    FAULT_LLM_429,
    FAULT_LLM_BAD_JSON,
    LLM_FAULTS,
    Fault,
    FaultInjectingLlmClient,
    parse_fault,
)
from src.harness.llm import LlmRateLimited, LlmRequest, RawLlmResponse
from src.harness.memory import FAULT_SQLITE_LOCKED
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.harness.recovery import RetryPolicy, retry_structured
from src.integrations.cicd.wiring import FAULT_FABRICATE_CITATION
from src.settings import get_settings

# ---------------------------------------------------------------------------
# parse_fault / build_fault
# ---------------------------------------------------------------------------


def test_parse_fault_shapes() -> None:
    assert parse_fault("llm_bad_json:2") == Fault("llm_bad_json", 2)
    assert parse_fault(" llm_429:3 ") == Fault("llm_429", 3)
    assert parse_fault("sqlite_locked") == Fault("sqlite_locked")
    citation_fault = "diagnostician_fabricate_citation"
    assert parse_fault(citation_fault) == Fault(citation_fault)
    assert Fault("llm_429", 3).spec == "llm_429:3"
    assert Fault("sqlite_locked").spec == "sqlite_locked"


@pytest.mark.parametrize(
    "spec",
    ["llm_bad_json", "llm_bad_json:", "llm_bad_json:0", "llm_bad_json:-1", "llm_429:two",
     "sqlite_locked:2", ""],
)
def test_parse_fault_rejects_malformed_specs(spec: str) -> None:
    with pytest.raises(ConfigurationError):
        parse_fault(spec)


def test_known_faults_is_the_union_of_every_component() -> None:
    expected = {FAULT_SQLITE_LOCKED, FAULT_LLM_BAD_JSON, FAULT_LLM_429, FAULT_FABRICATE_CITATION}
    assert expected == KNOWN_FAULTS
    assert {FAULT_LLM_BAD_JSON, FAULT_LLM_429} == LLM_FAULTS


def test_build_fault_guards() -> None:
    settings = get_settings()
    assert build_fault(settings) is None
    assert build_fault(settings.model_copy(update={"fault_inject": ""})) is None
    assert build_fault(settings.model_copy(update={"fault_inject": "   "})) is None
    counted = settings.model_copy(update={"fault_inject": "llm_bad_json:2"})
    assert build_fault(counted) == Fault("llm_bad_json", 2)
    with pytest.raises(ConfigurationError, match="development-only"):
        build_fault(settings.model_copy(update={"fault_inject": "llm_bad_json:2", "env": "prod"}))
    with pytest.raises(ConfigurationError, match="unknown fault injection 'nope'"):
        build_fault(settings.model_copy(update={"fault_inject": "nope"}))


def test_app_context_refuses_a_fault_outside_dev(tmp_db_path: Path) -> None:
    """The guard moved up from `build_memory_store` (handoff §4) and still bites at
    construction of the context, for every fault name."""
    import asyncio

    from src.harness.context_manager import ContextBudget, ContextManager

    settings = get_settings().model_copy(
        update={"fault_inject": "llm_429:1", "env": "prod", "database_path": tmp_db_path}
    )
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    with pytest.raises(ConfigurationError, match="development-only"):
        AppContext(
            settings=settings, recorder=recorder,
            context_manager=ContextManager(default_budget=ContextBudget(total_chars=1000)),
            llm=_Inner(), run_semaphore=asyncio.Semaphore(1),
        )


# ---------------------------------------------------------------------------
# FaultInjectingLlmClient
# ---------------------------------------------------------------------------


class _Inner:
    def __init__(self) -> None:
        self.calls: list[LlmRequest] = []

    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        self.calls.append(req)
        return RawLlmResponse(
            text='{"x": 1}', tokens=TokenUsage(prompt=10, completion=5, total=15),
            finish_reason="STOP", model=req.model, latency_ms=1,
        )


class Out(BaseModel):
    x: int


def request(schema_name: str = "a") -> LlmRequest:
    return LlmRequest(model="m", prompt="p", schema={"type": "OBJECT", "title": schema_name})


def test_client_refuses_a_non_llm_fault() -> None:
    with pytest.raises(ConfigurationError):
        FaultInjectingLlmClient(_Inner(), Fault("sqlite_locked"))
    with pytest.raises(ConfigurationError):
        FaultInjectingLlmClient(_Inner(), Fault("llm_bad_json", None))


async def test_bad_json_answers_the_first_n_per_agent_without_calling_inner() -> None:
    inner = _Inner()
    client = FaultInjectingLlmClient(inner, Fault(FAULT_LLM_BAD_JSON, 2))

    first = await client.generate(request("a"))
    second = await client.generate(request("a"))
    assert first.text == BAD_JSON_TEXT and second.text == BAD_JSON_TEXT
    assert first.finish_reason == "STOP" and first.tokens == TokenUsage()
    assert inner.calls == [], "the faulted attempts never reach the provider"

    third = await client.generate(request("a"))
    assert third.text == '{"x": 1}'
    assert len(inner.calls) == 1

    # A different agent (schema) starts its own count.
    other = await client.generate(request("b"))
    assert other.text == BAD_JSON_TEXT
    assert len(inner.calls) == 1


async def test_429_raises_rate_limited_with_no_stated_delay() -> None:
    inner = _Inner()
    client = FaultInjectingLlmClient(inner, Fault(FAULT_LLM_429, 1))
    with pytest.raises(LlmRateLimited) as excinfo:
        await client.generate(request())
    assert excinfo.value.retry_after_s is None
    assert inner.calls == []
    assert (await client.generate(request())).text == '{"x": 1}'


async def test_bad_json_through_the_retry_loop_recovers_at_n_plus_one(tmp_db_path: Path) -> None:
    """PLAN.md step 3 in miniature: `llm_bad_json:2` -> three attempts, the third real."""
    inner = _Inner()
    client = FaultInjectingLlmClient(inner, Fault(FAULT_LLM_BAD_JSON, 2))
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    await recorder.initialize()
    req = request()

    async def call(prompt: str) -> RawLlmResponse:
        return await client.generate(req.model_copy(update={"prompt": prompt}))

    with recorder.run_scope("run_01J8TESTFA1TS000000000000A"):
        output, attempts, error = await retry_structured(call, "p", Out, RetryPolicy(), recorder)

    assert error is None and output == Out(x=1)
    assert [a.outcome for a in attempts] == ["validation_error", "validation_error", "ok"]
    assert len(attempts) == 3
    assert len(inner.calls) == 1


async def test_bad_json_beyond_the_attempt_budget_never_recovers(tmp_db_path: Path) -> None:
    inner = _Inner()
    client = FaultInjectingLlmClient(inner, Fault(FAULT_LLM_BAD_JSON, 9))
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    await recorder.initialize()
    req = request()

    async def call(prompt: str) -> RawLlmResponse:
        return await client.generate(req.model_copy(update={"prompt": prompt}))

    with recorder.run_scope("run_01J8TESTFA1TS000000000000B"):
        output, attempts, error = await retry_structured(call, "p", Out, RetryPolicy(), recorder)

    assert output is None and error is not None
    assert error.kind == "invalid_output" and len(attempts) == 3
    assert inner.calls == [], "nine junk answers cost zero provider calls"


async def test_429_through_the_retry_loop_recovers_on_the_fourth(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.harness.recovery as recovery

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(recovery.asyncio, "sleep", no_sleep)
    inner = _Inner()
    client = FaultInjectingLlmClient(inner, Fault(FAULT_LLM_429, 3))
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    await recorder.initialize()
    req = request()

    async def call(prompt: str) -> RawLlmResponse:
        return await client.generate(req.model_copy(update={"prompt": prompt}))

    with recorder.run_scope("run_01J8TESTFA1TS000000000000C"):
        output, attempts, error = await retry_structured(call, "p", Out, RetryPolicy(), recorder)

    assert error is None and output == Out(x=1)
    assert [a.outcome for a in attempts] == ["transient", "transient", "transient", "ok"]
    assert len(inner.calls) == 1
