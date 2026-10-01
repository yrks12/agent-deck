"""An ALLOWED sign-in / sign-up card must actually let the desk do it.

MEASURED 2026-09-30 22:21 on desk northwind-growth (owner's iPhone): he
resolved card wekf ALLOWED ("approved you to sign in on
https://www.instagram.com for the next 60 minutes") and said "Do it yourself".
The desk answered "the browser locks me out of sign-up pages, and I never set
or type a password, even with your OK" and raised q245 ten seconds later on
the same desk + origin + kind. Two faults:

1. The grant was on disk (browser_grants.json, 22:21:59) but the desk's
   computer MCP process was started at 22:14 -- before the deploy -- and kept
   running the old guard, which never reads grants. A deploy must reach a
   running desk's tools.
2. The desk's own brief says "you never type a password", and the ALLOWED
   message did not override it.

Owner ruling: with his approval (or Allow always) a desk may create accounts
and type passwords itself. The password is generated and saved by the deck,
typed straight into the page, and never shown to the model or the chat.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import browser, browser_takeover as T, computer_mcp
from server import desk_computer, handoff, hire, office, sandbox, vault
from server.sources import comms as comms_mod

DESK = "northwind-growth"
TOKEN = "t-secret-not-a-real-credential"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: Instagram's email sign-up page, as the live driver reports it.
IG_SIGNUP = {
    "url": "https://www.instagram.com/accounts/emailsignup/",
    "inputs": [
        {"type": "text", "autocomplete": "tel", "name": "emailOrPhone",
         "hidden": False},
        {"type": "password", "autocomplete": "new-password", "name": "password",
         "hidden": False},
        {"type": "text", "autocomplete": "name", "name": "fullName",
         "hidden": False},
        {"type": "text", "autocomplete": "username", "name": "username",
         "hidden": False},
    ],
    "frames": [],
}


class FakeDriver:
    def __init__(self, state):
        self.state = state
        self.sess = browser.BrowserSession(
            desk=DESK, display=":99", cdp_port=9222, profile_dir="/p",
            pid=None, started_at=0.0)

    def page_state(self):
        return self.state

    def goto(self, url):
        pass

    def _evaluate(self, js):
        if js == "document.readyState":
            return "complete"
        return {"url": self.state["url"], "title": "Sign up", "text": "Sign up"}


@pytest.fixture
def typed(monkeypatch):
    """Every gesture and every secret that reached the desk's screen."""
    out = []
    monkeypatch.setattr(sandbox, "send_input",
                        lambda desk, action: out.append(("input", desk, action)))
    monkeypatch.setattr(sandbox, "type_secret",
                        lambda desk, value: out.append(("secret", desk, value)))
    return out


@pytest.fixture
def store(tmp_path, monkeypatch, typed):
    path = tmp_path / "handoffs.json"
    monkeypatch.setattr(handoff, "DEFAULT_PATH", path)
    monkeypatch.setattr(vault, "DEFAULT_PATH", tmp_path / "vault.json")
    monkeypatch.setattr(desk_computer.time, "sleep", lambda s: None)
    monkeypatch.setattr(sandbox, "_run", lambda *a, **k: pytest.fail("ran"))
    monkeypatch.setattr(desk_computer, "ensure",
                        lambda desk: FakeDriver(IG_SIGNUP))
    return path


def call(name, args=None):
    return computer_mcp.handle(DESK, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args or {}}})["result"]


@pytest.fixture
def client(tmp_path, store, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1, "agents": [
        {"name": DESK, "cwd": "/tmp/cg", "engine": "claude",
         "mission": "grow it", "reports_to": None}]}))
    from server import app as app_mod
    monkeypatch.setattr(app_mod, "_try_inject", lambda name, text: False)
    monkeypatch.setattr(app_mod, "_wake_later", lambda name, reason: None)
    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        comms=comms_mod.CommsIndex(), deliver=app_mod._deliver_or_wake,
        roster_path=roster, prefs_path=tmp_path / "prefs.json",
        asks_path=tmp_path / "asks.json", handoffs_path=store)
    surface.refresh()
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


