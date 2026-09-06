"""Structured-generation seam.

Frozen transcription of PLAN.md Appendix A.10. The harness sends a prompt plus an
already-flattened schema and receives raw text plus usage; parsing back into a typed
model is the caller's responsibility (PLAN.md: the response is *still* validated with
``model_validate_json``, because a response schema is a strong hint, not a guarantee).
"""

from __future__ import annotations

import warnings
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from src.harness.contracts import TokenUsage

# PLAN.md "Concrete numbers in one place": provider request timeout.
DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 60.0

with warnings.catch_warnings():
    # `schema` is a deprecated classmethod on `BaseModel`, so declaring a field of that
    # name makes Pydantic v2 emit a shadowing UserWarning. Appendix A names the field
    # `schema`; renaming it would be a contract change, so the warning is suppressed at
    # the single point where it is raised rather than the field being renamed.
    warnings.filterwarnings(
        "ignore",
        message=r'Field name "schema".*shadows an attribute',
        category=UserWarning,
    )

    class LlmRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        model: str
        prompt: str
        # The ignore below pairs with the warnings filter above: this field deliberately
        # shadows the deprecated `BaseModel.schema` classmethod, because Appendix A names
        # the field `schema` and renaming it would be a contract change.
        schema: dict[str, JsonValue] | None  # type: ignore[assignment]  # provider-flattened
        temperature: float = 0.0
        max_output_tokens: int = 4096
        thinking_budget: int | None = None
        timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S


class RawLlmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    tokens: TokenUsage
    finish_reason: str
    model: str
    latency_ms: int


class LlmClient(Protocol):
    """A single-shot text generator. Concrete clients are injected at the composition root."""

    async def generate(self, req: LlmRequest) -> RawLlmResponse: ...


def to_gemini_schema(model: type[BaseModel]) -> dict[str, JsonValue]:
    """Rewrite a Pydantic JSON Schema into the constrained dialect the provider accepts.

    Dereferences ``$defs``/``$ref`` inline, rewrites ``anyOf: [T, null]`` into ``T`` plus
    ``nullable: true``, strips keywords the provider rejects, adds ``propertyOrdering``
    from the declared field order, and raises :class:`SchemaTranslationError` for a
    nested discriminated union, which must be flattened instead.
    """
    raise NotImplementedError
