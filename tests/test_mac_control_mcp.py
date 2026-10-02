"""Mac control, the desk's tools: click, move, drag, scroll, type_text, key.

Owner, 2026-10-01: "agents can click, type, press keys and scroll on his
Mac", gated behind a switch he approves per session. The defects this
closes, before any of it exists:

* A desk had no tool to put a click or a keystroke on his Mac at all.
* Without a live grant a desk must be refused with a reason it can act on,
  and the refusal must not dead-end: it files ONE card asking him (per desk,
  while it waits), not one per click.
* Coordinates are screenshot pixels. A desk that never took a screenshot
  has no pixels to speak in, so it is told to take one, and the Mac is told
  the size of the picture every coordinate is in.

Hermetic: the same rig as tests/test_mac_mcp.py (tmp store, fake Mac thread).
"""

import pytest

from server import handoff, mac_mcp, mac_nodes
from tests.test_mac_mcp import MAC, Rig, call

CONTROL_TOOLS = {"click", "move", "drag", "scroll", "type_text", "key"}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.stop()


def _grant(rig, scope="atlas", secs=1800):
    rig.store.poll(rig.node_id, 4, [], "ask", {},
                   control={"scope": scope, "until": rig.clock() + secs})


def _mac_that_controls(rig, seen, shot=(1440, 900), scope="atlas"):
    """A Mac with a live grant: screenshots answer `shot`, input succeeds."""
    def behave(job):
        seen.append(job)
        payload = ({"mime": "image/jpeg", "base64": "AAAA", "width": shot[0],
                    "height": shot[1]} if job["kind"] == "screenshot" else {})
        rig.events(job["id"], {"type": "result",
                               "data": {"state": "done", "payload": payload}})

    _grant(rig, scope)
    # the fake Mac keeps reporting its grant in every poll, as the app does
    rig.poll = lambda *a, **k: rig.store.poll(
        rig.node_id, 4, [], "ask", {},
        control={"scope": scope, "until": rig.clock() + 1800})
    rig.mac(behave)


def test_the_control_tools_exist_and_none_takes_a_desk():
    names = {t["name"] for t in mac_mcp.TOOLS}
    assert CONTROL_TOOLS <= names
    for tool in mac_mcp.TOOLS:
        if tool["name"] in CONTROL_TOOLS:
            props = tool["inputSchema"]["properties"]
            assert "desk" not in props and "mac" in props
            assert tool["inputSchema"]["additionalProperties"] is False


def test_the_new_refusals_are_in_the_closed_set():
    assert {"control_off", "owner_active", "secure_field",
            "needs_screenshot"} <= set(mac_mcp.REFUSALS)


def test_no_grant_is_refused_at_once_with_one_card_per_desk(rig):
    rig.pair()
    rig.poll()
    body, is_error, _ = call("atlas", "click", x=10, y=10)
    assert is_error and body["reason"] == "control_off"
    assert "Allow" in body["detail"] and "Do not retry" in body["detail"]
    assert rig.store._all_jobs() == []          # nothing was queued
    (card,) = handoff.load(rig.handoffs)
    assert card.agent == "atlas" and card.status == "waiting"
    assert card.id in body["detail"]
    assert rig.store.control_asks(rig.node_id) == ["atlas"]
    again, _, _ = call("atlas", "type_text", text="hi")
    assert again["reason"] == "control_off"
    assert len(handoff.load(rig.handoffs)) == 1  # same card, not a second
    call("scout", "key", keys="Return")
    assert len(handoff.load(rig.handoffs)) == 2  # one per desk


def test_a_grant_for_another_desk_does_not_cover_this_one(rig):
    rig.pair()
    _grant(rig, scope="scout")
    body, is_error, _ = call("atlas", "key", keys="Return")
    assert is_error and body["reason"] == "control_off"


