"""Mac control, the wire: the live view, the owner's own hands, the grant.

Owner, 2026-10-01: a live screen of his Mac in the Mac and iPhone apps "like
a desk's screen view", and he can drive it from the phone "the same way he
controls a desk". The defects this closes, before any of it exists:

* There was no way to see his Mac live: only a still screenshot a desk asks
  for. The viewer apps already poll a desk's `screen` / `screen.jpg` /
  `screen/input`; the Mac gets the same three, under `/v1/nodes/{id}`.
* The Mac must capture only while someone is watching (idle CPU): a frame
  request raises a watch flag, the long-poll wakes on it, and every frame
  upload is answered with whether anyone still is.
* A desk refused for `control_off` must hear when he turns control on, and
  its card must close -- whether he tapped Allow on the card or flipped the
  switch in Settings. The Mac shows the waiting asks, so they ride the poll.
* His phone input runs ONLY under a live grant he turned on at the Mac. With
  none it is refused before anything is queued: a leaked bearer token still
  cannot put a click on his Mac.

Real HTTP through FastAPI's TestClient against the same rig as
tests/test_mac_api.py.
"""

from __future__ import annotations

import threading
import time

import pytest

from server import handoff, mac_nodes
from tests.test_mac_api import BEARER, TOKEN, Rig

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 64 + b"\xff\xd9"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DECK_TOKEN", TOKEN)
    return Rig(tmp_path)


def _input_mac(rig):
    """A Mac whose app offers `input` (what the new app registers)."""
    r = rig.client.post("/v1/nodes", headers=BEARER, json={
        "machine_id": "m-9", "name": "Sam's MacBook Pro", "os": "macOS 26",
        "app_version": "2.0", "capabilities": list(mac_nodes.CAPABILITIES),
        "mode": "ask"}).json()
    return r["node_id"], {**BEARER,
                          "X-Deck-Node": f"{r['node_id']}.{r['node_secret']}"}


def _control(rig, scope="all", secs=1800):
    return {"scope": scope, "until": rig.clock() + secs}


def _frame(rig, node_id, headers, data=JPEG, w=1440, h=900):
    return rig.client.post(
        f"/v1/nodes/{node_id}/screen/frame", content=data,
        headers={**headers, "Content-Type": "image/jpeg",
                 "X-Frame-Width": str(w), "X-Frame-Height": str(h)})


# ── the grant rides the poll, and closes the desk's card ────────────────────


def test_a_poll_reports_control_and_answers_watch_and_asks(rig):
    node_id, mac = rig.mac()
    r = rig.poll(node_id, mac, control=_control(rig, "atlas"),
                 screen={"width": 1440, "height": 900},
                 perms={"accessibility": True, "screen_recording": True})
    assert r.status_code == 200, r.text
    assert r.json()["watch"] is False and r.json()["control_asks"] == []
    assert rig.store.node(node_id)["control"]["scope"] == "atlas"


def test_turning_control_on_closes_the_card_and_tells_the_desk_once(rig):
    node_id, mac = rig.mac()
    rig.poll(node_id, mac)
    card = handoff.raise_handoff(rig.handoffs, agent="atlas", kind="other",
                                 needs="Let atlas control", state="nothing ran",
                                 where="Sam's MacBook Pro")
    rig.store.set_control_ask(node_id, "atlas", card.id)
    assert rig.poll(node_id, mac).json()["control_asks"] == ["atlas"]
    rig.poll(node_id, mac, control=_control(rig, "all"))
    assert [d for d, _ in rig.told] == ["atlas"]
    assert "control" in rig.told[0][1] and "screenshot" in rig.told[0][1]
    (closed,) = handoff.load(rig.handoffs)
    assert closed.status == "done"
    rig.poll(node_id, mac, control=_control(rig, "all"))
    assert len(rig.told) == 1


