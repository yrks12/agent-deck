"""A paired device must reach EVERY `/v1` route, and a stranger none.

**The defect this class pins.** `api._authorise` used to know one credential,
the master `AGENT_DECK_TOKEN`. Pairing (K3) adds a second: a per-device token.
Teaching the gate a second credential in one place is easy; the failure is a
route that keeps its own copy of the check (as `calls.py` and `mac_api.py` do
through the shared `_authorise`, and as the next slice's router might not) and
so answers 401 to a phone that paired fine. One phone then "works except for
the call button", which is the kind of bug nobody finds until the demo.

So this is a sweep over the routes that exist, not a list of the ones somebody
remembered:

* every `/v1` path the mounted app publishes is called once with the master
  token and once with a device token, and the two answers must be identical
  (whatever a route says to the owner -- 200, 404 for an unknown desk, 422 for
  an empty body -- it says to a paired phone), never 401/429/503;
* every `/v1` path refuses a caller with no token;
* and, because an SSE route cannot be called and closed from a test client,
  every `@router.<verb>` handler under `server/` that serves a `/v1` path is
  read from source and must call `_authorise` -- so a route added tomorrow
  that forgets is caught here even if nobody adds it to a list.
"""

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import api, ratelimit
from server.pairing import DeviceStore

MASTER = "m-test-master-token-not-a-real-one"
SERVER = Path(__file__).resolve().parent.parent / "server"
#: `/v1/pair` is the one door whose credential is the pairing code.
OPEN_DOORS = {"/v1/pair"}
#: An SSE stream never ends, so a test client cannot call it and hang up. It is
#: covered by the source sweep below instead.
STREAMS = {"/v1/stream"}
GATE_NAMES = {"_authorise", "_auth", "_node_auth"}
METHODS = ("get", "post", "patch", "put", "delete")


def _routes():
    out = []
    for path, verbs in app_mod.app.openapi()["paths"].items():
        if path.startswith("/v1") and path not in OPEN_DOORS | STREAMS:
            out += [(verb, path) for verb in verbs if verb in METHODS]
    return sorted(out)


ROUTES = _routes()


@pytest.fixture
def paired(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DECK_TOKEN", MASTER)
    gate = api._GATE
    assert gate is not None, "the /v1 gate is not wired: pair_api is not mounted"
    monkeypatch.setattr(gate, "devices", DeviceStore(tmp_path / "state"))
    monkeypatch.setattr(gate, "limiter", ratelimit.RateLimiter())
    token, _device = gate.devices.add("Dan's phone")
    return token


def _call(client, verb, path, token):
    url = path.replace("{", "").replace("}", "")  # `{name}` -> `name`: an unknown id
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.request(verb.upper(), url, headers=headers)


@pytest.fixture
def client():
    c = TestClient(app_mod.app, client=("198.51.100.4", 50000))
    c.headers.pop("authorization", None)
    return c


def test_the_sweep_actually_found_the_routes():
    """An empty parametrize list passes silently -- the classic way a sweep dies."""
    assert len(ROUTES) >= 30, ROUTES
    assert ("post", "/v1/calls") in ROUTES      # a router `api.py` does not define
    assert ("get", "/v1/agents") in ROUTES


@pytest.mark.parametrize("verb,path", ROUTES, ids=[f"{v}:{p}" for v, p in ROUTES])
def test_a_device_token_is_answered_exactly_like_the_master(client, paired, verb, path):
    owner = _call(client, verb, path, MASTER)
    device = _call(client, verb, path, paired)
    assert owner.status_code not in (401, 429, 503), (owner.status_code, owner.text[:200])
    assert device.status_code == owner.status_code, (device.text[:200], owner.text[:200])


@pytest.mark.parametrize("verb,path", ROUTES, ids=[f"{v}:{p}" for v, p in ROUTES])
def test_no_token_and_a_made_up_token_reach_no_route(client, paired, verb, path):
    # 422 is FastAPI rejecting an empty body/query before the handler (and so
    # the gate) runs: nothing is served. Anything else but 401 would be a leak.
    assert _call(client, verb, path, None).status_code in (401, 422)
    assert _call(client, verb, path, "adt_" + "A" * 43).status_code in (401, 422)


def _v1_handlers():
    """(file, function name, calls _authorise?) for every route handler that
    hangs off a router whose paths live under `/v1`."""
    found = []
    for src in sorted(SERVER.glob("*.py")):
        tree = ast.parse(src.read_text(encoding="utf-8"))
        text = src.read_text(encoding="utf-8")
        if 'prefix="/v1"' not in text and '"/v1/' not in text:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            is_route = any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"
                and d.func.attr in METHODS
                for d in fn.decorator_list)
            if not is_route:
                continue
            # A reference, not only a call: `asyncio.to_thread(_node_auth, ...)`
            # passes the helper without calling it. `_auth` / `_node_auth` are
            # mac_api's wrappers and both end in `_authorise`.
            calls = any(isinstance(n, ast.Name) and n.id in GATE_NAMES
                        for n in ast.walk(fn))
            found.append((src.name, fn.name, calls))
    return found


#: Routes that authorise some other way, each for a reason written down.
#: pair_api's own routes: `/healthz` is public by design, `/v1/pair` takes the
#: pairing code, and `/v1/version` calls `gate.check` directly.
OWN_AUTH = {("pair_api.py", "healthz"), ("pair_api.py", "pair"), ("pair_api.py", "get_version")}


def test_every_v1_handler_in_the_source_goes_through_the_gate():
    handlers = _v1_handlers()
    assert len(handlers) >= 30, handlers
    naked = [(f, n) for f, n, ok in handlers if not ok and (f, n) not in OWN_AUTH]
    assert naked == [], f"these /v1 handlers never call _authorise: {naked}"
