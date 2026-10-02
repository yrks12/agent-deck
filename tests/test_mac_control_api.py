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
* Watching and driving his OWN Mac need no agent grant (owner, 2026-10-01
  21:00): the grant is for agents. His input is refused before anything is
  queued only when the Mac cannot take it (asleep, Accessibility off).

Real HTTP through FastAPI's TestClient against the same rig as
tests/test_mac_api.py.
"""

from __future__ import annotations

import threading
import time

import pytest

from server import handoff, mac_api, mac_nodes
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


PERMS = {"accessibility": True, "screen_recording": True}


def test_he_watches_his_own_mac_with_no_agent_grant(rig):
    """Owner, 2026-10-01 21:00: the iPhone viewer of his awake, permitted Mac
    said "has no computer running yet" -- the deck called it not running
    because no AGENT held a control grant. Watching is his, not an agent's."""
    node_id, mac = rig.mac()
    rig.poll(node_id, mac, perms=PERMS, screen={"width": 1512, "height": 982})
    body = rig.client.get(f"/v1/nodes/{node_id}/screen",
                          headers=BEARER).json()
    assert body["computer"]["running"] is True, body
    assert body["detail"] == "" and body["control"]["live"] is False


def test_his_frame_request_wakes_the_mac_with_no_agent_grant(rig):
    node_id, mac = rig.mac()
    rig.poll(node_id, mac, perms=PERMS)
    r = rig.client.get(f"/v1/nodes/{node_id}/screen.jpg", headers=BEARER)
    assert r.status_code == 409 and r.json()["reason"] == "frame_pending"
    assert rig.poll(node_id, mac, perms=PERMS).json()["watch"] is True
    assert _frame(rig, node_id, mac).json()["watch"] is True
    r = rig.client.get(f"/v1/nodes/{node_id}/screen.jpg", headers=BEARER)
    assert r.status_code == 200 and r.content == JPEG


def test_screen_status_says_why_there_is_nothing_to_see_in_mac_words(rig):
    node_id, mac = rig.mac()
    rig.poll(node_id, mac, perms={"accessibility": True,
                                  "screen_recording": False})
    body = rig.client.get(f"/v1/nodes/{node_id}/screen",
                          headers=BEARER).json()
    assert body["computer"]["running"] is False
    assert body["reason"] == "screen_recording_off"
    assert body["detail"].startswith("Screen Recording is off")
    assert "Settings" in body["detail"]
    assert "computer" not in body["detail"]
    r = rig.client.get(f"/v1/nodes/{node_id}/screen.jpg", headers=BEARER)
    assert r.json()["reason"] == "screen_recording_off"
    rig.clock.offset += mac_nodes.ONLINE_WINDOW + 5
    body = rig.client.get(f"/v1/nodes/{node_id}/screen",
                          headers=BEARER).json()
    assert body["computer"]["running"] is False
    assert body["reason"] == "mac_asleep"
    assert "asleep" in body["detail"] and "Sam's MacBook Pro" in body["detail"]


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
    assert r.status_code == 409 and r.json()["reason"] == "frame_pending"
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
    rig.poll(node_id, mac, control=_control(rig))
    r = rig.client.get(f"/v1/nodes/{node_id}/screen.jpg", headers=BEARER)
    assert r.status_code == 409 and r.json()["reason"] == "frame_pending"


# ── his own hands ────────────────────────────────────────────────────────────


def test_his_input_needs_no_agent_grant(rig, monkeypatch):
    """Driving his own Mac from his phone is his, not an agent's: no grant."""
    monkeypatch.setattr(mac_api, "INPUT_WAIT", 1.0)
    node_id, mac = _input_mac(rig)
    rig.poll(node_id, mac, perms=PERMS, mode="full")
    _frame(rig, node_id, mac, w=1440, h=900)
    r = rig.client.post(f"/v1/nodes/{node_id}/screen/input", headers=BEARER,
                        json={"action": "key", "key": "Return"})
    assert r.json().get("reason") != "control_off", r.text
    (job,) = rig.store._all_jobs()
    assert job["desk"] == mac_nodes.OWNER_DESK and job["kind"] == "input"


def test_his_input_needs_full_access_on_the_mac(rig):
    """A leaked deck token alone must not click and type on his Mac: his hands
    from the phone need the Mac in Full access (owner ruling 2026-10-01)."""
    node_id, mac = _input_mac(rig)
    rig.poll(node_id, mac, perms=PERMS, mode="ask")
    _frame(rig, node_id, mac, w=1440, h=900)
    r = rig.client.post(f"/v1/nodes/{node_id}/screen/input", headers=BEARER,
                        json={"action": "key", "key": "Return"})
    assert r.status_code == 409, r.text
    assert r.json()["reason"] == "full_access_required"
    assert r.json()["detail"] == mac_api.FULL_ACCESS_COPY
    assert "Full access" in r.json()["detail"]
    assert rig.store._all_jobs() == []


def test_watching_needs_no_full_access(rig):
    node_id, mac = _input_mac(rig)
    rig.poll(node_id, mac, perms=PERMS, mode="ask",
             screen={"width": 1512, "height": 982})
    _frame(rig, node_id, mac)
    body = rig.client.get(f"/v1/nodes/{node_id}/screen", headers=BEARER).json()
    assert body["computer"]["running"] is True and body["reason"] == ""
    r = rig.client.get(f"/v1/nodes/{node_id}/screen.jpg", headers=BEARER)
    assert r.status_code == 200


def test_his_input_on_a_mac_without_accessibility_says_so(rig):
    node_id, mac = _input_mac(rig)
    rig.poll(node_id, mac, mode="full",
             perms={"accessibility": False, "screen_recording": True})
    r = rig.client.post(f"/v1/nodes/{node_id}/screen/input", headers=BEARER,
                        json={"action": "key", "key": "Return"})
    assert r.status_code == 409 and r.json()["reason"] == "accessibility_off"
    assert r.json()["detail"].startswith("Accessibility is off")
    assert rig.store._all_jobs() == []


def test_his_tap_becomes_an_owner_input_job_in_the_frames_pixels(rig):
    node_id, mac = _input_mac(rig)
    rig.poll(node_id, mac, mode="full", control=_control(rig, "atlas"))
    _frame(rig, node_id, mac, w=1440, h=900)

    def fake_mac():
        for _ in range(100):
            jobs = rig.poll(node_id, mac, mode="full",
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
    rig.poll(node_id, mac, mode="full", control=_control(rig))
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
