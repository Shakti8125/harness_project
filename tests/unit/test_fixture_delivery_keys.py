# ruff: noqa: E501
"""Every recorded scenario has its own Appendix C key, and the replay-mode webhook route
matches each delivery to its own directory.

`AppContext.match_scenario` (Phase 5) walks `fixtures/scenarios/*/webhook.json` in sorted
order and returns the first key match; `fixtures/README.md` states "one key per scenario"
as a checklist item. Nothing pinned it (Phase 5 audit, plan drift): a sixth fixture copied
from another would have silently replayed the alphabetically-first match. This does.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from src.api.deps import FIXTURES_ROOT, _delivery_key


def _scenarios() -> list[Path]:
    return sorted(p for p in FIXTURES_ROOT.iterdir() if (p / "webhook.json").is_file())


def test_every_recorded_scenario_has_a_distinct_delivery_key() -> None:
    keys = {}
    for scenario in _scenarios():
        body = json.loads((scenario / "webhook.json").read_text(encoding="utf-8"))
        key = _delivery_key(body)
        assert key is not None, f"{scenario.name}/webhook.json is not shaped like a workflow_run delivery"
        keys[scenario.name] = key
    assert len(keys) >= 5
    duplicates = [key for key, count in Counter(keys.values()).items() if count > 1]
    assert not duplicates, {name: key for name, key in keys.items() if key in duplicates}


def test_delivery_key_refuses_non_integer_ids() -> None:
    """Phase 5 audit finding 9: `int(inf)` is an `OverflowError`, not a `ValueError`."""
    base = {"repository": {"full_name": "o/r"}}
    assert _delivery_key({**base, "workflow_run": {"id": float("inf"), "run_attempt": 1}}) is None
    assert _delivery_key({**base, "workflow_run": {"id": 1, "run_attempt": float("nan")}}) is None
    assert _delivery_key({**base, "workflow_run": {"id": "7", "run_attempt": "2"}}) == ("o/r", 7, 2)
    assert _delivery_key({**base, "workflow_run": {"id": None}}) is None
