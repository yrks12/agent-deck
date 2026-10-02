"""A desk's computer that stops at a login must raise the owner's card.

MEASURED gap: `desk_computer._guard` refused at a login/2FA/captcha/payment
page and only told the desk to report it in prose -- a dead end for the owner,
who never saw a "Needs your attention" card. The deck itself now files the
handoff (the existing `handoff` store, answered by the existing
`POST /v1/handoffs/{id}`), tells the desk a card was raised, and the owner's
"I'm done, continue" sends the desk a message and wakes it if asleep.

Hermetic: a fake driver stands in for the browser/CDP layer.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import browser, computer_mcp, desk_computer, handoff, office, sandbox
from server.sources import comms as comms_mod

DESK = "atlas"
TOKEN = "t-secret-not-a-real-credential"
LOGIN_URL = "https://accounts.example.com/signin"
LOGIN_STATE = {"url": LOGIN_URL, "inputs": [{"type": "password"}], "frames": []}


class FakeDriver:
    def __init__(self, state):
        self.state = state
        self.sess = browser.BrowserSession(
            desk=DESK, display=":99", cdp_port=9222, profile_dir="/p", pid=None,
            started_at=0.0)

    def page_state(self):
        return self.state

    def goto(self, url):
        pass

    def _evaluate(self, js):
        if js == "document.readyState":
            return "complete"
        return {"url": self.state["url"], "title": "Sign in", "text": "Sign in"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "handoffs.json"
    monkeypatch.setattr(handoff, "DEFAULT_PATH", path)
    monkeypatch.setattr(desk_computer.time, "sleep", lambda s: None)
    monkeypatch.setattr(sandbox, "_run", lambda *a, **k: pytest.fail("ran"))
    return path


def on_page(monkeypatch, state):
    monkeypatch.setattr(desk_computer, "ensure", lambda desk: FakeDriver(state))


def call(name, args=None):
    return computer_mcp.handle(DESK, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args or {}}})["result"]


def test_navigating_to_a_login_raises_exactly_one_card_for_that_desk(
        store, monkeypatch):
    on_page(monkeypatch, LOGIN_STATE)

    first = call("navigate", {"url": LOGIN_URL})
    call("read_page")      # the desk looks again -- still the same block
    call("click", {"x": 1, "y": 1})

    cards = handoff.waiting(store)
    assert len(cards) == 1, "one stop, one card -- not one per tool call"
    card = cards[0]
    assert (card.agent, card.kind) == (DESK, "login")
    assert "accounts.example.com" in card.where
    assert "sign in" in card.needs.lower()
    text = first["content"][0]["text"]
    assert card.id in text and "card" in text.lower()


def test_a_click_on_a_login_is_refused_and_says_a_card_was_raised(
        store, monkeypatch):
    on_page(monkeypatch, LOGIN_STATE)
    reply = call("click", {"x": 1, "y": 1})
    assert reply["isError"] is True
    assert handoff.waiting(store)[0].id in reply["content"][0]["text"]


def test_an_ordinary_page_raises_nothing(store, monkeypatch):
    on_page(monkeypatch, {"url": "https://example.com", "inputs": [],
                          "frames": []})
    call("navigate", {"url": "https://example.com"})
    assert handoff.load(store) == []


def test_a_card_that_cannot_be_filed_does_not_break_the_tool(store, monkeypatch):
    on_page(monkeypatch, LOGIN_STATE)
    monkeypatch.setattr(handoff, "raise_handoff",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    reply = call("navigate", {"url": LOGIN_URL})
    assert reply["isError"] is False
    assert "STOP" in reply["content"][0]["text"]


# -- answering it: the desk is told to continue, and woken --------------------


@pytest.fixture
def surface(tmp_path, store, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1, "agents": [
        {"name": DESK, "cwd": "/tmp/atlas", "engine": "claude",
         "mission": "run it", "reports_to": None}]}))
    from server import app as app_mod
    woken = []
    monkeypatch.setattr(app_mod, "_try_inject", lambda name, text: False)
    monkeypatch.setattr(app_mod, "_wake_later",
                        lambda name, reason: woken.append((name, reason)))
    made = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        comms=comms_mod.CommsIndex(),
        deliver=app_mod._deliver_or_wake,
        roster_path=roster, prefs_path=tmp_path / "prefs.json",
        asks_path=tmp_path / "asks.json", handoffs_path=store)
    made.refresh()
    made.woken = woken
    return made


def test_im_done_continue_sends_the_desk_a_message_and_wakes_it(
        surface, store, monkeypatch):
    on_page(monkeypatch, LOGIN_STATE)
    call("navigate", {"url": LOGIN_URL})
    card = handoff.waiting(store)[0]

    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    rows = client.get("/v1/handoffs", headers=auth).json()["handoffs"]
    assert [r["id"] for r in rows] == [card.id]
    assert rows[0]["agent"] == DESK

    done = client.post(f"/v1/handoffs/{card.id}", json={"reply": "done"},
                       headers=auth)

    assert done.status_code == 200 and done.json()["resumed"] is True
    assert surface.woken == [(DESK, "owner_message")], "the asleep desk is woken"
    lines = [json.loads(x) for x in office.MESSAGES_FILE.read_text().splitlines() if x]
    mail = [m for m in lines if m.get("to") == DESK]
    assert len(mail) == 1 and card.id in mail[0]["text"]
    assert "continue" in mail[0]["text"].lower() or "carry on" in mail[0]["text"].lower()
