"""The /v1 surface is actually mounted on the daemon's app.

`server/api.py` is a router. A router that nobody calls `register()` on is a
module with passing tests and no reachable endpoint -- which is exactly what a
phone would experience as "the app cannot connect", with nothing in any log
saying why.

Every assertion here is against the REAL `server.app.app`, not a bare router,
because the thing being pinned is the wiring, not the handler.
"""

import os

import pytest
from fastapi.testclient import TestClient

from server import api as api_mod
from server import app as app_mod
from server import office


@pytest.fixture
def client(monkeypatch, short_tmp):
    """The daemon's own app, with /v1 state pointed at throwaway files."""
    monkeypatch.setattr(api_mod, "DEFAULT_PREFS_PATH", short_tmp / "prefs.json",
                        raising=False)
    # Entering TestClient fires the daemon's startup event, which runs a
    # collector tick, which calls `office.publish()`. `publish` takes no path,
    # so it writes `office.OFFICE_FILE` -- the board `cc-office.js` reads in
    # every live session to resolve a peer's address. Wiring is what this file
    # pins; republishing the office while it does that is not part of it.
    monkeypatch.setattr(office, "OFFICE_FILE", short_tmp / "office.json")
    with TestClient(app_mod.app) as c:
        yield c


@pytest.mark.parametrize("path", ["/v1/agents", "/v1/threads"])
def test_every_client_path_is_reachable_through_the_daemon(client, monkeypatch, path):
    """The good signal: a real client request reaches a real handler.

    Asserting on `app.routes` would be wrong -- the surface is mounted as a
    sub-application, so its children never appear in the parent's route table
    even when everything works. What a phone can actually reach is the only
    thing worth pinning. Revert the mount and this is a 404, which is exactly
    what a client cannot tell apart from the daemon being down.
    """
    monkeypatch.setenv("AGENT_DECK_TOKEN", "t0k3n-for-tests")
    r = client.get(path, headers={"Authorization": "Bearer t0k3n-for-tests"})
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"


def test_v1_answers_through_the_real_app_when_a_token_is_set(client, monkeypatch):
    """A correct token gets a real body. This is the positive half.

    Without it, a test that only checked 503-when-unconfigured would pass
    against an app where /v1 was never mounted at all -- a 404 and a 503 are
    both "not 200", and only one of them means what we want.
    """
    monkeypatch.setenv("AGENT_DECK_TOKEN", "t0k3n-for-tests")
    r = client.get("/v1/agents", headers={"Authorization": "Bearer t0k3n-for-tests"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "agents" in body
    assert isinstance(body["agents"], list)


def test_v1_fails_closed_when_no_token_is_configured(client, monkeypatch):
    """Unconfigured is 503 with a named reason -- never open, never a bare 404."""
    monkeypatch.delenv("AGENT_DECK_TOKEN", raising=False)
    r = client.get("/v1/agents", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503
    assert r.json()["reason"] == "auth_not_configured"


def test_the_old_surface_still_works(client):
    """Mounting /v1 must not disturb what the web board already calls."""
    r = client.get("/api/state")
    assert r.status_code == 200
    assert "sessions" in r.json()
