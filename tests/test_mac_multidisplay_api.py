"""His Mac has two displays: the live view and his hands reach both.

MEASURED 2026-10-02 on his MacBook Pro: Built-in Retina (id 1, 0,0, 1512x982
points, scale 2) plus a PM1561P (id 3, at 1512,0, 1920x1080, scale 1). The
node reported one `screen`, the viewer could only ever be shown the main
display, and every tap was mapped onto it -- so a window on the second
monitor could be neither seen nor clicked from the phone.

* The poll reports every display; `screen` stays the main one (old viewers).
* `screen`, `screen.jpg` and `screen/input` take `?display=<id>` (default
  main). The watch says which display to stream; frames are kept per display.
* His tap on display 3 is in display 3's picture, and the job says so.
* A display that is unplugged falls back to the main one, with a note.

Real HTTP through FastAPI's TestClient, same rig as tests/test_mac_api.py.
"""

from __future__ import annotations

import threading
import time

import pytest

from server import mac_api
from tests.test_mac_api import BEARER, TOKEN, Rig
from tests.test_mac_control_api import JPEG, PERMS, _input_mac

BUILT_IN = {"id": 1, "name": "Built-in Retina Display", "width_px": 3024,
            "height_px": 1964, "scale": 2.0, "origin_x": 0.0, "origin_y": 0.0,
            "is_main": True}
MONITOR = {"id": 3, "name": "PM1561P", "width_px": 1920, "height_px": 1080,
           "scale": 1.0, "origin_x": 1512.0, "origin_y": 0.0, "is_main": False}
JPEG3 = b"\xff\xd8\xff\xe0" + b"3" * 64 + b"\xff\xd9"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DECK_TOKEN", TOKEN)
    return Rig(tmp_path)


def _two(rig, **kw):
    node_id, mac = _input_mac(rig)
    r = rig.poll(node_id, mac, perms=PERMS, mode="full",
                 screen={"width": 1512, "height": 982},
                 displays=[MONITOR, BUILT_IN], **kw)
    assert r.status_code == 200, r.text
    return node_id, mac


def _frame(rig, node_id, mac, data=JPEG, w=1512, h=982, display=None):
    headers = {**mac, "Content-Type": "image/jpeg", "X-Frame-Width": str(w),
               "X-Frame-Height": str(h)}
    if display is not None:
        headers["X-Frame-Display-Id"] = str(display)
    return rig.client.post(f"/v1/nodes/{node_id}/screen/frame", content=data,
                           headers=headers)


def _status(rig, node_id, display=None):
    q = "" if display is None else f"?display={display}"
    return rig.client.get(f"/v1/nodes/{node_id}/screen{q}", headers=BEARER)


def test_the_node_reports_every_display_and_screen_stays_main(rig):
    node_id, _ = _two(rig)
    node = rig.store.node(node_id)
    assert node["screen"] == {"width": 1512, "height": 982}
    assert [d["id"] for d in node["displays"]] == [3, 1]
    (row,) = rig.client.get("/v1/nodes", headers=BEARER).json()["nodes"]
    assert [d["id"] for d in row["displays"]] == [1, 3]   # main first


def test_the_status_lists_the_displays_main_first_with_their_labels(rig):
    node_id, _ = _two(rig)
    body = _status(rig, node_id).json()
    assert body["display_id"] == 1 and body["display_note"] == ""
    assert [(d["id"], d["label"]) for d in body["displays"]] == [
        (1, "Display 1 · Built-in"), (3, "Display 2 · PM1561P")]
    assert (body["width"], body["height"]) == (1512, 982)


def test_the_second_displays_status_is_its_own_size(rig):
    node_id, mac = _two(rig)
    body = _status(rig, node_id, 3).json()
    assert body["display_id"] == 3
    # Before its first frame: its points, capped as the Mac caps them.
    assert (body["width"], body["height"]) == (1600, 900)
    _frame(rig, node_id, mac, JPEG3, 1600, 900, display=3)
    body = _status(rig, node_id, 3).json()
    assert (body["width"], body["height"]) == (1600, 900)


