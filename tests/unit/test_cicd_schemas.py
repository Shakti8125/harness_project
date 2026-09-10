"""`src.integrations.cicd.schemas` behaviour that is not just field-for-field
transcription: the `PriorHistory` fail-closed retry cap (review.md finding 11).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.integrations.cicd.schemas import PriorHistory


def test_unavailable_and_not_supplied_defaults_to_the_fail_closed_cap() -> None:
    """Appendix B.3: an unreadable history must not read as "no retries yet" -- the
    degraded-memory path must default `retries_in_24h` to the conservative `999` so a
    retry cap wired to this field (Phase 2) fails closed rather than fails open.
    """
    history = PriorHistory(signature_id=None, unavailable=True)
    assert history.retries_in_24h == 999


def test_unavailable_with_an_explicit_zero_survives_untouched() -> None:
    """`model_fields_set` distinguishes "not supplied" from "supplied as 0": a caller
    who explicitly means zero must not have it silently overwritten.
    """
    history = PriorHistory(signature_id=None, unavailable=True, retries_in_24h=0)
    assert history.retries_in_24h == 0


def test_not_unavailable_keeps_the_ordinary_zero_default() -> None:
    """The control case: the field default (`0`, per A.11) is untouched when the
    history is not degraded -- the validator's implication runs only one way.
    """
    history = PriorHistory(signature_id="sig_123", unavailable=False)
    assert history.retries_in_24h == 0


def test_unavailable_with_an_explicit_nonzero_value_also_survives() -> None:
    history = PriorHistory(signature_id=None, unavailable=True, retries_in_24h=3)
    assert history.retries_in_24h == 3


def test_still_frozen_after_the_validator_mutates_it() -> None:
    """The validator's `object.__setattr__` is a documented, single, internal mutation
    at construction time -- it must not have reopened the model to mutation generally.
    """
    history = PriorHistory(signature_id=None, unavailable=True)
    assert history.retries_in_24h == 999

    with pytest.raises(ValidationError):
        history.retries_in_24h = 0  # type: ignore[misc]