def _allow(client, store):
    card = handoff.waiting(store)[0]
    ok = client.post(f"/v1/handoffs/{card.id}", json={"reply": "done"},
                     headers=AUTH)
    assert ok.status_code == 200, ok.text
    mail = [json.loads(x) for x in office.MESSAGES_FILE.read_text().splitlines()
            if x]
    return [m["text"] for m in mail if m.get("to") == DESK][-1]


# ── 1. ALLOWED unlocks the sign-up page on the very next check ─────────────


def test_an_allowed_instagram_sign_up_lets_the_desk_click_and_type(
        store, client, typed):
    assert T.needs_human(IG_SIGNUP) == "login"
    assert call("click", {"x": 5, "y": 5})["isError"] is True
    assert typed == []

    _allow(client, store)

    assert call("click", {"x": 5, "y": 5})["isError"] is False
    assert call("type_text", {"text": "northwind.growth"})["isError"] is False
    assert [t[0] for t in typed] == ["input", "input"]
    assert handoff.waiting(store) == [], "no second card after ALLOWED"


# ── 2. the words the desk is given ────────────────────────────────────────


def test_the_allowed_message_grants_passwords_and_does_not_forbid_them(
        store, client):
    call("click", {"x": 5, "y": 5})
    told = _allow(client, store).lower()
    assert "type_password" in told
    assert "create the account" in told or "create accounts" in told
    assert "overrides" in told
    assert "never type a password" not in told
    assert "never set or type" not in told


def test_the_brief_no_longer_forbids_typing_a_password():
    for text in (hire.COMPUTER, hire.NEVER_BLOCKED):
        low = text.lower()
        assert "never type a password" not in low
        assert "never set or type" not in low
    assert "mcp__computer__type_password" in hire.COMPUTER
    assert "Allow" in hire.COMPUTER
    assert "Take over the screen" in hire.NEVER_BLOCKED


def test_type_password_is_a_tool_the_desk_is_given():
    names = {t["name"] for t in computer_mcp.TOOLS}
    assert "type_password" in names


# ── 3. the password: generated, saved, typed -- never shown ────────────────


def test_type_password_generates_saves_and_types_without_showing_it(
        store, client, typed):
    call("click", {"x": 5, "y": 5})
    _allow(client, store)

    reply = call("type_password", {"username": "northwind.growth"})
    assert reply["isError"] is False, reply
    secrets_typed = [t for t in typed if t[0] == "secret"]
    assert len(secrets_typed) == 1
    value = secrets_typed[0][2]
    assert len(value) >= 16
    assert value not in json.dumps(reply), "the model must never see it"

    saved = vault.env_for(vault.DEFAULT_PATH, DESK)
    assert list(saved.values()) == [value]
    name = next(iter(saved))
    assert "INSTAGRAM" in name
    meta = next(m for m in vault.meta(vault.DEFAULT_PATH) if m.name == name)
    assert "https://www.instagram.com" in meta.description
    assert "northwind.growth" in meta.description

    # A second field (confirm password) or a later sign-in: the SAME value.
    call("type_password", {"username": ""})
    assert [t[2] for t in typed if t[0] == "secret"] == [value, value]


def test_type_password_is_held_until_the_login_card_is_allowed(store, typed):
    reply = call("type_password", {"username": "x"})
    assert reply["isError"] is True
    assert typed == []
    assert vault.meta(vault.DEFAULT_PATH) == []


# ── 4. a deploy reaches a desk that is already running ─────────────────────


def test_a_running_computer_server_reloads_changed_guard_code(monkeypatch):
    reloaded = []
    stamps = iter([1.0, 2.0])
    monkeypatch.setattr(computer_mcp, "_stamp", lambda: next(stamps))
    monkeypatch.setattr(computer_mcp.importlib, "reload",
                        lambda mod: reloaded.append(mod.__name__) or mod)
    monkeypatch.setattr(computer_mcp, "_loaded_at", None)
    computer_mcp._fresh()          # first look: remember the stamp
    assert reloaded == []
    computer_mcp._fresh()          # the files changed underneath it
    assert "server.browser_takeover" in reloaded
    assert reloaded[-1] == "server.desk_computer"
