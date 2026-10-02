"""Decisions (K4): a desk asks the owner to pick, he taps, the desk hears it.

The defect: a question from a desk was prose. He had to type an answer, the
desk had to parse it, and a thread full of "option A or B?" could not show
which ones were settled. The only buttons in the app were tool approvals.

Now a desk's `ask` becomes a `kind: "decision"` message with 2-4 options;
`POST /v1/decisions/{id}` posts his pick into the thread as his own message,
flips the card to `answered`, and delivers it (which wakes a sleeping desk). A
later message of his in the same thread marks any still-open card `skipped`.

Hermetic: tmp bus dir, hand-written snapshot, no daemon.
"""

import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import decisions, office
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
DESK = {"name": "atlas", "cwd": "/tmp/p", "engine": "claude", "mission": "run",
        "label": "Chief", "charter": "Own it.", "reports_to": None}
SNAP = {"generated_at": 1_756_000_100.0, "sessions": []}
AUTH = {"Authorization": f"Bearer {TOKEN}"}
THREAD = "/v1/threads/direct:atlas/messages"
OPTIONS = [{"label": "Approve as-is", "value": "Approve copy as-is",
            "style": "primary"},
           {"label": "Rewrite", "value": "Rewrite the copy"}]


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(decisions, "DEFAULT_PATH", tmp_path / "decisions.json")
    (tmp_path / "messages.jsonl").write_text("")
    (tmp_path / "roster.json").write_text(
        json.dumps({"version": 1, "agents": [DESK]}))
    return tmp_path


def make(bus, monkeypatch, **kw):
    surface = api_mod.Surface(
        snapshot=lambda: SNAP, comms=comms_mod.CommsIndex(),
        roster_path=bus / "roster.json", prefs_path=bus / "prefs.json", **kw)
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return surface, TestClient(app)


def page(client):
    return client.get(THREAD, headers=AUTH).json()["messages"]


def ask(bus, **kw):
    args = {"prompt": "Northwind waitlist copy?", "options": OPTIONS,
            "help": "Full draft: ...", "allow_custom": True}
    args.update(kw)
    return decisions.create("atlas", path=bus / "decisions.json", **args)


# ── the shape ────────────────────────────────────────────────────────────────


def test_a_decision_has_a_stable_id_and_starts_open(bus):
    made = ask(bus)
    assert re.fullmatch(r"dec_[0-9a-f]{12}", made["id"])
    assert made["state"] == "open" and made["answer"] is None
    assert [o["label"] for o in made["options"]] == ["Approve as-is", "Rewrite"]
    # A missing style is `default`, a missing value is the label.
    assert made["options"][1]["style"] == "default"
    only = decisions.create("atlas", "Pick", ["Yes", "No"],
                            path=bus / "decisions.json")
    assert only["options"][0] == {"label": "Yes", "value": "Yes",
                                  "style": "default"}


@pytest.mark.parametrize("options,reason", [
    ([{"label": "one"}], "bad_options"),
    ([{"label": str(i)} for i in range(5)], "bad_options"),
    ([{"label": "x" * 41}, {"label": "y"}], "label_too_long"),
    ([{"label": "a", "style": "loud"}, {"label": "b"}], "bad_style"),
    ([{"label": ""}, {"label": "b"}], "bad_options"),
])
def test_options_are_two_to_four_short_labels(bus, options, reason):
    with pytest.raises(decisions.DecisionError) as err:
        ask(bus, options=options)
    assert err.value.reason == reason
    assert not (bus / "messages.jsonl").read_text()


def test_an_empty_prompt_is_refused(bus):
    with pytest.raises(decisions.DecisionError) as err:
        ask(bus, prompt="   ")
    assert err.value.reason == "empty_prompt"


# ── in the thread ────────────────────────────────────────────────────────────


