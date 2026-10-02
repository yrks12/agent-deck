"""K1: the `/v1/skills/*` router (C5) over real HTTP, on a bare FastAPI app.

The router is built here, not mounted: slice W1 mounts it in `server/app.py`.
These pin the exact route set, the bearer on every route, the wire shapes
(202 op, 409 `needs_confirmation` carrying the command and its sha256, the
`{ok:false, reason, detail}` refusals) and that the router never blocks on the
CLI inside the event loop's thread.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_skills import ADLC, CMD, CRUNCH, SDK, Rig, _sha

try:  # absent, every test must FAIL on behaviour, not error at collection
    from server import skills_api
except ImportError:  # pragma: no cover - the RED state
    skills_api = None

TOKEN = "test-token-not-a-real-one"
BEARER = {"Authorization": f"Bearer {TOKEN}"}

#: C5, restated rather than imported from the code under test.
C5_ROUTES = {
    ("GET", "/v1/skills/catalog"),
    ("GET", "/v1/skills/ops/{op_id}"),
    ("POST", "/v1/skills/install"),
    ("GET", "/v1/skills/{skill_id}"),
    ("DELETE", "/v1/skills/{skill_id}"),
    ("PATCH", "/v1/skills/{skill_id}"),
}


class Api:
    def __init__(self, tmp_path) -> None:
        assert skills_api is not None, "server/skills_api.py does not exist"
        self.rig = Rig(tmp_path)
        self.router = skills_api.build_router(self.rig.svc)
        app = FastAPI()
        app.include_router(self.router)
        self.client = TestClient(app)

    def get(self, path, **kw):
        return self.client.get(path, headers=BEARER, **kw)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DECK_TOKEN", TOKEN)
    return Api(tmp_path)


def test_the_router_serves_exactly_c5(api):
    served = {(m, r.path) for r in api.router.routes for m in r.methods}
    assert served == C5_ROUTES


def test_every_route_refuses_without_the_bearer(api):
    for method, path in C5_ROUTES:
        url = path.format(op_id="sk_000000000000", skill_id=SDK)
        r = api.client.request(method, url, json={"id": CRUNCH, "scope": "all", "enabled": True})
        assert r.status_code == 401, (method, path, r.status_code)
        assert r.json()["reason"] == "unauthorized"
    assert not [a for a, _ in api.rig.cli.calls if a[1] != "list"], "no CLI call before auth"


def test_catalog_over_the_wire(api):
    r = api.get("/v1/skills/catalog", params={"q": "sdk", "installed": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["items"][0]["id"] == SDK and body["stale"] is False
    r = api.get("/v1/skills/catalog", params={"limit": 2, "offset": 0, "category": "development"})
    assert [i["id"] for i in r.json()["items"]] == [SDK, ADLC]
    assert api.get("/v1/skills/catalog", params={"limit": 0}).status_code == 400
    assert api.get("/v1/skills/catalog", params={"installed": "maybe"}).status_code == 400


def test_detail_over_the_wire(api):
    r = api.get(f"/v1/skills/{SDK}")
    assert r.status_code == 200 and r.json()["components"]["commands"] == ["new-sdk-app"]
    r = api.get("/v1/skills/nope@nowhere")
    assert r.status_code == 404 and r.json() == {
        "ok": False, "reason": "unknown_skill", "detail": r.json()["detail"]}


def test_install_is_202_then_the_op_reports_done(api):
    r = api.client.post("/v1/skills/install", headers=BEARER, json={"id": CRUNCH, "scope": "all"})
    assert r.status_code == 202
    op = r.json()
    assert op["state"] == "running" and op["op_id"].startswith("sk_")
    s = api.get(f"/v1/skills/ops/{op['op_id']}")
    assert s.status_code == 200
    assert s.json() == {"state": "done", "reason": None, "detail": s.json()["detail"],
                        "applies": "next_session"}
    assert api.get("/v1/skills/ops/sk_000000000000").json()["reason"] == "unknown_op"


def test_install_for_one_agent(api):
    r = api.client.post("/v1/skills/install", headers=BEARER,
                        json={"id": CRUNCH, "scope": "desk", "desk": "atlas"})
    assert r.status_code == 202
    r = api.client.post("/v1/skills/install", headers=BEARER,
                        json={"id": CRUNCH, "scope": "desk", "desk": "ghost"})
    assert r.status_code == 404 and r.json()["reason"] == "unknown_desk"


def test_needs_confirmation_is_409_with_the_command_and_its_sha(api):
    r = api.client.post("/v1/skills/install", headers=BEARER, json={"id": CMD, "scope": "all"})
    assert r.status_code == 409
    body = r.json()
    sha = _sha(CMD, "true")
    assert body["ok"] is False and body["reason"] == "needs_confirmation"
    assert body["command"] == "true" and body["sha256"] == sha and body["detail"]
    r = api.client.post("/v1/skills/install", headers=BEARER,
                        json={"id": CMD, "scope": "all", "accept_command": sha})
    assert r.status_code == 202


@pytest.mark.parametrize("body", [[], {"scope": "all"}, {"id": 3, "scope": "all"},
                                  {"id": CRUNCH, "scope": "all", "accept_command": 5}])
def test_a_malformed_install_body_is_bad_input(api, body):
    r = api.client.post("/v1/skills/install", headers=BEARER, json=body)
    assert r.status_code == 400 and r.json()["reason"] == "bad_input"


def test_uninstall_and_toggle_over_the_wire(api):
    r = api.client.patch(f"/v1/skills/{SDK}", headers=BEARER, json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = api.client.patch(f"/v1/skills/{SDK}", headers=BEARER, json={"enabled": "no"})
    assert r.status_code == 400 and r.json()["reason"] == "bad_input"
    r = api.client.delete(f"/v1/skills/{SDK}", headers=BEARER,
                          params={"scope": "desk", "desk": "scratch"})
    assert r.status_code == 200 and r.json() == {"ok": True, "applies": "next_session"}
    r = api.client.delete(f"/v1/skills/{SDK}", headers=BEARER)
    assert r.status_code == 200
    r = api.client.delete(f"/v1/skills/{SDK}", headers=BEARER, params={"scope": "all"})
    assert r.status_code == 404 and r.json()["reason"] == "not_installed"
    r = api.client.delete(f"/v1/skills/{SDK}", headers=BEARER, params={"scope": "nope"})
    assert r.status_code == 400 and r.json()["reason"] == "bad_scope"


def test_cli_missing_is_a_plain_refusal(api):
    api.rig.cli.raise_next = FileNotFoundError("claude")
    r = api.get("/v1/skills/catalog")
    assert r.status_code == 503 and r.json()["reason"] == "cli_missing"