def test_a_long_poll_wakes_when_a_control_ask_lands(rig):
    node_id, mac = rig.mac()
    rig.poll(node_id, mac)
    card = handoff.raise_handoff(rig.handoffs, agent="atlas", kind="other",
                                 needs="Let atlas control", state="nothing ran",
                                 where="Sam's MacBook Pro")
    threading.Timer(0.4, lambda: rig.store.set_control_ask(
        node_id, "atlas", card.id)).start()
    started = time.monotonic()
    r = rig.poll(node_id, mac, wait=5)
    assert time.monotonic() - started < 2.5
    assert r.json()["control_asks"] == ["atlas"]


# ── the live view ────────────────────────────────────────────────────────────


def test_screen_status_has_the_desk_viewer_shape(rig):
    node_id, mac = rig.mac()
    rig.poll(node_id, mac, control=_control(rig), screen={"width": 1440,
                                                           "height": 900},
             perms={"accessibility": True, "screen_recording": True})
    r = rig.client.get(f"/v1/nodes/{node_id}/screen", headers=BEARER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["computer"]["running"] is True
    assert (body["width"], body["height"]) == (1440, 900)
    assert body["frame_url"] == f"/v1/nodes/{node_id}/screen.jpg"
    assert body["input_url"] == f"/v1/nodes/{node_id}/screen/input"
    for key in ("desk", "display", "stale_after", "generated_at"):
        assert key in body
    assert body["control"]["live"] is True


def test_screen_status_says_why_there_is_nothing_to_see(rig):
    node_id, mac = rig.mac()
    rig.poll(node_id, mac)
    body = rig.client.get(f"/v1/nodes/{node_id}/screen",
                          headers=BEARER).json()
    assert body["computer"]["running"] is False
    assert "Let agents control this Mac" in body["detail"]
    rig.poll(node_id, mac, control=_control(rig),
             perms={"accessibility": True, "screen_recording": False})
    body = rig.client.get(f"/v1/nodes/{node_id}/screen",
                          headers=BEARER).json()
    assert body["computer"]["running"] is False
    assert "Screen Recording" in body["detail"]


def test_a_frame_request_raises_the_watch_and_wakes_the_poll(rig):
    node_id, mac = rig.mac()
    rig.poll(node_id, mac, control=_control(rig))
    threading.Timer(0.4, lambda: rig.client.get(
        f"/v1/nodes/{node_id}/screen.jpg", headers=BEARER)).start()
    started = time.monotonic()
    r = rig.poll(node_id, mac, wait=5, control=_control(rig))
    assert time.monotonic() - started < 2.5
    assert r.json()["watch"] is True


def test_no_frame_yet_is_409_then_the_uploaded_frame_is_served_with_its_age(
        rig):
    node_id, mac = rig.mac()
    rig.poll(node_id, mac, control=_control(rig))
    r = rig.client.get(f"/v1/nodes/{node_id}/screen.jpg", headers=BEARER)
    assert r.status_code == 409 and r.json()["reason"] == "no_frame"
    up = _frame(rig, node_id, mac)
    assert up.status_code == 200 and up.json() == {"ok": True, "watch": True}
    r = rig.client.get(f"/v1/nodes/{node_id}/screen.jpg", headers=BEARER)
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert float(r.headers["X-Frame-Age"]) < 5
    assert r.headers["Cache-Control"] == "no-store"
    body = rig.client.get(f"/v1/nodes/{node_id}/screen", headers=BEARER).json()
    assert (body["width"], body["height"]) == (1440, 900)


def test_frame_upload_needs_the_macs_own_secret_and_a_jpeg(rig):
    node_id, mac = rig.mac()
    other_id, other = rig.mac("m-2", "Studio")
    assert _frame(rig, node_id, BEARER).status_code == 401
    assert _frame(rig, node_id, other).status_code == 401
    assert _frame(rig, node_id, mac, data=b"not a jpeg").status_code == 400
    assert _frame(rig, node_id, mac, data=b"\xff\xd8" + b"x" * (5 << 20)
                  ).status_code == 413


def test_nobody_watching_tells_the_mac_to_stop(rig):
    node_id, mac = rig.mac()
    rig.poll(node_id, mac, control=_control(rig))
    rig.client.get(f"/v1/nodes/{node_id}/screen.jpg", headers=BEARER)
    assert _frame(rig, node_id, mac).json()["watch"] is True
    rig.clock.offset += mac_nodes.WATCH_TTL + 1
    assert _frame(rig, node_id, mac).json()["watch"] is False


def test_a_stale_frame_is_not_served_as_live(rig):
    node_id, mac = rig.mac()
    rig.poll(node_id, mac, control=_control(rig))
    _frame(rig, node_id, mac)
    rig.clock.offset += 60
    r = rig.client.get(f"/v1/nodes/{node_id}/screen.jpg", headers=BEARER)
    assert r.status_code == 409 and r.json()["reason"] == "no_frame"


# ── his own hands ────────────────────────────────────────────────────────────


def test_his_input_without_a_grant_is_refused_before_anything_is_queued(rig):
    node_id, mac = rig.mac()
    rig.poll(node_id, mac)
    r = rig.client.post(f"/v1/nodes/{node_id}/screen/input", headers=BEARER,
                        json={"action": "key", "key": "Return"})
    assert r.status_code == 409 and r.json()["reason"] == "control_off"
    assert rig.store._all_jobs() == []


def test_his_tap_becomes_an_owner_input_job_in_the_frames_pixels(rig):
    node_id, mac = _input_mac(rig)
    rig.poll(node_id, mac, control=_control(rig, "atlas"))
    _frame(rig, node_id, mac, w=1440, h=900)

    def fake_mac():
        for _ in range(100):
            jobs = rig.poll(node_id, mac,
                            control=_control(rig, "atlas")).json()["jobs"]
            for job in jobs:
                rig.client.post(
                    f"/v1/nodes/{node_id}/jobs/{job['id']}/events",
                    headers=mac, json={"events": [{"seq": 1, "type": "result",
                                                   "data": {"state": "done"}}]})
                return
            time.sleep(0.02)

    t = threading.Thread(target=fake_mac)
    t.start()
    r = rig.client.post(f"/v1/nodes/{node_id}/screen/input", headers=BEARER,
                        json={"action": "click", "x": 700, "y": 450})
    t.join(5)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "action": "click"}
    (job,) = rig.store._all_jobs()
    assert job["desk"] == mac_nodes.OWNER_DESK and job["kind"] == "input"
    assert job["args"]["space"] == {"width": 1440, "height": 900}
    assert job["args"]["button"] == "left"