def test_a_pointer_action_before_any_screenshot_asks_for_one(rig):
    rig.pair()
    seen = []
    _mac_that_controls(rig, seen)
    body, is_error, _ = call("atlas", "click", x=10, y=10)
    assert is_error and body["reason"] == "needs_screenshot"
    assert "mcp__mac__screenshot" in body["detail"]
    assert seen == []


def test_click_after_a_screenshot_carries_that_screenshots_pixel_space(rig):
    rig.pair()
    seen = []
    _mac_that_controls(rig, seen, shot=(1440, 900))
    shot, is_error, _ = call("atlas", "screenshot")
    assert not is_error and shot["width"] == 1440
    body, is_error, _ = call("atlas", "click", x=700, y=450, button="right",
                             double=True)
    assert not is_error and body["ok"] and body["mac"] == MAC
    job = seen[-1]
    assert job["kind"] == "input"
    assert job["args"] == {"action": "click", "x": 700, "y": 450,
                           "button": "right", "count": 2,
                           "space": {"width": 1440, "height": 900}}


def test_off_screenshot_coordinates_are_refused_before_queueing(rig):
    rig.pair()
    seen = []
    _mac_that_controls(rig, seen, shot=(1440, 900))
    call("atlas", "screenshot")
    body, is_error, _ = call("atlas", "click", x=1440, y=10)
    assert is_error and body["reason"] == "bad_input"
    assert [j["kind"] for j in seen] == ["screenshot"]


def test_each_tool_becomes_one_input_gesture(rig):
    rig.pair()
    seen = []
    _mac_that_controls(rig, seen, shot=(800, 600))
    call("atlas", "screenshot")
    space = {"width": 800, "height": 600}
    for name, args, want in [
        ("move", {"x": 1, "y": 2},
         {"action": "move", "x": 1, "y": 2, "space": space}),
        ("drag", {"x": 1, "y": 2, "to_x": 30, "to_y": 40},
         {"action": "drag", "x": 1, "y": 2, "to_x": 30, "to_y": 40,
          "button": "left", "space": space}),
        ("scroll", {"x": 5, "y": 5, "direction": "down", "amount": 3},
         {"action": "scroll", "x": 5, "y": 5, "dy": -3, "space": space}),
        ("scroll", {"x": 5, "y": 5, "direction": "right"},
         {"action": "scroll", "x": 5, "y": 5, "dx": 3, "space": space}),
        ("type_text", {"text": "hello from atlas"},
         {"action": "type", "text": "hello from atlas"}),
        ("key", {"keys": "cmd+s"}, {"action": "key", "key": "cmd+s"}),
    ]:
        body, is_error, _ = call("atlas", name, **args)
        assert not is_error, (name, body)
        assert seen[-1]["args"] == want, name


def test_the_macs_own_refusal_comes_back_as_its_reason(rig):
    rig.pair()
    _grant(rig)
    rig.poll = lambda *a, **k: rig.store.poll(
        rig.node_id, 4, [], "ask", {},
        control={"scope": "atlas", "until": rig.clock() + 1800})
    rig.mac(lambda job: rig.events(job["id"], {"type": "result", "data": {
        "state": "refused", "reason": "secure_field",
        "detail": "A password field has focus; Agent Deck never types there."}}))
    body, is_error, _ = call("atlas", "type_text", text="hunter2")
    assert is_error and body["reason"] == "secure_field"


def test_status_says_whether_control_is_on_for_this_desk(rig):
    rig.pair()
    _grant(rig, scope="atlas")
    (mac,) = call("atlas", "status")[0]["macs"]
    assert mac["control"]["yours"] is True and mac["control"]["until"]
    (mac,) = call("scout", "status")[0]["macs"]
    assert mac["control"]["yours"] is False


def test_the_owner_desk_name_is_not_a_desk_a_tool_can_be_bound_to():
    with pytest.raises(SystemExit):
        mac_mcp.main(["--desk", mac_nodes.OWNER_DESK])
