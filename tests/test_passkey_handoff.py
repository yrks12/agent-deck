"""A passkey-first sign-in must never dead-end on the desk's container browser.

MEASURED (owner screenshot): Google went to /v3/signin/challenge/pk and Chrome
said "No passkeys available" -- passkeys live on the owner's Mac/phone, never in
the desk's container. The deck now detects the challenge, installs a virtual
authenticator with no credentials (so no OS passkey sheet), clicks "Try another
way" without ever typing a credential, and -- if the page is still a passkey
challenge -- files exactly one card that says so plainly.

Hermetic: a fake driver records the CDP commands it is sent.
"""

import pytest

from server import browser, browser_takeover, computer_mcp, desk_computer, handoff, sandbox

DESK = "atlas"
GOOGLE_PK = "https://accounts.google.com/v3/signin/challenge/pk?flowName=GlifWebSignIn"


@pytest.mark.parametrize("url", [
    GOOGLE_PK,
    "https://accounts.google.com/signin/v2/challenge/pk",
    "https://github.com/sessions/two-factor/webauthn",
    "https://login.microsoftonline.com/common/fido/get",
    "https://appleid.apple.com/auth/passkey",
])
def test_passkey_challenges_are_detected(url):
    assert browser_takeover.is_passkey_challenge(url)


@pytest.mark.parametrize("url", [
    "https://example.com/blog/why-passkeys-win",
    "https://accounts.google.com/signin/v2/challenge/pwd",
    "https://evil.example/challenge/pk",
    "not a url",
])
def test_ordinary_pages_are_not_passkey_challenges(url):
    assert not browser_takeover.is_passkey_challenge(url)


def test_steer_commands_install_an_empty_authenticator_and_never_type():
    cmds = browser_takeover.passkey_steer_commands()
    methods = [m for m, _ in cmds]
    assert methods[:2] == ["WebAuthn.enable", "WebAuthn.addVirtualAuthenticator"]
    assert not any(m.startswith("Input.") or m == "WebAuthn.addCredential"
                   for m in methods)
    js = cmds[-1][1]["expression"]
    assert "try another way" in js.lower() and ".value" not in js


class FakeDriver:
    def __init__(self, url):
        self.url = url
        self.sent = []
        self.sess = browser.BrowserSession(
            desk=DESK, display=":99", cdp_port=9222, profile_dir="/p", pid=None,
            started_at=0.0)

    def run_commands(self, commands):
        self.sent.append(commands)
        return [{} for _ in commands]

    def page_state(self):
        return {"url": self.url, "inputs": [], "frames": []}

    def goto(self, url):
        pass

    def _evaluate(self, js):
        if js == "document.readyState":
            return "complete"
        return {"url": self.url, "title": "Sign in", "text": "x"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "handoffs.json"
    monkeypatch.setattr(handoff, "DEFAULT_PATH", path)
    monkeypatch.setattr(desk_computer.time, "sleep", lambda s: None)
    monkeypatch.setattr(sandbox, "_run", lambda *a, **k: pytest.fail("ran"))
    return path


def call(name, args=None):
    return computer_mcp.handle(DESK, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args or {}}})["result"]


def test_landing_on_a_passkey_page_steers_and_raises_one_passkey_card(
        store, monkeypatch):
    driver = FakeDriver(GOOGLE_PK)
    monkeypatch.setattr(desk_computer, "ensure", lambda desk: driver)

    call("navigate", {"url": GOOGLE_PK})
    call("read_page")

    assert driver.sent, "the virtual authenticator / steer was sent"
    assert driver.sent[0][0][0] == "WebAuthn.enable"
    cards = handoff.waiting(store)
    assert len(cards) == 1
    needs = cards[0].needs
    assert "wants a passkey" in needs
    # Google is Device Bound (PR #164): the fresh passkey sign-in leads and his
    # Chrome login is not offered -- never "try another way" (a takeover).
    assert "Sign in fresh with passkey" in needs and "Use my Chrome login" not in needs
    assert "not Atlas" in needs


def test_a_steer_that_left_the_passkey_page_raises_no_passkey_card(
        store, monkeypatch):
    driver = FakeDriver(GOOGLE_PK)

    def steer(commands):
        driver.url = "https://accounts.google.com/v3/signin/challenge/pwd"
        return [{} for _ in commands]

    driver.run_commands = steer
    monkeypatch.setattr(desk_computer, "ensure", lambda desk: driver)
    call("navigate", {"url": GOOGLE_PK})
    # No PASSKEY card. Google's password step is still a sign-in, so the
    # ordinary sign-in card (his Chrome login first) is right -- 2026-10-01.
    assert not [h for h in handoff.waiting(store) if "wants a passkey" in h.needs]
