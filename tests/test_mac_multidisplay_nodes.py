"""The Mac store knows every display of his Mac, not just the main one.

MEASURED 2026-10-02 on his MacBook Pro: Built-in Retina (id 1, at 0,0,
3024x1964 px, scale 2) plus a PM1561P (id 3, at 1512,0, 1920x1080, scale 1).
The node kept one `screen` size, so nothing downstream could name, watch or
click the second display.

* A poll's `displays` report is checked and kept; `screen` stays the main one.
* Displays are numbered main first, then left to right: "Display 2 · PM1561P".
* A viewer's watch names its display, so the Mac streams that one.
* A gesture's `space` may name its display; a screenshot may ask for one.
"""

from __future__ import annotations

import pytest

from server import mac_nodes

BUILT_IN = {"id": 1, "name": "Built-in Retina Display", "width_px": 3024,
            "height_px": 1964, "scale": 2.0, "origin_x": 0.0, "origin_y": 0.0,
            "is_main": True}
MONITOR = {"id": 3, "name": "PM1561P", "width_px": 1920, "height_px": 1080,
           "scale": 1.0, "origin_x": 1512.0, "origin_y": 0.0, "is_main": False}
LEFT = {**MONITOR, "id": 4, "name": "LG HDR 4K", "origin_x": -1920.0}


@pytest.fixture
def store(tmp_path):
    s = mac_nodes.Store(tmp_path / "mac")
    node = s.register("m-1", "MacBook Pro", "macOS 26", "2.0",
                      list(mac_nodes.CAPABILITIES), "full", None)
    s.node_id = node["node_id"]
    return s


def test_the_poll_keeps_every_display_and_screen_stays_main(store):
    store.poll(store.node_id, 4, [], "full", {},
               screen={"width": 1512, "height": 982}, displays=[MONITOR, BUILT_IN])
    node = store.node(store.node_id)
    assert node["screen"] == {"width": 1512, "height": 982}
    assert [d["id"] for d in node["displays"]] == [3, 1]
    (row,) = store.listing()
    assert [d["id"] for d in row["displays"]] == [1, 3]   # main first


@pytest.mark.parametrize("bad", [
    "nope", [{"id": "x"}], [{**MONITOR, "scale": 0}],
    [{**MONITOR, "width_px": -1}], [{**MONITOR, "origin_x": "left"}],
    [MONITOR] * 17])
def test_a_malformed_display_report_is_refused(store, bad):
    with pytest.raises(mac_nodes.MacError) as err:
        store.poll(store.node_id, 4, [], "full", {}, displays=bad)
    assert err.value.reason == "bad_input"


def test_displays_are_numbered_main_first_then_left_to_right(store):
    store.poll(store.node_id, 4, [], "full", {},
               displays=[MONITOR, LEFT, BUILT_IN])
    node = store.node(store.node_id)
    assert [mac_nodes.display_label(d, node)
            for d in mac_nodes.ordered_displays(node)] == [
        "Display 1 · Built-in", "Display 2 · LG HDR 4K",
        "Display 3 · PM1561P"]
    assert mac_nodes.main_display(node)["id"] == 1


def test_a_watch_names_its_display(store):
    store.poll(store.node_id, 4, [], "full", {}, displays=[BUILT_IN, MONITOR])
    store.watch(store.node_id, 3)
    node = store.node(store.node_id)
    now = store.now()
    assert mac_nodes.watching(node, now)
    assert mac_nodes.watched_displays(node, now) == [3]
    store.watch(store.node_id, 1)
    assert mac_nodes.watched_displays(store.node(store.node_id), now) == [1, 3]
    later = now + mac_nodes.WATCH_TTL + 1
    assert mac_nodes.watched_displays(store.node(store.node_id), later) == []


def test_a_gesture_space_and_a_screenshot_may_name_a_display():
    args = mac_nodes.validate_args("input", {
        "action": "move", "x": 1, "y": 1,
        "space": {"width": 10, "height": 10, "display": 3}})
    assert args["space"] == {"width": 10, "height": 10, "display": 3}
    with pytest.raises(mac_nodes.MacError):
        mac_nodes.validate_args("input", {
            "action": "move", "x": 1, "y": 1,
            "space": {"width": 10, "height": 10, "display": "3"}})
    assert mac_nodes.validate_args("screenshot", {"display": 3}) == {
        "display": 3}
    assert mac_nodes.validate_args("screenshot", {}) == {}
    with pytest.raises(mac_nodes.MacError):
        mac_nodes.validate_args("screenshot", {"display": -1})
