"""The phone asks the Mac to sign a desk in.

Owner, with a screenshot: *"on iPhone I don't have it at all."* The phone
cannot read the Mac's Chrome, so it files a request; the Mac app (already
connected) claims it on a long-poll, runs the sign-in it already knows how to
do, shares the cookies on `POST /v1/logins` and reports a COUNT back here.

Pinned: the lifecycle queued -> claimed -> done / failed / expired (two
minutes), which Mac (node) handled it, the bearer on every route, and that no
cookie value ever reaches the phone -- even if a Mac sends one.

Fakes only: no Chrome, no Docker.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api, login_requests

TOKEN = "t-test-token-not-a-real-one"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "cookie-value-0xFEEDFACE"


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1_000.0}
    monkeypatch.setattr(login_requests, "_now", lambda: now["t"])
    return now


@pytest.fixture
def client(tmp_path, monkeypatch, clock):
    monkeypatch.setenv(api.TOKEN_ENV, TOKEN)
    surface = api.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        roster_path=tmp_path / "roster.json", prefs_path=tmp_path / "prefs.json",
        asks_path=tmp_path / "asks.json", handoffs_path=tmp_path / "handoffs.jsonl")
    app = FastAPI()
    api.register(app, surface=surface, background=False)
    return TestClient(app)


def _ask(client, method="chrome", origin="https://github.com/login",
         desk="probe", **extra):
    return client.post("/v1/logins/requests", headers=AUTH, json={
        "desk": desk, "origin": origin, "method": method, **extra})


def _claim(client, node="Studio Mac"):
    return client.get("/v1/logins/requests/next",
                      params={"node": node, "wait": 0}, headers=AUTH)


def _report(client, rid, **body):
    return client.post(f"/v1/logins/requests/{rid}/result", headers=AUTH,
                       json=body)


def _status(client, rid):
    return client.get(f"/v1/logins/requests/{rid}", headers=AUTH)


# -- the lifecycle -------------------------------------------------------------


def test_a_request_is_queued_with_the_site_front_door(client):
    reply = _ask(client, handoff="h1")
    assert reply.status_code == 201, reply.text
    row = reply.json()["request"]
    assert row["status"] == "queued"
    assert row["origin"] == "https://github.com"
    assert row["host"] == "github.com"
    assert row["method"] == "chrome" and row["desk"] == "probe"
    assert row["handoff"] == "h1"
    assert row["node"] is None


def test_the_mac_claims_it_once_and_its_name_is_shown(client):
    rid = _ask(client).json()["request"]["id"]
    claimed = _claim(client).json()["request"]
    assert claimed["id"] == rid and claimed["status"] == "claimed"
    assert claimed["node"] == "Studio Mac"
    # A second Mac polling finds nothing: one request, one sign-in.
    assert _claim(client, node="Other Mac").json()["request"] is None
    assert _status(client, rid).json()["request"]["node"] == "Studio Mac"


def test_done_carries_a_count_and_the_node(client):
    rid = _ask(client).json()["request"]["id"]
    _claim(client)
    reply = _report(client, rid, status="done", outcome="signed_in", desks=3)
    assert reply.status_code == 200, reply.text
    row = _status(client, rid).json()["request"]
    assert row["status"] == "done" and row["outcome"] == "signed_in"
    assert row["desks"] == 3 and row["node"] == "Studio Mac"


def test_failed_carries_the_macs_sentence(client):
    rid = _ask(client).json()["request"]["id"]
    _claim(client)
    _report(client, rid, status="failed", detail="You are not signed in to this site.")
    row = _status(client, rid).json()["request"]
    assert row["status"] == "failed"
    assert row["detail"] == "You are not signed in to this site."


def test_an_unclaimed_request_expires_after_two_minutes(client, clock):
    rid = _ask(client).json()["request"]["id"]
    clock["t"] += 119
    assert _status(client, rid).json()["request"]["status"] == "queued"
    clock["t"] += 2
    row = _status(client, rid).json()["request"]
    assert row["status"] == "expired" and row["node"] is None
    # Never handed to a Mac that wakes up late.
    assert _claim(client).json()["request"] is None


def test_a_claimed_request_that_never_reports_also_expires(client, clock):
    rid = _ask(client).json()["request"]["id"]
    _claim(client)
    clock["t"] += 121
    row = _status(client, rid).json()["request"]
    assert row["status"] == "expired" and row["node"] == "Studio Mac"
    assert _report(client, rid, status="done", desks=1).status_code == 409


def test_a_result_needs_a_claim_first(client):
    rid = _ask(client).json()["request"]["id"]
    assert _report(client, rid, status="done", desks=1).status_code == 409


def test_passkey_is_a_method_and_opened_is_its_outcome(client):
    rid = _ask(client, method="passkey").json()["request"]["id"]
    assert _claim(client).json()["request"]["method"] == "passkey"
    _report(client, rid, status="done", outcome="opened")
    assert _status(client, rid).json()["request"]["outcome"] == "opened"


# -- refusals ------------------------------------------------------------------


@pytest.mark.parametrize("body,reason", [
    ({"desk": "probe", "origin": "https://x.com", "method": "teleport"}, "bad_method"),
    ({"desk": "probe", "origin": "http://x.com", "method": "chrome"}, "bad_origin"),
    ({"desk": "probe", "origin": "not a url", "method": "chrome"}, "bad_origin"),
    ({"desk": "", "origin": "https://x.com", "method": "chrome"}, "missing_field"),
])
def test_bad_requests_are_refused_with_a_slug(client, body, reason):
    reply = client.post("/v1/logins/requests", headers=AUTH, json=body)
    assert reply.status_code == 400
    assert reply.json()["reason"] == reason


def test_unknown_request_is_404(client):
    assert _status(client, "nope").status_code == 404


@pytest.mark.parametrize("method,path", [
    ("post", "/v1/logins/requests"),
    ("get", "/v1/logins/requests/next"),
    ("get", "/v1/logins/requests/abc"),
    ("post", "/v1/logins/requests/abc/result"),
])
def test_every_route_needs_the_bearer(client, method, path):
    reply = getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
    assert reply.status_code == 401


# -- the phone never sees a cookie ---------------------------------------------


def test_cookies_sent_with_a_result_are_never_stored_or_returned(client, tmp_path):
    rid = _ask(client).json()["request"]["id"]
    _claim(client)
    _report(client, rid, status="done", desks=2, cookies=[
        {"name": "SID", "value": SECRET, "domain": ".github.com"}],
        detail="ok", value=SECRET)
    text = _status(client, rid).text
    assert SECRET not in text and "cookies" not in text
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(errors="ignore"), path


def test_a_row_has_only_the_known_fields(client):
    rid = _ask(client, cookies=[{"value": SECRET}]).json()["request"]["id"]
    row = _status(client, rid).json()["request"]
    assert set(row) == set(login_requests.FIELDS)
    assert SECRET not in str(row)
