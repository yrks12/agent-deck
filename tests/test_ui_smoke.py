"""Does the page actually render, and is it actually styled?

This exists because the manager page shipped once looking like raw text: the
stylesheet was served correctly and every unit test passed, and the page was
still unusable. Nothing below asserts on source -- it drives a real browser
against the running daemon.

Opt in with `-m ui`. Skips if the daemon is not up.
"""

import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.ui

DECK = "http://127.0.0.1:7788"


def daemon_up() -> bool:
    try:
        with urllib.request.urlopen(f"{DECK}/api/state", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(scope="module")
def page():
    if not daemon_up():
        pytest.skip("agent deck daemon is not running")
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1500, "height": 900})
        errors: list[str] = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.errors = errors
        yield pg
        browser.close()


def test_manager_page_renders_without_console_errors(page):
    page.goto(f"{DECK}/manager", wait_until="networkidle")
    page.wait_for_timeout(2500)
    assert page.errors == [], f"console errors on /manager: {page.errors}"


def test_the_stylesheet_actually_applied(page):
    """A served-but-uncached stylesheet is the failure this catches.

    If style.css does not reach the page, every element falls back to browser
    defaults -- transparent backgrounds and no accent border -- which is exactly
    what 'nothing clear on screen' looked like.
    """
    page.goto(f"{DECK}/manager", wait_until="networkidle")
    page.wait_for_timeout(1500)
    body_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert body_bg not in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"), (
        f"body is unstyled ({body_bg}) -- the stylesheet did not apply"
    )
    font = page.evaluate("getComputedStyle(document.body).fontFamily")
    assert "SF Mono" in font or "Menlo" in font, f"deck font missing: {font}"


def test_the_office_canvas_is_drawn(page):
    page.goto(f"{DECK}/manager", wait_until="networkidle")
    page.wait_for_timeout(2500)
    size = page.evaluate("""() => {
      const c = document.getElementById('office-canvas');
      return c ? {w: c.width, h: c.height} : null;
    }""")
    assert size and size["w"] > 200 and size["h"] > 80, f"canvas not sized: {size}"
    # A blank canvas is the same shape as a drawn one, so check it has ink.
    painted = page.evaluate("""() => {
      const c = document.getElementById('office-canvas');
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      let lit = 0;
      for (let i = 0; i < d.length; i += 4) if (d[i] + d[i+1] + d[i+2] > 90) lit++;
      return lit;
    }""")
    assert painted > 500, f"office canvas looks empty ({painted} lit pixels)"


def test_clicking_a_desk_filters_the_thread(page):
    page.goto(f"{DECK}/manager", wait_until="networkidle")
    page.wait_for_timeout(2500)
    if "no manager" in (page.text_content("#crown") or ""):
        pytest.skip("no manager crowned on this machine")
    before = page.text_content("#thread-head")
    box = page.query_selector("#office-canvas").bounding_box()

    # Desks are centred and their spacing depends on how many are seated, so
    # sweep the room rather than pinning a fraction that a departing session
    # would invalidate. If desk clicking is broken, no position works.
    after = before
    for fraction in (0.08, 0.2, 0.32, 0.44, 0.56, 0.68, 0.8, 0.92):
        page.mouse.click(box["x"] + box["width"] * fraction, box["y"] + box["height"] * 0.7)
        page.wait_for_timeout(400)
        after = page.text_content("#thread-head")
        if after != before:
            break

    assert after != before, "no desk in the room filtered the thread"
    assert "everything" not in after


def test_the_composer_follows_the_crown(page):
    """Deliberately does not press send: that would message a real session."""
    page.goto(f"{DECK}/manager", wait_until="networkidle")
    page.wait_for_timeout(2500)
    crowned = "no manager" not in (page.text_content("#crown") or "")
    box = page.query_selector("#say")
    send = page.query_selector("#send")
    assert box and send, "the composer is missing"
    assert box.is_disabled() is not crowned, "input must be live only when crowned"
    assert send.is_disabled() is not crowned, "send must be live only when crowned"
    if crowned:
        assert "talk to" in (box.get_attribute("placeholder") or "")


def _armed_composer(page):
    """Open the page with /api/manager/say intercepted, so nothing is sent.

    Every send below is answered by the stub. No socket is touched and no real
    session is ever messaged.
    """
    page.goto(f"{DECK}/manager", wait_until="networkidle")
    page.wait_for_timeout(2500)
    if "no manager" in (page.text_content("#crown") or ""):
        pytest.skip("no manager crowned on this machine")


def test_sending_posts_what_you_typed_and_clears_the_box(page):
    sent = {}

    def stub(route, request):
        sent.update(request.post_data_json or {})
        route.fulfill(status=200, content_type="application/json",
                      body='{"ok": true, "pid": 1}')

    page.route("**/api/manager/say", stub)
    try:
        _armed_composer(page)
        page.fill("#say", "merge")
        page.click("#send")
        page.wait_for_timeout(600)
        assert sent.get("text") == "merge", f"the endpoint got {sent}"
        assert sent.get("mute") is False
        assert "pid" not in sent, "the page must never choose the target pid"
        assert page.input_value("#say") == "", "the box should clear on success"
        assert "sent" in (page.text_content("#say-status") or "")
    finally:
        page.unroute("**/api/manager/say")


