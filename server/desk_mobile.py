"""A desk's browser becomes a phone, for what sites only allow on mobile web.

LIVE, 2026-10-01: a growth desk: "the website link can only be edited
on mobile, and the business categories don't load on the web". Instagram
keeps the bio link, posting and the business-account switch on its mobile
site. `mobile_mode` makes the desk's Chromium a phone over CDP: an Android
Chrome user agent of the browser's real version (with matching client hints,
so `Sec-CH-UA-Mobile` agrees with the UA), a 390px touch screen, and the
mouse delivered as touch -- so the existing click tool taps.

**Why a holder process.** MEASURED on the box (Chromium 151): every override
is cleared the moment the CDP connection that set it closes, and the deck's
commands use one short connection per call. So "on" starts
`python -m server.desk_mobile --desk <d>`, detached, which opens ONE
long-lived connection, applies the phone, and holds it; "off" stops it, which
is what clears the phone, then says so explicitly and reloads. The holder
reconnects and re-applies if the relay or the browser restarts, and exits
when the desk's computer is gone. State is a small JSON file per desk under
the browser root on the box, outside the bind-mounted home the desk's
container can write.

**What the owner sees.** MEASURED: the 1280x800 screen shows the phone at the
top-left at its CSS size, and its visible viewport is 712px tall -- an 844px
phone was cut off at the bottom of his take-over view. So the phone is as
tall as the window shows (`view_height`), while `screen` says 844, as a real
phone with its browser bars would.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import browser, desk_computer, sandbox

STATE_DIR: Path = browser.DEFAULT_ROOT / "mobile"
ROOT = str(Path(__file__).resolve().parents[1])

WIDTH = 390
SCREEN_HEIGHT = 844
DEFAULT_VIEW_HEIGHT = 712   # MEASURED: innerHeight of the 1280x800 window
MIN_VIEW_HEIGHT = 480
SCALE = 3
FALLBACK_MAJOR = "151"
MODEL = "Pixel 8"
ANDROID = "14"

HOLD_SECONDS = 12 * 3600    # one relay's life; the holder reconnects after
START_WAIT = 25.0
QUIET = 20.0

_UA = ("Mozilla/5.0 (Linux; Android {android}; {model}) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/{major}.0.0.0 Mobile Safari/537.36")


def major_of(product: str) -> str:
    """"Chrome/151.0.7922.173" -> "151". PURE."""
    match = re.search(r"Chrome/(\d+)\.", str(product or ""))
    return match.group(1) if match else FALLBACK_MAJOR


def view_height(inner) -> int:
    """How tall the phone can be and still be fully on the owner's screen."""
    try:
        value = int(inner)
    except (TypeError, ValueError):
        return DEFAULT_VIEW_HEIGHT
    if value < MIN_VIEW_HEIGHT:
        return DEFAULT_VIEW_HEIGHT
    return min(value, SCREEN_HEIGHT)


def mobile_commands(major: str, height: int) -> list[tuple[str, dict]]:
    """The CDP commands that make the page a phone. PURE."""
    brands = [{"brand": "Chromium", "version": major},
              {"brand": "Google Chrome", "version": major},
              {"brand": "Not.A/Brand", "version": "99"}]
    return [
        ("Emulation.setUserAgentOverride", {
            "userAgent": _UA.format(android=ANDROID, model=MODEL, major=major),
            "acceptLanguage": "en-GB,en;q=0.9",
            "platform": "Linux armv8l",
            "userAgentMetadata": {
                "brands": brands, "fullVersionList": brands,
                "platform": "Android", "platformVersion": f"{ANDROID}.0.0",
                "architecture": "", "model": MODEL, "mobile": True},
        }),
        ("Emulation.setDeviceMetricsOverride", {
            "width": WIDTH, "height": int(height), "deviceScaleFactor": SCALE,
            "mobile": True, "screenWidth": WIDTH,
            "screenHeight": SCREEN_HEIGHT,
            "screenOrientation": {"type": "portraitPrimary", "angle": 0}}),
        ("Emulation.setTouchEmulationEnabled", {"enabled": True,
                                                "maxTouchPoints": 5}),
        ("Emulation.setEmitTouchEventsForMouse", {"enabled": True,
                                                  "configuration": "mobile"}),
    ]


def desktop_commands() -> list[tuple[str, dict]]:
    """Back to the desktop browser. PURE.

    MEASURED on Chromium 151: when the holder's connection closes the user
    agent reverts but the 390px viewport STAYS (innerWidth 390 on a desktop
    page), and a plain clear from another connection does not undo it. An
    explicit all-zero override ("use the window") and then a clear does."""
    return [
        ("Emulation.setDeviceMetricsOverride", {
            "width": 0, "height": 0, "deviceScaleFactor": 0,
            "mobile": False}),
        ("Emulation.clearDeviceMetricsOverride", {}),
        ("Emulation.setTouchEmulationEnabled", {"enabled": False}),
        ("Emulation.setEmitTouchEventsForMouse", {"enabled": False}),
    ]