def test_the_decision_is_a_desk_message_of_kind_decision(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    made = ask(bus)
    (message,) = page(client)
    assert message["kind"] == "decision"
    assert (message["author"], message["role"]) == ("atlas", "agent")
    d = message["decision"]
    assert d["id"] == made["id"] and d["state"] == "open"
    assert d["prompt"] == "Northwind waitlist copy?"
    assert d["help"] == "Full draft: ..."
    assert d["allow_custom"] is True and d["answer"] is None
    assert [o["value"] for o in d["options"]] == [o["value"] for o in OPTIONS]
    # An old client that knows only `text` still reads the question.
    assert "Northwind waitlist copy?" in message["text"]
    assert "Approve as-is" in message["text"]


def test_ordinary_messages_stay_kind_text(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    office.send("owner", "done", sender="atlas")
    (message,) = page(client)
    assert message["kind"] == "text"


# ── answering ────────────────────────────────────────────────────────────────


def test_answering_posts_his_pick_flips_the_card_and_delivers(bus, monkeypatch):
    delivered = []
    _, client = make(bus, monkeypatch,
                     deliver=lambda name, text: delivered.append((name, text))
                     or False)
    made = ask(bus)
    r = client.post(f"/v1/decisions/{made['id']}", headers=AUTH,
                    json={"value": "Approve copy as-is"})
    assert r.status_code == 200 and r.json()["ok"] is True

    card, pick = page(client)
    assert card["decision"]["state"] == "answered"
    assert card["decision"]["answer"] == "Approve copy as-is"
    assert (pick["author"], pick["role"]) == ("owner", "owner")
    assert pick["text"] == "Approve copy as-is"
    # Handed to the desk through the same door his typing uses, which is the
    # door that wakes a sleeping desk.
    assert [name for name, _ in delivered] == ["atlas"]
    assert "Approve copy as-is" in delivered[0][1]


def test_a_second_answer_is_409_already_answered(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    made = ask(bus)
    url = f"/v1/decisions/{made['id']}"
    assert client.post(url, headers=AUTH, json={"value": "Rewrite the copy"}
                       ).status_code == 200
    r = client.post(url, headers=AUTH, json={"value": "Approve copy as-is"})
    assert r.status_code == 409 and r.json()["reason"] == "already_answered"
    assert len(page(client)) == 2  # no second owner message


def test_unknown_decision_is_404(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    r = client.post("/v1/decisions/dec_000000000000", headers=AUTH,
                    json={"value": "x"})
    assert r.status_code == 404 and r.json()["reason"] == "unknown_decision"


def test_custom_text_only_when_allowed(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    closed = ask(bus, allow_custom=False)
    r = client.post(f"/v1/decisions/{closed['id']}", headers=AUTH,
                    json={"value": "something else"})
    assert r.status_code == 400 and r.json()["reason"] == "not_an_option"
    opened = ask(bus, allow_custom=True)
    r = client.post(f"/v1/decisions/{opened['id']}", headers=AUTH,
                    json={"value": "Shorter, and mention the dentist"})
    assert r.status_code == 200


def test_an_empty_answer_is_400(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    made = ask(bus)
    r = client.post(f"/v1/decisions/{made['id']}", headers=AUTH,
                    json={"value": "  "})
    assert r.status_code == 400 and r.json()["reason"] == "empty_value"


def test_a_later_message_of_his_skips_the_open_card(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    made = ask(bus)
    client.post(THREAD, headers=AUTH, json={"text": "actually, hold off"})
    card = next(m for m in page(client) if m["kind"] == "decision")
    assert card["decision"]["state"] == "skipped"
    assert decisions.find(made["id"], path=bus / "decisions.json")[
        "state"] == "skipped"


def test_the_engineer_does_not_skip_his_card(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    ask(bus)
    client.post(THREAD, headers=AUTH, json={"text": "ping", "as": "engineer"})
    card = next(m for m in page(client) if m["kind"] == "decision")
    assert card["decision"]["state"] == "open"


def test_answering_one_card_does_not_skip_it_by_its_own_message(
        bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    made = ask(bus)
    client.post(f"/v1/decisions/{made['id']}", headers=AUTH,
                json={"value": "Rewrite the copy"})
    card = next(m for m in page(client) if m["kind"] == "decision")
    assert card["decision"]["state"] == "answered"


# ── the stream ───────────────────────────────────────────────────────────────


def test_the_stream_carries_a_decision_frame_on_create_and_on_answer(
        bus, monkeypatch):
    surface, client = make(bus, monkeypatch)
    surface.refresh()
    made = ask(bus)
    frames = [e for e in surface.refresh() if e["type"] == "decision"]
    assert len(frames) == 1
    assert frames[0]["thread_id"] == "direct:atlas"
    assert frames[0]["decision"]["id"] == made["id"]
    assert frames[0]["decision"]["state"] == "open"

    surface.answer_decision(made["id"], "Rewrite the copy")
    frames = [e for e in surface.refresh() if e["type"] == "decision"]
    assert [f["decision"]["state"] for f in frames] == ["answered"]
    assert frames[0]["decision"]["answer"] == "Rewrite the copy"
    assert [e for e in surface.refresh() if e["type"] == "decision"] == []


def test_a_decision_open_before_the_deck_started_is_not_re_announced(
        bus, monkeypatch):
    ask(bus)
    surface, _ = make(bus, monkeypatch)
    assert [e for e in surface.refresh() if e["type"] == "decision"] == []