def test_his_off_picture_tap_is_a_400(rig):
    node_id, mac = _input_mac(rig)
    rig.poll(node_id, mac, control=_control(rig))
    _frame(rig, node_id, mac, w=1440, h=900)
    r = rig.client.post(f"/v1/nodes/{node_id}/screen/input", headers=BEARER,
                        json={"action": "click", "x": 99999, "y": 10})
    assert r.status_code == 400 and r.json()["reason"] == "bad_input"


def test_the_viewer_routes_need_the_bearer(rig):
    node_id, _ = rig.mac()
    for method, path in (("GET", "screen"), ("GET", "screen.jpg"),
                         ("POST", "screen/input")):
        r = rig.client.request(method, f"/v1/nodes/{node_id}/{path}", json={})
        assert r.status_code == 401, path


def test_an_ask_whose_card_he_closed_leaves_his_mac(rig):
    """He skipped (or finished) the card in the thread: the Mac stops showing it."""
    node_id, mac = rig.mac()
    rig.poll(node_id, mac)
    card = handoff.raise_handoff(rig.handoffs, agent="atlas", kind="other",
                                 needs="Let atlas control", state="nothing ran",
                                 where="Sam's MacBook Pro")
    rig.store.set_control_ask(node_id, "atlas", card.id)
    assert rig.poll(node_id, mac).json()["control_asks"] == ["atlas"]
    handoff.resolve(rig.handoffs, card.id, "skipped")
    assert rig.poll(node_id, mac).json()["control_asks"] == []