def test_the_mute_box_reaches_the_endpoint(page):
    sent = {}
    page.route("**/api/manager/say", lambda route, request: (
        sent.update(request.post_data_json or {}),
        route.fulfill(status=200, content_type="application/json", body='{"ok": true}'),
    ))
    try:
        _armed_composer(page)
        page.check("#mute")
        page.fill("#say", "quietly")
        page.click("#send")
        page.wait_for_timeout(600)
        assert sent.get("mute") is True
        assert "muted" in (page.text_content("#say-status") or "")
    finally:
        page.unroute("**/api/manager/say")


def test_a_refusal_is_named_in_plain_words_and_your_text_comes_back(page):
    """The failure mode that matters: losing what you typed and not knowing why."""
    page.route("**/api/manager/say", lambda route: route.fulfill(
        status=409, content_type="application/json",
        body='{"ok": false, "reason": "no_socket"}'))
    try:
        _armed_composer(page)
        page.fill("#say", "merge and close the issues")
        page.click("#send")
        page.wait_for_timeout(600)
        status = page.text_content("#say-status") or ""
        assert "crown another" in status, f"refusal not explained: {status!r}"
        assert page.get_attribute("#say-status", "data-bad") == "1"
        assert page.input_value("#say") == "merge and close the issues", (
            "a refused message must be handed back, not lost"
        )
    finally:
        page.unroute("**/api/manager/say")


def test_an_empty_box_sends_nothing(page):
    calls = []
    page.route("**/api/manager/say", lambda route: (
        calls.append(1),
        route.fulfill(status=200, content_type="application/json", body='{"ok": true}'),
    ))
    try:
        _armed_composer(page)
        page.fill("#say", "   ")
        page.click("#send")
        page.wait_for_timeout(600)
        assert calls == [], "whitespace must not reach a live session"
    finally:
        page.unroute("**/api/manager/say")


def _stub_deck(page):
    """Open the deck with both write endpoints intercepted.

    /api/focus is stubbed too: unstubbed it drives AppleScript and would yank a
    real Terminal window to the front mid-test.
    """
    crowned, focused = [], []
    page.route("**/api/manager/crown", lambda route, request: (
        crowned.append(request.post_data_json or {}),
        route.fulfill(status=200, content_type="application/json",
                      body='{"ok": true, "manager": {"name": "x"}}'),
    ))
    page.route("**/api/focus/*", lambda route: (
        focused.append(route.request.url),
        route.fulfill(status=200, content_type="application/json",
                      body='{"ok": true, "detail": "stub"}'),
    ))
    page.goto(f"{DECK}/", wait_until="networkidle")
    page.wait_for_timeout(3000)
    return crowned, focused


def test_the_crown_button_crowns_that_card_and_nothing_else(page):
    """The failure mode: the click bubbles and focuses a Terminal instead."""
    crowned, focused = _stub_deck(page)
    try:
        card = page.query_selector(".card")
        if card is None:
            pytest.skip("no live sessions on this machine")
        expected = page.evaluate("""() => {
          const el = document.querySelector('.card');
          return el ? el.dataset.session || null : null;
        }""")
        card.query_selector(".crown-btn").click()
        page.wait_for_timeout(600)

        assert crowned, "the crown button posted nothing"
        assert expected, "a card must carry its session id so this is assertable"
        assert crowned[0].get("session_id") == expected, (
            f"crowned {crowned[0].get('session_id')} but clicked {expected}"
        )
        assert focused == [], "crowning must not also focus the Terminal window"
    finally:
        page.unroute("**/api/manager/crown")
        page.unroute("**/api/focus/*")


def test_clicking_the_card_body_still_focuses(page):
    """The other half of stopPropagation: focusing must survive the crown button."""
    crowned, focused = _stub_deck(page)
    try:
        name = page.query_selector(".card .card__name")
        if name is None:
            pytest.skip("no live sessions on this machine")
        name.click()
        page.wait_for_timeout(600)
        assert focused, "clicking a card no longer focuses its Terminal"
        assert crowned == [], "focusing must not crown"
    finally:
        page.unroute("**/api/manager/crown")
        page.unroute("**/api/focus/*")


def test_every_page_is_reachable_from_every_other(page):
    for route in ("/", "/feed", "/manager"):
        page.goto(f"{DECK}{route}", wait_until="domcontentloaded")
        hrefs = page.evaluate(
            "Array.from(document.querySelectorAll('nav.tabs a')).map(a => a.getAttribute('href'))"
        )
        assert set(hrefs) == {"/", "/feed", "/manager"}, f"{route} nav is {hrefs}"
