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


def test_unavailable_overrides_an_explicit_zero() -> None:
    """The fail-open the final audit found, now closed. An earlier version honoured an
    explicitly supplied count via `model_fields_set`, which fails open on the exact
    shape it was written for: a caller reading a degraded `MemoryHit`, computing `0`
    from its empty `actions_in_window`, and passing that `0` alongside
    `unavailable=True`. B.3 does not admit an override, so neither does this.
    """
    history = PriorHistory(signature_id=None, unavailable=True, retries_in_24h=0)
    assert history.retries_in_24h == 999


def test_not_unavailable_keeps_the_ordinary_zero_default() -> None:
    """The control case: the field default (`0`, per A.11) is untouched when the
    history is not degraded -- the validator's implication runs only one way.
    """
    history = PriorHistory(signature_id="sig_123", unavailable=False)
    assert history.retries_in_24h == 0


def test_unavailable_overrides_an_explicit_nonzero_value_too() -> None:
    """Not only the zero case: a stale count read before the store degraded is still a
    count nobody can vouch for, and B.3's answer to "we cannot read the history" is the
    same number regardless of what the last readable value happened to be.
    """
    history = PriorHistory(signature_id=None, unavailable=True, retries_in_24h=3)
    assert history.retries_in_24h == 999


def test_still_frozen_after_the_validator_mutates_it() -> None:
    """The validator's `object.__setattr__` is a documented, single, internal mutation
    at construction time -- it must not have reopened the model to mutation generally.
    """
    history = PriorHistory(signature_id=None, unavailable=True)
    assert history.retries_in_24h == 999

    with pytest.raises(ValidationError):
        history.retries_in_24h = 0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Fix round: re-audit finding 4 -- the fail-closed rule did not survive
# `model_copy`, which pydantic documents as *not* re-running validators.
# `PriorHistory.model_copy` is now overridden to reapply the same rule to the
# returned copy. Phase 3's memory wiring is expected to read a history, then flip
# `unavailable` on the already-constructed object -- i.e. exactly
# `history.model_copy(update={"unavailable": True})`.
# ---------------------------------------------------------------------------


def test_model_copy_with_unavailable_flipped_on_fails_closed_to_999() -> None:
    """The hole the audit found: before the fix this returned `0`, not `999`, because
    `model_copy` never re-runs the `after` validator that the plain-construction tests
    above rely on.
    """
    history = PriorHistory(signature_id="sig_123", occurrences=2)
    assert history.retries_in_24h == 0  # not unavailable yet -- ordinary default

    degraded = history.model_copy(update={"unavailable": True})
    assert degraded.unavailable is True
    assert degraded.retries_in_24h == 999
    # The override must return a genuine, independent copy, not mutate the original.
    assert history.retries_in_24h == 0
    assert history.unavailable is False


def test_model_copy_explicit_retries_in_the_same_update_call_is_overridden_too() -> None:
    """`model_copy` must land on the same answer the constructor gives for the same
    field values -- setting `unavailable` and a count in one `update=` call is the
    `model_copy` spelling of the constructor case above, and answers the same way.
    """
    history = PriorHistory(signature_id="sig_123")
    degraded = history.model_copy(update={"unavailable": True, "retries_in_24h": 0})
    assert degraded.unavailable is True
    assert degraded.retries_in_24h == 999


def test_model_copy_preserves_an_already_fail_closed_instance_on_an_unrelated_update() -> None:
    """A later, unrelated `model_copy` must not disturb the fail-closed value, and must
    not re-derive it into something else either -- the rule is idempotent.
    """
    history = PriorHistory(signature_id="sig_123", unavailable=True, retries_in_24h=5)
    assert history.retries_in_24h == 999  # supplied count does not override B.3

    later = history.model_copy(update={"occurrences": 1})
    assert later.retries_in_24h == 999
    assert later.occurrences == 1
    assert later.unavailable is True


def test_read_then_degrade_is_the_shape_the_override_exists_for() -> None:
    """The final audit's repro, verbatim: read a real history carrying a real retry
    count, then flip `unavailable` on the constructed object when a follow-up read
    degrades. This is the Phase 3 wiring shape, and it used to keep the stale count.
    """
    history = PriorHistory.model_validate(
        {"signature_id": "sig_123", "retries_in_24h": 2, "occurrences": 5}
    )
    assert history.retries_in_24h == 2  # readable history: the real count stands

    degraded = history.model_copy(update={"unavailable": True})
    assert degraded.retries_in_24h == 999


def test_model_copy_deep_variant_also_fails_closed() -> None:
    history = PriorHistory(signature_id="sig_123")
    degraded = history.model_copy(update={"unavailable": True}, deep=True)
    assert degraded.retries_in_24h == 999


def test_model_construct_deliberately_bypasses_the_fail_closed_rule() -> None:
    """`model_construct` is pydantic's documented validation bypass -- a caller
    reaching for it directly is opting out of validation on purpose, and this class
    deliberately does not re-derive fail-closed state there. Pinned as intended
    behaviour (per the fix's own docstring), not as an oversight to "fix" later.
    """
    history = PriorHistory.model_construct(unavailable=True)
    assert history.retries_in_24h == 0
