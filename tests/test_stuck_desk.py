"""Detector 2: a desk frozen mid-session must not read as an ordinary one.

THE SILENCE. `blocked` is set only when `state == "OFFLINE"`. That covered the
desk that never started, which was the bug of the day. It does not cover the
desk that started fine and then stopped: a session sitting at
`attention.kind == "permission_prompt"` with NO matching question on the board
is stuck at a dialog in its own Terminal window, and the row says nothing at
all. Every desk not under the permission hooks lands here -- older desks, and
the owner's own windows -- and the deck reports them as working.

WHERE THE LINE IS, and it is the whole difficulty. A desk waiting on a question
the owner **can** answer in the app is working correctly; the ask loop is doing
exactly what it was built to do and flagging it would make `blocked` noise, and
noise is how the next real one gets ignored. Only the *unanswerable* case is
blocked -- a dialog is drawn, and `asks.json` holds no live question for that
desk. Both halves are pinned below.

THE CONTRACT THIS BREAKS, deliberately. `docs/client-api.md` states as a rule a
client may rely on: "`blocked` is non-null ONLY while `state == OFFLINE`". That
sentence stops being true here, so it is amended in the same change, and the new
slug gets its own row in the §3.1 table -- `tests/test_client_api_doc.py` fails
otherwise, by design. The client falls back to `detail` for a slug it does not
know, so `detail` has to be a sentence worth reading rather than a slug echoed
back.

THE GOOD SIGNAL is a blocked row a person can act on: the slug, a `what` that
names the dialog and says it must be answered at the keyboard, and a `detail`
that stands on its own. Never the absence of an error.
"""

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import asking
from server import office
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"

CHIEF = {
    "name": "chief", "cwd": "/tmp/p", "engine": "claude", "mission": "run it",
    "label": "Negotiator", "charter": "Own the deal.", "reports_to": None,
}


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_file(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [CHIEF]}))
    return path


@pytest.fixture
def asks_file(bus):
    return bus / "asks.json"


@pytest.fixture
def snapshot():
    """One session seated at `chief`, working normally."""
    return {
        "generated_at": 1_756_000_100.0,
        "sessions": [{
            "session_id": "sid-chief", "pid": 4242, "name": "chief",
            "cwd": "/tmp/p", "project": "p", "state": "WORKING",
            "state_since": 1_756_000_000.0, "attention": None,
        }],
    }


@pytest.fixture
def client(bus, roster_file, asks_file, snapshot, monkeypatch):
    surface = api_mod.Surface(
        snapshot=lambda: snapshot,
        comms=comms_mod.CommsIndex(),
        roster_path=roster_file,
        prefs_path=bus / "agent_prefs.json",
        asks_path=asks_file,
    )
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    built = FastAPI()
    api_mod.register(built, surface=surface, background=False)
    return TestClient(built)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def stall(snapshot, kind="permission_prompt",
          message="Claude needs your permission to use Bash"):
    """Freeze the seated session on a dialog."""
    snapshot["sessions"][0]["state"] = "NEEDS_YOU"
    snapshot["sessions"][0]["attention"] = {
        "kind": kind, "message": message, "since": 1_756_000_050.0}


def row(client, name="chief"):
    body = client.get("/v1/agents", headers=auth()).json()
    return next(a for a in body["agents"] if a["name"] == name)


# ── the silence ────────────────────────────────────────────────────────────


def test_a_desk_stuck_on_a_dialog_nobody_can_answer_says_so(
    client, snapshot, asks_file
):
    """The GOOD signal, and the whole slice: the row carries a reason."""
    stall(snapshot)

    blocked = row(client)["blocked"]

    assert blocked is not None, (
        "a desk frozen on a permission dialog with no question on the board "
        "reads as an ordinary working desk; nobody will go and look")
    assert blocked["reason"] == api_mod.STUCK_ON_DIALOG


def test_the_reason_is_a_sentence_a_person_can_act_on(client, snapshot):
    """The client renders a sentence per slug and falls back to `detail` for one
    it does not know. A slug echoed into `detail` is a row that tells the owner
    nothing on the very build where it matters most."""
    stall(snapshot)

    blocked = row(client)["blocked"]

    assert "keyboard" in blocked["what"].lower(), blocked["what"]
    assert len(blocked["what"]) > 40, "not a sentence"
    assert blocked["detail"] and blocked["detail"] != blocked["reason"], blocked
    assert blocked["at"] == 1_756_000_050.0, (
        "the clock should be when the desk froze, not when it was noticed")


def test_the_notification_text_is_carried_through_as_the_fallback(
    client, snapshot
):
    """What Claude Code actually said names the tool. That is the single most
    useful string the deck holds about this stall."""
    stall(snapshot, message="Claude needs your permission to use Bash")

    assert "Bash" in row(client)["blocked"]["detail"]


# ── the line: an answerable question is not a block ────────────────────────


