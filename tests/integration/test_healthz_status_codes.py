"""Wave-3 re-audit finding 1 [LOW-MED], `src/api/main.py:77-88`:

    "the reported *value* is fixed [`db: 'degraded'` for the disk-full case per
    Appendix B.3]. The *rotation* consequence named in the same finding did not
    [land]" -- until api-surface's follow-up fix, verified by hand in a real
    container: `/healthz` now returns HTTP 503 when `db == 'error'`, while `'ok'`
    and `'degraded'` both stay HTTP 200 (Appendix B.3: a degraded-but-serving
    machine belongs in rotation; an unreachable database does not).

Carried forward from the prior phase-0 gate run as a named coverage gap
("`/healthz`'s degraded/error paths had no test") and closed here because it turned
out to be cheap: `_db_reachable` is a module-level async function referenced by
plain name inside `healthz()`, so monkeypatching `src.api.main._db_reachable`
redirects every call the handler makes without touching any real database, without
mocking HTTP (this route makes none), and without needing a live SQLite failure
mode (disk-full, permission-denied) that would be brittle to construct portably on
Windows CI.

This is the one behaviour in this round a future refactor could silently revert --
folding the branch back into a blanket `return status.HTTP_200_OK` regression would
put a database-less machine back into the Fly rotation with nothing failing loudly
except this test.

Byte-identical body requirement (DoD / PLAN.md's exact-body check, re-audit finding
10): the healthy body must remain exactly `{"status": "ok", "db": "ok", "version":
"0.1.0"}` -- pinned in `test_healthz_ok_is_200_with_exact_body` below, independent
of and in addition to the manual `curl.exe` check in the phase gate.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from fastapi.testclient import TestClient

from src.api.main import APP_VERSION, app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _fake_db_reachable(value: str) -> Callable[..., Awaitable[str]]:
    async def _fake(*_args: object, **_kwargs: object) -> str:
        return value

    return _fake


def test_healthz_ok_is_200_with_exact_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.api.main._db_reachable", _fake_db_reachable("ok"))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok", "version": APP_VERSION}


def test_healthz_degraded_is_200_not_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Appendix B.3's disk-full case: impaired, still serving, still in rotation."""
    monkeypatch.setattr("src.api.main._db_reachable", _fake_db_reachable("degraded"))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "degraded",
        "db": "degraded",
        "version": APP_VERSION,
    }


def test_healthz_error_is_503_not_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this test exists to catch: an unreachable database must pull
    the machine out of Fly/Docker rotation. Both `fly.toml`'s
    `[[http_service.checks]]` and the Dockerfile `HEALTHCHECK` key rotation purely
    on the HTTP status code, never the JSON body -- so the status code, not just the
    reported `db` value, is the load-bearing assertion here.
    """
    monkeypatch.setattr("src.api.main._db_reachable", _fake_db_reachable("error"))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "error", "db": "error", "version": APP_VERSION}