def test_watching_display_3_tells_the_mac_to_stream_display_3(rig):
    node_id, mac = _two(rig)
    threading.Timer(0.4, lambda: rig.client.get(
        f"/v1/nodes/{node_id}/screen.jpg?display=3", headers=BEARER)).start()
    started = time.monotonic()
    r = rig.poll(node_id, mac, wait=5, mode="full", displays=[MONITOR, BUILT_IN])
    assert time.monotonic() - started < 2.5
    assert r.json()["watch"] is True and r.json()["watch_displays"] == [3]
    up = _frame(rig, node_id, mac, JPEG3, 1600, 900, display=3).json()
    assert up["watch"] is True and up["watch_displays"] == [3]


def test_each_display_serves_its_own_frame(rig):
    node_id, mac = _two(rig)
    _frame(rig, node_id, mac, JPEG, display=1)
    _frame(rig, node_id, mac, JPEG3, 1600, 900, display=3)
    main = rig.client.get(f"/v1/nodes/{node_id}/screen.jpg", headers=BEARER)
    second = rig.client.get(f"/v1/nodes/{node_id}/screen.jpg?display=3",
                            headers=BEARER)
    assert main.content == JPEG and main.headers["X-Frame-Display-Id"] == "1"
    assert second.content == JPEG3
    assert second.headers["X-Frame-Display-Id"] == "3"


def test_an_old_macs_frame_without_a_display_is_the_main_display(rig):
    node_id, mac = _two(rig)
    _frame(rig, node_id, mac, JPEG)
    r = rig.client.get(f"/v1/nodes/{node_id}/screen.jpg?display=1",
                       headers=BEARER)
    assert r.status_code == 200 and r.content == JPEG


def test_an_unplugged_display_falls_back_to_main_with_a_note(rig):
    node_id, mac = _two(rig)
    rig.poll(node_id, mac, mode="full", perms=PERMS, displays=[BUILT_IN])
    body = _status(rig, node_id, 3).json()
    assert body["display_id"] == 1
    assert body["display_note"] == ("Display 3 is no longer connected. "
                                    "Showing Display 1 · Built-in.")
    _frame(rig, node_id, mac, JPEG, display=1)
    r = rig.client.get(f"/v1/nodes/{node_id}/screen.jpg?display=3",
                       headers=BEARER)
    assert r.status_code == 200 and r.headers["X-Frame-Display-Id"] == "1"


def test_his_tap_on_display_3_is_in_display_3s_picture(rig, monkeypatch):
    monkeypatch.setattr(mac_api, "INPUT_WAIT", 1.0)
    node_id, mac = _two(rig)
    _frame(rig, node_id, mac, JPEG, display=1)
    _frame(rig, node_id, mac, JPEG3, 1600, 900, display=3)
    rig.client.post(f"/v1/nodes/{node_id}/screen/input?display=3",
                    headers=BEARER, json={"action": "move", "x": 1500, "y": 800})
    (job,) = rig.store._all_jobs()
    assert job["args"]["space"] == {"width": 1600, "height": 900, "display": 3}


def test_his_tap_with_no_display_is_on_main(rig, monkeypatch):
    monkeypatch.setattr(mac_api, "INPUT_WAIT", 1.0)
    node_id, mac = _two(rig)
    _frame(rig, node_id, mac, JPEG, display=1)
    _frame(rig, node_id, mac, JPEG3, 1600, 900, display=3)
    rig.client.post(f"/v1/nodes/{node_id}/screen/input", headers=BEARER,
                    json={"action": "move", "x": 10, "y": 10})
    (job,) = rig.store._all_jobs()
    assert job["args"]["space"] == {"width": 1512, "height": 982, "display": 1}
