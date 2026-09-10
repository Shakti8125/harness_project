"""review.md finding 9, at the point the two sentinels are actually written.

`test_recovery_terminal_finish_reasons.py` pins what `retry_structured` does with each
sentinel; this file pins that `GeminiClient.generate` writes the *right* one for each of
the two distinct wire shapes Appendix B.1's safety row conflates at first glance:

- no candidate at all (`response.candidates` empty or absent) -> `NO_CANDIDATE_FINISH_REASON`
- a candidate that came back but left `finish_reason` unset -> `UNSTATED_FINISH_REASON`

No network, no provider quota: the SDK's own `aio.models.generate_content` is monkeypatched
to return a fabricated response object built from plain attribute holders, never a real
`google.genai.Client` call.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.harness.llm import (
    NO_CANDIDATE_FINISH_REASON,
    UNSTATED_FINISH_REASON,
    GeminiClient,
    LlmRequest,
)


def _client() -> GeminiClient:
    # `GeminiClient.__init__` only constructs `genai.Client(api_key=...)`, which does not
    # itself make a network call -- the call happens inside `generate_content`, which every
    # test below replaces before it can be reached.
    return GeminiClient(api_key="test-key-not-a-real-credential")


async def test_no_candidates_at_all_writes_the_no_candidate_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()

    async def fake_generate_content(**_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(text="", usage_metadata=None, candidates=[])

    monkeypatch.setattr(
        client._client.aio.models, "generate_content", fake_generate_content
    )

    response = await client.generate(
        LlmRequest(model="stub-model", prompt="hello", schema=None)
    )

    assert response.finish_reason == NO_CANDIDATE_FINISH_REASON
    assert response.finish_reason == "NO_CANDIDATES"


async def test_a_candidate_that_states_no_reason_writes_the_unstated_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct from the empty-candidates case: there IS a candidate here, so this must
    not collapse into the terminal sentinel the other test pins.
    """
    client = _client()

    async def fake_generate_content(**_kwargs: Any) -> SimpleNamespace:
        candidate = SimpleNamespace(finish_reason=None)
        return SimpleNamespace(text="some text", usage_metadata=None, candidates=[candidate])

    monkeypatch.setattr(
        client._client.aio.models, "generate_content", fake_generate_content
    )

    response = await client.generate(
        LlmRequest(model="stub-model", prompt="hello", schema=None)
    )

    assert response.finish_reason == UNSTATED_FINISH_REASON
    assert response.finish_reason == "UNKNOWN"
    assert response.finish_reason != NO_CANDIDATE_FINISH_REASON
