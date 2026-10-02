"""The desk's Mac tools see and reach every display, not just the main one.

MEASURED 2026-10-02: his MacBook Pro has a second monitor (PM1561P, id 3, to
the right of the built-in panel). `mcp__mac__screenshot` ran a bare
`screencapture` -- the main display only -- and every click was mapped onto
the main display, so an agent could neither see nor click anything on the
second one, and had no way to know it was there.

* `list_displays` names every display (id, label, size, which is main).
* `screenshot(display=3)` asks the Mac for display 3; its result says which
  display it is and lists the others.
* A click after that screenshot is in display 3's pixels, and the gesture
  says so. `display=` on a click picks that display's latest screenshot.

Hermetic: the same rig as tests/test_mac_mcp.py.
"""

import pytest

from server import mac_mcp, mac_nodes
from tests.test_mac_control_mcp import _grant
from tests.test_mac_mcp import Rig, call

BUILT_IN = {"id": 1, "name": "Built-in Retina Display", "width_px": 3024,
            "height_px": 1964, "scale": 2.0, "origin_x": 0.0, "origin_y": 0.0,
            "is_main": True}
MONITOR = {"id": 3, "name": "PM1561P", "width_px": 1920, "height_px": 1080,
           "scale": 1.0, "origin_x": 1512.0, "origin_y": 0.0, "is_main": False}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.stop()


def _two_display_mac(rig, seen):
    """A Mac with two displays and a live grant; a screenshot of display N
    answers that display's own pixels and names it."""
    sizes = {1: (3024, 1964), 3: (1920, 1080)}

    def behave(job):
        seen.append(job)
        payload = {}
        if job["kind"] == "screenshot":
            d = job["args"].get("display", 1)
            payload = {"mime": "image/jpeg", "base64": "AAAA",
                       "width": sizes[d][0], "height": sizes[d][1],
                       "display": d}
        rig.events(job["id"], {"type": "result",
                               "data": {"state": "done", "payload": payload}})

    def poll(*a, **k):
        return rig.store.poll(rig.node_id, 4, [], "ask", {},
                              control={"scope": "atlas",
                                       "until": rig.clock() + 1800},
                              displays=[BUILT_IN, MONITOR])
    _grant(rig)
    rig.poll = poll
    poll()
    rig.mac(behave)


def test_list_displays_names_every_display_main_first(rig):
    rig.pair()
    _two_display_mac(rig, [])
    body, is_error, _ = call("atlas", "list_displays")
    assert not is_error, body
    assert [(d["id"], d["label"], d["is_main"]) for d in body["displays"]] == [
        (1, "Display 1 · Built-in", True), (3, "Display 2 · PM1561P", False)]
    assert body["displays"][1]["width_px"] == 1920


def test_a_screenshot_of_the_second_display_asks_for_it_and_says_so(rig):
    rig.pair()
    seen = []
    _two_display_mac(rig, seen)
    body, is_error, _ = call("atlas", "screenshot", display=3)
    assert not is_error, body
    assert seen[-1]["args"] == {"display": 3}
    assert (body["width"], body["height"], body["display"]) == (1920, 1080, 3)
    assert [d["id"] for d in body["displays"]] == [1, 3]


def test_a_click_after_that_screenshot_lands_on_that_display(rig):
    rig.pair()
    seen = []
    _two_display_mac(rig, seen)
    call("atlas", "screenshot", display=3)
    body, is_error, _ = call("atlas", "click", x=1900, y=1000)
    assert not is_error, body
    assert seen[-1]["args"]["space"] == {"width": 1920, "height": 1080,
                                         "display": 3}


def test_display_on_a_click_uses_that_displays_latest_screenshot(rig):
    rig.pair()
    seen = []
    _two_display_mac(rig, seen)
    call("atlas", "screenshot", display=3)
    call("atlas", "screenshot")
    body, is_error, _ = call("atlas", "move", x=1900, y=1000, display=3)
    assert not is_error, body
    assert seen[-1]["args"]["space"]["display"] == 3
    body, is_error, _ = call("atlas", "move", x=3000, y=1900)
    assert not is_error, body
    assert seen[-1]["args"]["space"] == {"width": 3024, "height": 1964,
                                         "display": 1}


def test_a_display_with_no_screenshot_yet_asks_for_one(rig):
    rig.pair()
    seen = []
    _two_display_mac(rig, seen)
    call("atlas", "screenshot")
    body, is_error, _ = call("atlas", "click", x=1, y=1, display=3)
    assert is_error and body["reason"] == "needs_screenshot"
    assert "display=3" in body["detail"]


def test_the_tools_take_display_and_the_refusal_is_in_the_closed_set():
    tools = {t["name"]: t for t in mac_mcp.TOOLS}
    assert "list_displays" in tools
    for name in ("screenshot", "click", "move", "drag", "scroll"):
        assert "display" in tools[name]["inputSchema"]["properties"], name
    assert "no_such_display" in mac_mcp.REFUSALS
    assert mac_nodes.validate_args("screenshot", {"display": 3}) == {
        "display": 3}
