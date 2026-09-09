"""`review-2.md` finding 3: a missing or malformed `prompts/*.md` file used to be a
silent green `/healthz` followed by a `500 text/plain "Internal Server Error"` on the
first `/v1/replay` call -- no RFC 9457 body, no `run_id`, nothing in the trace. Under the
pre-round Python-constant design this was structurally impossible (an `ImportError` at
process start); the `.md` port made `load_prompt_template` a lazy, unguarded file read
inside `build_prompt`.

`validate_prompt_templates()` (`src/integrations/cicd/rendering.py`) restores that
property by loading every template the integration renders, eagerly. `src/api/main.py`'s
`lifespan` calls it first, before `recorder.initialize()` -- this file pins both halves:
the function itself raises for a missing template, and the app's `lifespan` actually
calls it (so a broken template fails app *startup*, not the first request).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.integrations.cicd import rendering


@pytest.fixture(autouse=True)
def _clear_template_cache() -> None:
    rendering._template_cache.clear()
    yield
    rendering._template_cache.clear()


def test_validate_prompt_templates_succeeds_against_the_real_shipped_templates() -> None:
    """The positive control: without it, a failure below could pass vacuously (e.g. the
    function silently swallowing every error), not because it actually validated
    anything.
    """
    rendering.validate_prompt_templates()  # must not raise


def test_validate_prompt_templates_raises_for_a_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rendering, "_PROMPTS_DIR", tmp_path / "does-not-exist")

    with pytest.raises(OSError):
        rendering.validate_prompt_templates()


def test_validate_prompt_templates_raises_for_a_malformed_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that exists but is missing the `version:` front matter -- a packaging
    fault distinct from a missing file, and `load_prompt_template` raises a different
    exception type for it (`ValueError`, not `OSError`).
    """
    (tmp_path / "investigator.md").write_text("no front matter here at all", encoding="utf-8")
    (tmp_path / "diagnostician.md").write_text(
        "version: 1\n---\nfine", encoding="utf-8"
    )
    monkeypatch.setattr(rendering, "_PROMPTS_DIR", tmp_path)

    with pytest.raises(ValueError, match="version"):
        rendering.validate_prompt_templates()


def test_app_startup_fails_loudly_instead_of_serving_a_green_healthz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end claim: with a broken template directory, entering the app's ASGI
    lifespan (what `TestClient` does as a context manager, and what a real server does
    at process start) raises -- it does not start cleanly and defer the failure to the
    first request. Reproduces `review-2.md` finding 3's exact scenario (`/healthz` green,
    then a bare `500` on `/v1/replay`) as the *prevented* case: no client is ever able
    to complete a startup handshake against this broken configuration at all.
    """
    monkeypatch.setattr(rendering, "_PROMPTS_DIR", tmp_path / "does-not-exist")

    from src.api import main as api_main

    with pytest.raises(OSError), TestClient(api_main.app):
        pytest.fail(
            "the lifespan should have raised before the app finished starting up"
        )
