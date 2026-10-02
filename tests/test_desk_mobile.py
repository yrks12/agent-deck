"""A desk's browser can be a phone, for what sites only allow on mobile web.

LIVE, 2026-10-01: desk a growth desk: "the website link can only be edited
on mobile, and the business categories don't load on the web". Instagram
gates the bio link, posting and the business-account switch behind its mobile
site. `mobile_mode` emulates a phone over CDP: an Android Chrome user agent
(with matching client hints), a 390px touch screen, mouse-as-touch.

MEASURED on the box (Chromium 151, 1280x800 Xvfb): the overrides are cleared
the moment the CDP connection that set them closes, and the deck's commands
use one short connection per call -- so mobile mode is a small holder process
that keeps one connection open. The visible viewport is 712px tall; an 844px
phone was clipped at the bottom of the owner's take-over view, so the phone
is made as tall as the window shows.

Hermetic: no Docker, no Chromium.
"""

import json
import os
import sys

import pytest

from server import computer_mcp, desk_mobile

DESK = "acme"


def _by_method(commands):
    return {method: params for method, params in commands}


def test_the_phone_is_an_android_chrome_of_the_real_version():
    cmds = _by_method(desk_mobile.mobile_commands("151", 712))
    ua = cmds["Emulation.setUserAgentOverride"]
    assert "Android" in ua["userAgent"] and "Mobile" in ua["userAgent"]
    assert "Chrome/151." in ua["userAgent"]
    meta = ua["userAgentMetadata"]
    assert meta["mobile"] is True and meta["platform"] == "Android"
    assert any(b["version"] == "151" for b in meta["brands"])


def test_the_phone_screen_is_390_wide_mobile_and_fits_the_window():
    metrics = _by_method(desk_mobile.mobile_commands("151", 712))[
        "Emulation.setDeviceMetricsOverride"]
    assert metrics["width"] == 390 and metrics["mobile"] is True
    assert metrics["height"] == 712
    assert metrics["screenWidth"] == 390 and metrics["screenHeight"] == 844
    assert metrics["deviceScaleFactor"] >= 2


def test_it_is_a_touch_screen_and_the_mouse_taps():
    cmds = _by_method(desk_mobile.mobile_commands("151", 712))
    assert cmds["Emulation.setTouchEmulationEnabled"]["enabled"] is True
    assert cmds["Emulation.setTouchEmulationEnabled"]["maxTouchPoints"] >= 1
    assert cmds["Emulation.setEmitTouchEventsForMouse"]["enabled"] is True


@pytest.mark.parametrize("inner,want", [(712, 712), (900, 844), (0, 712),
                                        (None, 712), (300, 712)])
def test_the_view_height_fits_what_the_window_shows(inner, want):
    assert desk_mobile.view_height(inner) == want


def test_off_clears_every_override():
    methods = [m for m, _ in desk_mobile.desktop_commands()]
    assert "Emulation.clearDeviceMetricsOverride" in methods
    # MEASURED: a closed connection leaves the 390px viewport behind, and a
    # bare clear does not remove it; an all-zero override first does.
    first = desk_mobile.desktop_commands()[0]
    assert first == ("Emulation.setDeviceMetricsOverride",
                     {"width": 0, "height": 0, "deviceScaleFactor": 0,
                      "mobile": False})
    touch = _by_method(desk_mobile.desktop_commands())[
        "Emulation.setTouchEmulationEnabled"]
    assert touch["enabled"] is False


def test_major_version_is_read_from_the_browser_banner():
    assert desk_mobile.major_of("Chrome/151.0.7922.173") == "151"
    assert desk_mobile.major_of("HeadlessChrome/140.0.1.2") == "140"
    assert desk_mobile.major_of("") == desk_mobile.FALLBACK_MAJOR


# ── on / off: a holder process keeps the connection that keeps the phone ───


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(desk_mobile, "STATE_DIR", tmp_path)
    monkeypatch.setattr(desk_mobile, "_ensure", lambda desk: None)
    return tmp_path


def test_on_starts_one_holder_for_its_own_desk_and_waits_for_it(state,
                                                                monkeypatch):
    spawned = []

    def spawn(argv):
        spawned.append(argv)
        desk_mobile._write(DESK, {"pid": os.getpid(), "state": "on",
                                  "detail": "390x712"})
        return os.getpid()

    monkeypatch.setattr(desk_mobile, "_spawn", spawn)
    text = desk_mobile.mobile_mode(DESK, True)
    assert spawned == [[sys.executable, "-m", "server.desk_mobile",
                        "--desk", DESK]]
    assert "on" in text.lower()
    # Already on: no second holder.
    desk_mobile.mobile_mode(DESK, True)
    assert len(spawned) == 1


def test_a_holder_that_fails_is_reported_not_claimed(state, monkeypatch):
    def spawn(argv):
        desk_mobile._write(DESK, {"pid": 999999, "state": "failed",
                                  "detail": "no page"})
        return 999999

    monkeypatch.setattr(desk_mobile, "_spawn", spawn)
    with pytest.raises(Exception) as err:
        desk_mobile.mobile_mode(DESK, True)
    assert "no page" in str(err.value)


def test_off_stops_the_holder_and_puts_the_desktop_back(state, monkeypatch):
    killed, sent = [], []
    desk_mobile._write(DESK, {"pid": 4242, "state": "on"})
    monkeypatch.setattr(desk_mobile, "_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(desk_mobile, "_kill", lambda pid: killed.append(pid))

    class Driver:
        def run_commands(self, commands):
            sent.extend(commands)
            return [{} for _ in commands]

    monkeypatch.setattr(desk_mobile, "_ensure", lambda desk: Driver())
    text = desk_mobile.mobile_mode(DESK, False)
    assert killed == [4242]
    methods = [m for m, _ in sent]
    assert "Emulation.clearDeviceMetricsOverride" in methods
    assert methods[-1] == "Page.reload"
    assert not desk_mobile._state_path(DESK).exists()
    assert "off" in text.lower()


def test_apply_sends_the_phone_then_reloads_once():
    calls = []

    class Session:
        def call(self, method, params=None):
            calls.append((method, params))
            if method == "Browser.getVersion":
                return {"product": "Chrome/151.0.7922.173"}
            if method == "Runtime.evaluate":
                return {"result": {"value": 712}}
            return {}

    detail = desk_mobile.apply(Session(), reload=True)
    methods = [m for m, _ in calls]
    # The window is reset BEFORE its height is read (a leftover phone said 844).
    assert methods.index("Emulation.clearDeviceMetricsOverride") < \
        methods.index("Runtime.evaluate")
    assert "Emulation.setUserAgentOverride" in methods
    assert methods[-1] == "Page.reload"
    assert "390x712" in detail
    calls.clear()
    desk_mobile.apply(Session(), reload=False)
    assert "Page.reload" not in [m for m, _ in calls]


def test_mobile_mode_is_a_tool_on_the_bound_desk(monkeypatch):
    seen = []
    monkeypatch.setattr(desk_mobile, "mobile_mode",
                        lambda desk, on: seen.append((desk, on)) or "ok")
    tools = computer_mcp.handle(DESK, {"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/list"})["result"]
    tool = next(t for t in tools["tools"] if t["name"] == "mobile_mode")
    assert tool["inputSchema"]["required"] == ["on"]
    reply = computer_mcp.handle(DESK, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "mobile_mode", "arguments": {"on": True}}})
    assert reply["result"]["isError"] is False
    assert seen == [(DESK, True)]
    assert "desk" not in json.dumps(tool["inputSchema"]).lower()