def apply(sess, *, reload: bool) -> str:
    """Make `sess`'s page a phone. Returns e.g. "Android Chrome 151, 390x712"."""
    major = major_of(sess.call("Browser.getVersion").get("product", ""))
    # A phone left over from an earlier holder would make innerHeight the
    # phone's (MEASURED: 844), so the window is put back before it is read.
    for method, params in desktop_commands()[:2]:
        sess.call(method, params)
    inner = (sess.call("Runtime.evaluate", {
        "expression": "window.innerHeight", "returnByValue": True})
        .get("result") or {}).get("value")
    height = view_height(inner)
    for method, params in mobile_commands(major, height):
        sess.call(method, params)
    if reload:
        sess.call("Page.reload", {})
    return f"Android Chrome {major} ({MODEL}), {WIDTH}x{height} touch screen"


# ── state ───────────────────────────────────────────────────────────────────


def _state_path(desk: str) -> Path:
    return Path(STATE_DIR) / f"{browser._check_desk(desk)}.json"


def _read(desk: str) -> dict:
    try:
        value = json.loads(_state_path(desk).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(desk: str, state: dict) -> None:
    path = _state_path(desk)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _kill(pid) -> None:
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, TypeError, ValueError):
        pass


def _ensure(desk: str):
    return desk_computer.ensure(desk)


def _spawn(argv: list[str]) -> int:
    log = Path(STATE_DIR) / "holder.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=out, start_new_session=True,
                                env={**os.environ, "PYTHONPATH": ROOT},
                                cwd=ROOT)
    return proc.pid


def is_on(desk: str) -> bool:
    state = _read(desk)
    return state.get("state") == "on" and _alive(state.get("pid"))


# ── the tool ────────────────────────────────────────────────────────────────


def mobile_mode(desk: str, on: bool) -> str:
    desk = browser._check_desk(desk)
    state = _read(desk)
    holder = state.get("pid") if _alive(state.get("pid")) else None
    if on:
        if holder and state.get("state") == "on":
            return f"mobile mode is already on ({state.get('detail', '')})."
        if holder:
            _kill(holder)
        _ensure(desk)  # the computer and its browser are up before the holder
        _write(desk, {"state": "starting"})
        pid = _spawn([sys.executable, "-m", "server.desk_mobile",
                      "--desk", desk])
        deadline = time.monotonic() + START_WAIT
        while time.monotonic() < deadline:
            now = _read(desk)
            if now.get("state") == "on":
                return (f"mobile mode ON: your browser is now a phone -- "
                        f"{now.get('detail', '')}; the page was reloaded as "
                        f"mobile. Click taps; use upload_file for photos and "
                        f"videos. It stays on until mobile_mode off or your "
                        f"session's computer restarts.")
            if now.get("state") == "failed":
                raise browser.BrowserError(
                    "mobile_mode_failed", str(now.get("detail") or "failed"))
            time.sleep(0.25)
        _kill(pid)
        raise browser.BrowserError("mobile_mode_failed",
                                   "the phone did not come up in time")
    if holder:
        _kill(holder)
    _state_path(desk).unlink(missing_ok=True)
    try:
        _ensure(desk).run_commands(desktop_commands() + [("Page.reload", {})])
    except (browser.BrowserError, sandbox.SandboxError, OSError):
        pass  # the holder is gone, and with it the phone
    return "mobile mode off: your browser is a desktop browser again."


# ── the holder process ─────────────────────────────────────────────────────


def _mine(desk: str) -> bool:
    return _read(desk).get("pid") == os.getpid()


def hold(desk: str) -> int:
    """Keep one CDP connection open with the phone applied, until told off."""
    state = _read(desk)
    if state.get("state") != "starting":
        return 0
    _write(desk, {"pid": os.getpid(), "state": "starting"})
    first, failures = True, 0
    while _mine(desk):
        try:
            driver = desk_computer.ContainerCDP(desk)
            driver.relay_seconds = HOLD_SECONDS
            with driver.session() as sess:
                detail = apply(sess, reload=first)
                first, failures = False, 0
                _write(desk, {"pid": os.getpid(), "state": "on",
                              "detail": detail})
                while _mine(desk):
                    sess.wait_event("Inspector.detached", QUIET)
                    sess.events.clear()
        except Exception as exc:  # noqa: BLE001 - report, retry, or give up
            failures += 1
            if first or failures > 5 or not sandbox.is_up(desk):
                if _mine(desk):
                    _write(desk, {"pid": os.getpid(), "state": "failed",
                                  "detail": browser.safe(str(exc))[:300]})
                return 1
            time.sleep(2.0)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="desk_mobile")
    parser.add_argument("--desk", required=True)
    return hold(browser._check_desk(parser.parse_args(argv).desk))


if __name__ == "__main__":
    sys.exit(main())
