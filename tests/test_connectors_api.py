"""`/v1/store/*` over real HTTP: the route set, the bearer, the refusal shape,
and that a key sent in an install never comes back out of any route."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_connectors import BRAVE, KEY, MS_LEARN, NOTION, Rig

try:
    from server import connectors_api
except ImportError:  # pragma: no cover - the RED state
    connectors_api = None

TOKEN = "store-test-token-not-real"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
ROUTES = {
    ("GET", "/v1/store/catalog"), ("GET", "/v1/store/item"),
    ("POST", "/v1/store/install"), ("POST", "/v1/store/update"),
    ("POST", "/v1/store/uninstall"), ("GET", "/v1/store/installed"),
    ("POST", "/v1/store/refresh"), ("POST", "/v1/store/connect"),
    ("POST", "/v1/store/connect/complete"), ("GET", "/v1/store/connect/status"),
}


@pytest.fixture
def api(tmp_path, monkeypatch):
    assert connectors_api is not None, "server/connectors_api.py does not exist"
    monkeypatch.setenv("AGENT_DECK_TOKEN", TOKEN)
    rig = Rig(tmp_path, monkeypatch)
    router = connectors_api.build_router(rig.store)
    app = FastAPI()
    app.include_router(router)
    rig.router = router
    rig.client = TestClient(app)
    return rig


def test_the_router_serves_exactly_the_contract(api):
    served = {(m, r.path) for r in api.router.routes for m in r.methods}
    assert served == ROUTES


@pytest.mark.parametrize("method,path", sorted(ROUTES))
def test_every_route_needs_the_bearer(api, method, path):
    r = api.client.request(method, path, json={})
    assert r.status_code == 401
    assert r.json()["reason"] == "unauthorized"


def test_browse_install_list_remove(api):
    c = api.client
    page = c.get("/v1/store/catalog?kind=connector", headers=BEARER).json()
    assert MS_LEARN in {i["id"] for i in page["items"]}
    assert "refreshed_at" in page and page["stale"] is False
    item = c.get("/v1/store/item", params={"id": MS_LEARN}, headers=BEARER).json()
    assert item["trust"] == "official"
    r = c.post("/v1/store/install", headers=BEARER,
               json={"id": MS_LEARN, "desks": ["atlas"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["reload"][0]["desk"] == "atlas"
    inst = c.get("/v1/store/installed", params={"desk": "atlas"}, headers=BEARER).json()
    assert [i["id"] for i in inst["desks"][0]["items"]] == [MS_LEARN]
    r = c.post("/v1/store/uninstall", headers=BEARER,
               json={"id": MS_LEARN, "desks": ["atlas"]})
    assert r.json()["removed"] == [{"desk": "atlas", "id": MS_LEARN}]


def test_refusals_keep_their_reason(api):
    r = api.client.post("/v1/store/install", headers=BEARER,
                        json={"id": NOTION, "desks": ["atlas"]})
    assert r.status_code == 409
    assert r.json() == {"ok": False, "reason": "needs_oauth",
                        "detail": r.json()["detail"]}
    r = api.client.post("/v1/store/install", headers=BEARER,
                        json={"id": MS_LEARN, "desks": "some"})
    assert r.status_code == 400 and r.json()["reason"] == "bad_input"


def test_a_key_goes_in_and_never_comes_out(api):
    c = api.client
    r = c.post("/v1/store/install", headers=BEARER,
               json={"id": BRAVE, "desks": ["atlas"], "secrets": {"BRAVE_API_KEY": KEY}})
    assert r.status_code == 200
    bodies = [r.text,
              c.get("/v1/store/catalog?limit=200", headers=BEARER).text,
              c.get("/v1/store/item", params={"id": BRAVE}, headers=BEARER).text,
              c.get("/v1/store/installed", headers=BEARER).text]
    for body in bodies:
        assert KEY not in body
    # A refusal that echoes input must not echo the key either.
    r = c.post("/v1/store/install", headers=BEARER,
               json={"id": BRAVE, "desks": ["nobody"], "secrets": {"BRAVE_API_KEY": KEY}})
    assert r.status_code == 404 and KEY not in r.text
    assert KEY not in json.dumps(dict(r.headers))


def test_the_deck_mounts_the_store():
    from server import app as app_mod

    def walk(routes):
        # Newer FastAPI keeps an included router as one entry with no path.
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                yield from walk(inner.routes)
            else:
                yield getattr(r, "path", "")
    served = set(walk(app_mod.app.routes))
    assert {p for _, p in ROUTES} <= served
