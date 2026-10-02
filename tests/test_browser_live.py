"""The live half: a real Xvfb, a real Chrome, a real DevTools connection.

**Excluded from the default suite** (`-m live`, pytest.ini `addopts`). Nothing
in the ordinary run starts an X server, a browser or ffmpeg, or opens a socket.

Run on the Linux box, from the isolated venv described in docs/the-browser.md:

    /opt/deck-browser/venv/bin/python -m pytest -m live tests/test_browser_live.py

These are the tests that cannot be written purely, and each one covers a claim
the pure tests can only assert about an argv rather than about reality:

  * Chrome actually comes up under Xvfb and answers CDP on the loopback port;
  * the port is bound to loopback and **not** reachable on the box's LAN
    address -- the argv test proves we asked, this proves Chrome listened;
  * a cookie set in one run is still there in the next, which is the entire
    reason a human's sign-in is worth anything to the agent afterwards;
  * `stop()` really does leave no Chrome and no Xvfb behind.

`CDPDriver` needs a WebSocket client, which the stdlib does not have. The deck
itself must stay stdlib-only, so the dependency lives in that separate venv and
`CDPDriver` imports it lazily -- importing `server.browser_cdp` on the Mac, with
nothing installed, must still work. That last claim is the one thing here that
is *not* live-marked, because it is exactly what protects the default suite.
"""

import shutil
import socket
import time

import pytest

from server import browser as B

pytestmark = pytest.mark.live


needs_x = pytest.mark.skipif(
    shutil.which("Xvfb") is None or shutil.which(B.CHROME_BINARY) is None,
    reason="needs Xvfb and Chrome on PATH",
)


# ── the one test here that is NOT live: the import must never break ────────


@pytest.mark.filterwarnings("ignore")
def test_importing_the_cdp_driver_does_not_need_the_websocket_package():
    """Runs everywhere, including the Mac with nothing installed.

    If this module ever imports `websockets` at the top level, the default
    suite starts failing on a machine that has no browser stack at all -- and
    the failure looks like "the deck is broken", not "an optional driver is
    missing".
    """
    from server import browser_cdp

    assert hasattr(browser_cdp, "CDPDriver")
    assert browser_cdp.DRIVER_PACKAGE == "websockets"


# ── the live path ─────────────────────────────────────────────────────────


@pytest.fixture
def live_session(tmp_path):
    sess = B.start("livetest", root=tmp_path / "browser", url="about:blank")
    try:
        yield sess
    finally:
        B.stop(sess)


@needs_x
def test_chrome_comes_up_and_answers_cdp(live_session):
    """The good signal: a real target list from a real browser."""
    from server import browser_cdp

    driver = browser_cdp.CDPDriver(live_session)
    for _ in range(50):
        targets = driver.targets()
        if targets:
            break
        time.sleep(0.2)
    assert targets, "Chrome never published a CDP target"
    assert any(t.get("type") == "page" for t in targets)


@needs_x
def test_the_devtools_port_is_not_reachable_off_loopback(live_session):
    """The argv test proves we asked for a loopback bind. This proves Chrome
    honoured it: the same port, on this box's routable address, refuses."""
    lan = socket.gethostbyname(socket.gethostname())
    if lan.startswith("127."):
        pytest.skip("no non-loopback address on this box")

    with socket.socket() as s:
        s.settimeout(2.0)
        with pytest.raises((ConnectionRefusedError, socket.timeout, OSError)):
            s.connect((lan, live_session.cdp_port))


@needs_x
def test_a_cookie_survives_into_the_next_run(tmp_path):
    """The whole point of the design. Sam signs in by hand once; the agent
    finds itself already signed in on every run afterwards, because it is the
    same profile directory and the same Chrome."""
    from server import browser_cdp

    root = tmp_path / "browser"
    first = B.start("cookietest", root=root, url="https://example.com/")
    try:
        driver = browser_cdp.CDPDriver(first)
        driver.wait_for_page(timeout=15)
        driver.set_cookie(name="deck_probe", value="kept",
                          domain="example.com", path="/")
    finally:
        B.stop(first)

    second = B.start("cookietest", root=root, url="https://example.com/")
    try:
        driver = browser_cdp.CDPDriver(second)
        driver.wait_for_page(timeout=15)
        names = {c["name"] for c in driver.cookies()}
        assert "deck_probe" in names
    finally:
        B.stop(second)


@needs_x
def test_needs_human_fires_on_a_real_password_form(live_session):
    """End to end on a real DOM: the page state a real Chrome reports about a
    real sign-in form is the shape `needs_human` was written against."""
    from server import browser_cdp
    from server import browser_takeover as T

    driver = browser_cdp.CDPDriver(live_session)
    driver.wait_for_page(timeout=15)
    driver.goto("data:text/html,"
                "<form><input type=email><input type=password></form>")
    time.sleep(1.0)
    assert T.needs_human(driver.page_state()) == "login"


@needs_x
def test_stop_leaves_no_chrome_and_no_xvfb_behind(tmp_path):
    """A leaked Xvfb holds its display lock forever, and a leaked Chrome holds
    the profile. Both turn "restart the desk" into "reboot the box"."""
    sess = B.start("leaktest", root=tmp_path / "browser")
    assert B.is_running(sess) is True

    B.stop(sess)
    time.sleep(1.0)

    assert B.is_running(sess) is False
    assert not B.display_lock(tmp_path / "browser", sess.display).exists()
