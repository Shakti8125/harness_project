"""Appendix B.1: `MAX_TOKENS` -> retry with `max_output_tokens x 1.5` (Phase 4).

Phase 1 could not honour the multiplier: A.8's `call` takes only a prompt, so the loop
had no handle on the request budget and retried with "answer more briefly" instead. The
`OutputBudget` shared between the caller's closure and the loop is what closes that
(Phase 2 backlog; Phase 4 dispatch decision 11). Without one the loop behaves as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from src.harness.agent import LLMAgent
from src.harness.contracts import TokenUsage
from src.harness.llm import LlmRequest, RawLlmResponse
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.harness.orchestrator import RunState
from src.harness.recovery import (
    ATTR_MAX_OUTPUT_TOKENS,
    OUTPUT_BUDGET_CEILING,
    OUTPUT_BUDGET_GROWTH,
    OutputBudget,
    RetryPolicy,
    retry_structured,
)


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str


def response(text: str, finish_reason: str) -> RawLlmResponse:
    return RawLlmResponse(
        text=text, tokens=TokenUsage(prompt=10, completion=5, total=15),
        finish_reason=finish_reason, model="m", latency_ms=1,
    )


def test_output_budget_grows_by_the_multiplier_and_stops_at_the_ceiling() -> None:
    budget = OutputBudget(8192)
    assert OUTPUT_BUDGET_GROWTH == 1.5
    assert budget.grow() == 12288
    assert budget.grow() == 18432
    assert budget.max_output_tokens == 18432

    capped = OutputBudget(OUTPUT_BUDGET_CEILING - 1)
    assert capped.grow() == OUTPUT_BUDGET_CEILING
    assert capped.grow() == OUTPUT_BUDGET_CEILING
    # A tiny budget still moves by at least one, so growth is never a no-op.
    assert OutputBudget(1).grow() == 2


async def test_max_tokens_grows_the_budget_the_next_call_reads(tmp_db_path: Path) -> None:
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    await recorder.initialize()
    budget = OutputBudget(1000)
    seen: list[int] = []
    prompts: list[str] = []

    async def call(prompt: str) -> RawLlmResponse:
        seen.append(budget.max_output_tokens)
        prompts.append(prompt)
        if len(seen) < 3:
            return response('{"value": "cut', "MAX_TOKENS")
        return response('{"value": "ok"}', "STOP")

    with recorder.run_scope("run_01J8TESTB7DGET00000000000A"):
        output, attempts, error = await retry_structured(
            call, "p", _Out, RetryPolicy(), recorder, output_budget=budget
        )

    assert error is None and output == _Out(value="ok")
    assert seen == [1000, 1500, 2250]
    assert [a.outcome for a in attempts] == ["validation_error", "validation_error", "ok"]
    assert "raised to 1500 tokens" in prompts[1]
    assert "raised to 2250 tokens" in prompts[2]
    assert "more briefly" not in prompts[1]

    trace = await recorder.read_trace("run_01J8TESTB7DGET00000000000A")
    assert trace is not None
    budgets = [
        span.attributes[ATTR_MAX_OUTPUT_TOKENS]
        for span in trace.spans if span.name == "llm.attempt"
    ]
    assert budgets == [1000, 1500, 2250]


async def test_without_a_budget_the_phase_one_instruction_stands(tmp_db_path: Path) -> None:
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    await recorder.initialize()
    prompts: list[str] = []

    async def call(prompt: str) -> RawLlmResponse:
        prompts.append(prompt)
        if len(prompts) == 1:
            return response('{"value": "cut', "MAX_TOKENS")
        return response('{"value": "ok"}', "STOP")

    with recorder.run_scope("run_01J8TESTB7DGET00000000000B"):
        output, attempts, _ = await retry_structured(call, "p", _Out, RetryPolicy(), recorder)

    assert output == _Out(value="ok") and len(attempts) == 2
    assert "substantially more briefly" in prompts[1]
    trace = await recorder.read_trace("run_01J8TESTB7DGET00000000000B")
    assert trace is not None
    assert all(
        ATTR_MAX_OUTPUT_TOKENS not in span.attributes
        for span in trace.spans if span.name == "llm.attempt"
    )


async def test_llm_agent_shares_its_budget_with_the_loop(tmp_db_path: Path) -> None:
    """The request the client receives carries the grown budget: 4096 -> 6144."""
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    await recorder.initialize()
    requests: list[LlmRequest] = []

    class Client:
        async def generate(self, req: LlmRequest) -> RawLlmResponse:
            requests.append(req)
            if len(requests) == 1:
                return response('{"value": "cut', "MAX_TOKENS")
            return response('{"value": "ok"}', "STOP")

    class Agent(LLMAgent[_Out]):
        async def build_prompt(self, state: RunState) -> object:
            from src.harness.agent import AgentPrompt

            return AgentPrompt(text="hello")

    agent = Agent(
        key="a", output_model=_Out, llm=Client(), model="m", recorder=recorder,
        max_output_tokens=4096,
    )
    from src.harness.contracts import RunRequest

    state = RunState(
        run_id="run_01J8TESTB7DGET00000000000C",
        request=RunRequest(integration="cicd", subject={}, idempotency_key="cicd:budget"),
        artifacts={}, degraded=[], stages=[],
    )
    with recorder.run_scope(state.run_id):
        result = await agent.run(state)

    assert result.status == "ok" and result.attempts == 2
    assert [r.max_output_tokens for r in requests] == [4096, 6144]
    # Each agent call starts from the agent's own figure again.
    requests.clear()
    with recorder.run_scope(state.run_id):
        await agent.run(state)
    assert requests[0].max_output_tokens == 4096


@pytest.mark.parametrize("finish", ["STOP", "UNKNOWN"])
async def test_a_validation_error_that_is_not_max_tokens_leaves_the_budget_alone(
    tmp_db_path: Path, finish: str
) -> None:
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    await recorder.initialize()
    budget = OutputBudget(1000)
    calls = 0

    async def call(prompt: str) -> RawLlmResponse:
        nonlocal calls
        calls += 1
        return response("not json" if calls == 1 else '{"value": "ok"}', finish)

    with recorder.run_scope("run_01J8TESTB7DGET00000000000D"):
        output, _, _ = await retry_structured(
            call, "p", _Out, RetryPolicy(), recorder, output_budget=budget
        )
    assert output == _Out(value="ok")
    assert budget.max_output_tokens == 1000