def test_a_desk_waiting_on_a_question_he_can_answer_is_not_flagged(
    client, snapshot, asks_file
):
    """The ask loop working correctly must not be reported as a fault. This is
    the half that decides whether `blocked` stays worth reading."""
    asking.record(asks_file, agent="chief", tool="Bash",
                  subject="npx some-brand-new-thing", cwd="/tmp/p")
    stall(snapshot)

    assert row(client)["blocked"] is None, (
        "the deck put this question to the owner and is waiting for his tap; "
        "flagging it turns `blocked` into noise")


def test_an_answered_question_no_longer_holds_the_flag_off(
    client, snapshot, asks_file
):
    """Precision on "pending". Once he has answered, there is nothing left in
    the app to answer -- if the dialog is still up, he is needed at the desk."""
    ask = asking.record(asks_file, agent="chief", tool="Bash",
                        subject="npx some-brand-new-thing", cwd="/tmp/p")
    asking.answer(asks_file, ask.id, "never")
    stall(snapshot)

    assert row(client)["blocked"] is not None


def test_a_question_for_a_different_desk_does_not_cover_this_one(
    client, snapshot, asks_file
):
    """Matching has to be to the desk. Any pending ask anywhere would otherwise
    silence every stuck desk on the board."""
    asking.record(asks_file, agent="scout", tool="Bash", subject="npm i",
                  cwd="/tmp/elsewhere")
    stall(snapshot)

    assert row(client)["blocked"] is not None


# ── and it does not fire on desks that are fine ────────────────────────────


def test_a_working_desk_is_not_blocked(client):
    assert row(client)["blocked"] is None


def test_agent_needs_input_is_not_reported_as_blocked(client, snapshot):
    """The deliberate scope line. `agent_needs_input` is the other attention
    kind, and no ask row is ever written for one -- so "no matching pending
    ask" is true by construction and this would flag every question a desk asks
    its owner. The deck has no evidence those are unanswerable, and a rule that
    fires on all of them is the confident wrong answer this slice is against."""
    stall(snapshot, kind="agent_needs_input", message="Chief has a question")

    assert row(client)["blocked"] is None


def test_the_flag_clears_itself_when_the_dialog_goes(client, snapshot):
    """Computed live off the card, never stored. A `blocked` that outlives the
    modal is exactly the stale-reason failure the OFFLINE half already fixed."""
    stall(snapshot)
    assert row(client)["blocked"] is not None

    snapshot["sessions"][0]["attention"] = None
    snapshot["sessions"][0]["state"] = "WORKING"

    assert row(client)["blocked"] is None


def test_the_stall_is_never_written_into_the_prefs_file(client, bus, snapshot):
    """The mechanism behind the test above, pinned separately: persisting this
    would leave a desk looking broken long after it was fixed."""
    stall(snapshot)
    assert row(client)["blocked"] is not None

    prefs = bus / "agent_prefs.json"
    raw = prefs.read_text() if prefs.exists() else "{}"
    assert api_mod.STUCK_ON_DIALOG not in raw, raw


# ── the OFFLINE contract still holds ───────────────────────────────────────


def test_an_offline_desk_still_reports_its_stored_reason(client, bus, snapshot):
    """The existing half must not regress: a desk that never started still
    carries the pretrust reason written when it was spawned."""
    snapshot["sessions"] = []
    (bus / "agent_prefs.json").write_text(json.dumps({"version": 1, "agents": {
        "chief": {"blocked": {"what": "workspace trust was not pre-accepted",
                              "reason": "unexpected_shape", "detail": "",
                              "at": time.time()}}}}))

    blocked = row(client)["blocked"]
    assert blocked is not None and blocked["reason"] == "unexpected_shape"


def test_the_detail_panel_shows_the_same_stall_as_the_sidebar(client, snapshot):
    """`GET /v1/agents/{name}` is the screen a human opens when a desk looks
    dead. It reads `blocked` off the same row, and this pins that it stays that
    way."""
    stall(snapshot)

    panel = client.get("/v1/agents/chief", headers=auth()).json()
    assert panel["blocked"]["reason"] == api_mod.STUCK_ON_DIALOG


# ── the doc is part of the change ──────────────────────────────────────────


def test_the_doc_no_longer_promises_blocked_only_while_offline(client):
    """The invariant a client is entitled to rely on is being broken on purpose,
    so the sentence stating it has to go with it. A doc that still promises the
    old rule is worse than no doc: it is the deck asserting something about
    itself that is not true, which is this whole slice's subject."""
    doc = (api_mod.Path(api_mod.__file__).resolve().parent.parent
           / "docs" / "client-api.md").read_text()
    section = doc[doc.index("### 3.1"):doc.index("### The preview line")]

    assert f"`{api_mod.STUCK_ON_DIALOG}`" in section, (
        "the new slug needs its own row in the §3.1 table; the client renders "
        "a sentence per slug and shows a generic fallback for one it lacks")
    assert "non-null **only** while `state == \"OFFLINE\"`" not in doc, (
        "that rule is no longer true -- a seated desk can now be blocked")
