"""`to_gemini_schema` and the Appendix B.1 error classification.

The schema translator is the reason `propertyOrdering` exists in the output at all, and
`propertyOrdering` is what makes PLAN.md's "the model reasons before it concludes" true at
generation time rather than only on paper. That property is asserted here against the two
models it was written for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from src.harness.errors import SchemaTranslationError
from src.harness.llm import (
    AUTH_FAILURE_MESSAGE,
    LlmAuthError,
    LlmRateLimited,
    LlmTimeout,
    LlmUpstreamError,
    classify_provider_error,
    to_gemini_schema,
)
from src.integrations.cicd.schemas import Diagnosis, InvestigationNotes, RemediationPlan


class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    count: int = 0


class Outer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reasoning: str = Field(max_length=100)
    choice: Literal["a", "b"]
    inner: Inner
    maybe_inner: Inner | None = None
    items: list[Inner] = []
    note: str | None = None


def test_refs_are_dereferenced_inline() -> None:
    """The dialect has no `$ref`, so nothing may survive that needs one to be read."""
    schema = to_gemini_schema(Outer)
    rendered = repr(schema)

    assert "$ref" not in rendered
    assert "$defs" not in rendered
    assert schema["properties"]["inner"]["properties"]["label"]["type"] == "STRING"


def test_optional_becomes_nullable_not_any_of() -> None:
    schema = to_gemini_schema(Outer)

    assert schema["properties"]["note"] == {"type": "STRING", "nullable": True}
    maybe = schema["properties"]["maybe_inner"]
    assert maybe["type"] == "OBJECT"
    assert maybe["nullable"] is True
    assert "anyOf" not in maybe


def test_literals_become_enums_and_lists_keep_their_items() -> None:
    schema = to_gemini_schema(Outer)

    assert schema["properties"]["choice"]["enum"] == ["a", "b"]
    assert schema["properties"]["items"]["type"] == "ARRAY"
    assert schema["properties"]["items"]["items"]["type"] == "OBJECT"


def test_unsupported_keywords_are_stripped() -> None:
    """`maxLength` is deliberately absent from this list (review.md finding 8): the
    dialect carries string bounds so the model can be shown a constraint it will
    otherwise be graded against blind. `format` is added (the bonus fix): Pydantic
    emits it for `datetime`/`UUID`/`HttpUrl` and the dialect accepts only a narrow
    set of values for it, so an unrecognised one is an opaque 400 rather than a
    silently-ignored keyword like the others here.
    """
    schema = to_gemini_schema(Outer)
    rendered = repr(schema)

    for keyword in ("title", "format", "additionalProperties", "default", "$defs"):
        assert keyword not in rendered


def test_string_length_bounds_survive_into_the_dialect() -> None:
    """review.md finding 8: `Field(max_length=...)` must reach the model, not just
    Pydantic's own validator on the way back -- otherwise every violation burns a
    repair round trip against a daily request quota.
    """

    class Bounded(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        reasoning: str = Field(max_length=1200)
        short: str = Field(min_length=2, max_length=10)

    schema = to_gemini_schema(Bounded)

    assert schema["properties"]["reasoning"] == {"type": "STRING", "maxLength": 1200}
    assert schema["properties"]["short"] == {
        "type": "STRING", "minLength": 2, "maxLength": 10,
    }


def test_diagnosis_and_investigation_notes_string_bounds_pinned_against_the_sdk() -> None:
    """Pinned against the real dialect (`google_genai==2.22.0`, `types.py:2959`/`:2975`
    define `min_length`/`max_length` on `Schema`, "If type is STRING"), not from memory.
    A *list* bound stays `maxItems`, never `maxLength` -- the two keywords police
    different things and the dialect would silently misapply a length bound to an array.
    """
    diagnosis_schema = to_gemini_schema(Diagnosis)
    assert diagnosis_schema["properties"]["reasoning"] == {
        "type": "STRING", "maxLength": 1200,
    }
    assert diagnosis_schema["properties"]["summary"] == {"type": "STRING", "maxLength": 280}
    citation_schema = diagnosis_schema["properties"]["citations"]
    assert citation_schema["type"] == "ARRAY"
    assert citation_schema["maxItems"] == 6
    assert "maxLength" not in citation_schema
    quote_schema = citation_schema["items"]["properties"]["quote"]
    assert quote_schema == {"type": "STRING", "maxLength": 500}
    note_schema = citation_schema["items"]["properties"]["note"]
    assert note_schema["maxLength"] == 200

    notes_schema = to_gemini_schema(InvestigationNotes)
    assert notes_schema["properties"]["narrative"] == {"type": "STRING", "maxLength": 800}


def test_format_is_stripped_and_the_pin_is_non_vacuous() -> None:
    """Bonus fix (not a numbered finding): `format` came out of `_ALLOWED_SCHEMA_KEYS`
    and the copy loop, aligning code to PLAN.md:170. Nothing generated today emits a
    `format` keyword, so a test that only checked "'format' not in repr(...)" against
    an ordinary model would pass against a translator that never had the chance to
    strip anything. The non-vacuity assertion (`Formatted`'s own
    `model_json_schema()` genuinely contains `format` for each of these three types)
    is the load-bearing half: it proves the input this test feeds `to_gemini_schema`
    actually exercises the strip, not just a schema `format` was never going to touch.
    """

    class Formatted(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        when: datetime
        ident: UUID
        link: HttpUrl

    pydantic_schema = Formatted.model_json_schema()
    assert "format" in pydantic_schema["properties"]["when"]  # non-vacuous
    assert "format" in pydantic_schema["properties"]["ident"]
    assert "format" in pydantic_schema["properties"]["link"]

    rendered = repr(to_gemini_schema(Formatted))
    assert "format" not in rendered


def test_property_ordering_follows_declaration_order() -> None:
    schema = to_gemini_schema(Outer)
    assert schema["propertyOrdering"] == [
        "reasoning", "choice", "inner", "maybe_inner", "items", "note"
    ]


def test_diagnosis_makes_the_model_reason_before_it_concludes() -> None:
    """PLAN.md's ordering rule, asserted where it actually takes effect.

    `Diagnosis.reasoning` being declared first is only meaningful if it survives into
    `propertyOrdering` — that is the field the provider generates in order.
    """
    ordering = to_gemini_schema(Diagnosis)["propertyOrdering"]

    assert ordering[0] == "reasoning"
    assert ordering.index("reasoning") < ordering.index("category")
    assert ordering.index("reasoning") < ordering.index("summary")
    assert ordering.index("reasoning") < ordering.index("self_confidence")


def test_remediation_plan_reasons_before_it_acts() -> None:
    ordering = to_gemini_schema(RemediationPlan)["propertyOrdering"]

    assert ordering[0] == "rationale"
    assert ordering.index("rationale") < ordering.index("action")


def test_investigation_notes_translates() -> None:
    schema = to_gemini_schema(InvestigationNotes)

    assert schema["type"] == "OBJECT"
    assert set(schema["propertyOrdering"]) == {
        "observations", "additional_tool_calls", "narrative"
    }


def test_a_recursive_model_is_refused_rather_than_inlined_forever() -> None:
    class Node(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        name: str
        child: Node | None = None

    Node.model_rebuild()

    with pytest.raises(SchemaTranslationError, match="recursive"):
        to_gemini_schema(Node)


def test_a_discriminated_union_is_refused_with_advice() -> None:
    """PLAN.md: reject it here rather than emit a schema the provider silently ignores."""

    class Cat(BaseModel):
        kind: Literal["cat"] = "cat"
        lives: int = 9

    class Dog(BaseModel):
        kind: Literal["dog"] = "dog"
        tricks: int = 0

    class Pet(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        animal: Cat | Dog = Field(discriminator="kind")

    with pytest.raises(SchemaTranslationError, match="flatten"):
        to_gemini_schema(Pet)


# --- Appendix B.1 classification -------------------------------------------


class FakeProviderError(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def test_401_and_403_are_auth_failures() -> None:
    for code in (401, 403):
        error = classify_provider_error(FakeProviderError("nope", code))
        assert isinstance(error, LlmAuthError)
        assert str(error) == AUTH_FAILURE_MESSAGE


def test_an_invalid_key_is_auth_even_though_it_arrives_as_400() -> None:
    """The real shape: this provider rejects a bad key with 400 INVALID_ARGUMENT.

    Classifying on status alone reads that as a generic bad request and spends the whole
    transient retry budget on a credential that cannot become valid.
    """
    error = classify_provider_error(
        FakeProviderError(
            "400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': "
            "'API key not valid. Please pass a valid API key.', "
            "'status': 'INVALID_ARGUMENT'}}",
            400,
        )
    )

    assert isinstance(error, LlmAuthError)
    assert str(error) == AUTH_FAILURE_MESSAGE


def test_the_auth_message_never_carries_the_key() -> None:
    error = classify_provider_error(
        FakeProviderError("API key not valid: AIzaSyREDACTEDLOOKINGKEY123456789", 400)
    )
    assert "AIzaSy" not in str(error)


def test_rate_limit_and_upstream_and_timeout() -> None:
    assert isinstance(
        classify_provider_error(FakeProviderError("slow down", 429)), LlmRateLimited
    )
    assert isinstance(
        classify_provider_error(FakeProviderError("boom", 503)), LlmUpstreamError
    )
    assert isinstance(
        classify_provider_error(FakeProviderError("request timed out")), LlmTimeout
    )


def test_an_unclassified_error_names_its_type_but_not_the_provider_body() -> None:
    error = classify_provider_error(
        FakeProviderError("secret-ish body echoing the prompt", 418)
    )
    assert isinstance(error, LlmUpstreamError)
    assert "secret-ish" not in str(error)
    assert "FakeProviderError" in str(error)
