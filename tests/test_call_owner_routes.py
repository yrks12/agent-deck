"""The apps' side of a desk calling him: list, answer, decline, settings.

Answering joins the SAME live-call path as his own call (`POST /v1/calls`):
a call id on the desk's thread, the desk briefed by a deck line, and the
reason handed back as `opening` -- the line the app speaks first. Declining
posts the reason to his thread. Settings are one GET and one PATCH, and
turning on "Call me now" tells the chief to call.
"""

import pytest

from server import office, ringing
from test_calls import AUTH, Rig

T0 = 1_790_000_000.0


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    r.clock[0] = T0 + 2
    ringing.update({"when": "urgent"}, at=T0)
    return r


def ring(reason="prod db is down"):
    return ringing.place("atlas", reason, "urgent", chief="atlas", at=T0)["ring_id"]


def test_the_ringing_list(rig):
    rid = ring()
    body = rig.client.get("/v1/calls/incoming", headers=AUTH).json()
    assert [r["id"] for r in body["ringing"]] == [rid]


def test_answer_joins_the_live_call_with_the_desk_briefed(rig):
    rid = ring()
    r = rig.client.post(f"/v1/calls/incoming/{rid}/answer", headers=AUTH)
    assert r.status_code == 201
    body = r.json()
    assert body["call_id"].startswith("call_") and body["thread_id"] == "direct:atlas"
    assert body["ring_id"] == rid and body["opening"] == "prod db is down"
    [line] = rig.deck_lines()
    assert line["to"] == "atlas" and "picked up the call you placed" in line["text"]
    assert "prod db is down" in line["text"] and "do not repeat" in line["text"]
    again = rig.client.post(f"/v1/calls/incoming/{rid}/answer", headers=AUTH)
    assert again.status_code == 409 and again.json()["reason"] == "not_ringing"
    end = rig.client.post(f"/v1/calls/{body['call_id']}/end", headers=AUTH)
    assert end.status_code == 200, "an answered ring ends like any call"


def test_decline_posts_the_reason_to_his_thread(rig):
    rid = ring()
    r = rig.client.post(f"/v1/calls/incoming/{rid}/decline", headers=AUTH)
    assert r.json() == {"ok": True, "ring_id": rid, "state": "declined"}
    mine = [x for x in rig.records() if x["to"] == office.OWNER_INBOX]
    assert mine and "prod db is down" in mine[0]["text"]


def test_an_unknown_ring_is_a_404(rig):
    r = rig.client.post("/v1/calls/incoming/ring_nope/answer", headers=AUTH)
    assert r.status_code == 404 and r.json()["reason"] == "unknown_ring"


def test_settings_read_and_edit_and_a_bad_edit_is_refused(rig):
    got = rig.client.get("/v1/calls/settings", headers=AUTH).json()
    assert got["when"] == "urgent" and got["who"] == "chief"
    assert got["ring_seconds"] == 30
    r = rig.client.patch("/v1/calls/settings", headers=AUTH,
                         json={"when": "anytime", "max_per_day": 2})
    assert r.status_code == 200 and r.json()["when"] == "anytime"
    bad = rig.client.patch("/v1/calls/settings", headers=AUTH, json={"when": "x"})
    assert bad.status_code == 400 and bad.json()["reason"] == "bad_when"


def test_call_me_now_tells_the_chief_to_call(rig):
    r = rig.client.patch("/v1/calls/settings", headers=AUTH,
                         json={"call_me_now": True})
    assert r.json()["call_me_now"] is True
    [line] = rig.deck_lines()
    assert line["to"] == "atlas" and "mcp__deck__call_owner" in line["text"]


def test_every_route_needs_the_token(rig):
    for method, path in (("get", "/v1/calls/incoming"),
                         ("post", "/v1/calls/incoming/x/answer"),
                         ("post", "/v1/calls/incoming/x/decline"),
                         ("get", "/v1/calls/settings"),
                         ("patch", "/v1/calls/settings")):
        kw = {"json": {}} if method == "patch" else {}
        assert getattr(rig.client, method)(path, **kw).status_code == 401, path
