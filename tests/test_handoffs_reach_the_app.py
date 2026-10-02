"""The block only a human can clear must be visible IN THE APP.

THE GAP. `server/handoff.py` is finished, tested and completely unexposed:
there is no `/v1/handoffs` route, so nothing on the phone can see a secure
handoff. The one class of block a human MUST clear by hand -- a 2FA code, a
CAPTCHA, an SMS confirmation, `gh auth login`, signing into Google -- reaches
the owner on WhatsApp or not at all. That is the same complaint as the
approvals defect one layer up: the deck knows, the product does not draw it.

The card it has to become, in the words a comparable product already uses:

    Needs your attention
    Sign in to Microsoft 365 admin (initech.example), then hand back
    [ Skip this step ]  [ I'm done, continue ]

THE GOOD SIGNALS, all four asserted as presence:

1. A waiting handoff is PUBLISHED, shaped like an approval, so one tray can
   draw both -- with the DESK NAME in `agent`, resolved by the same helper the
   approvals fix uses, never a session UUID.
2. It carries both options with their own summary text, so the two buttons can
   be drawn from the payload rather than hardcoded in the client.
3. Everything that leaves goes through `handoff.redact()`. A one-time code in
   a payload that reaches a phone is the precise thing that module exists to
   prevent.
4. Answering it INSTRUCTS the desk, and `done` and `skipped` are different
   instructions: `done` orders a re-check ("a human says they did it" is a
   claim, not a fact); `skipped` orders abandonment. Collapse the two and a
   skipped handoff is an infinite retry loop -- rule 2 of that module.

Hermetic: tmp bus, hand-written snapshot, nothing spawned, no real session.
"""

import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import handoff, office
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
ATLAS_SID = "bfffdc59-5f34-4b6b-b304-300c80cb3c25"
STRANGER_SID = "11111111-2222-3333-4444-555555555555"

NEEDS = "Sign in to Microsoft 365 admin (initech.example), then hand back"
STATE = "the tenant exists and nothing has been billed yet"
WHERE = "https://admin.microsoft.com"


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_file(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [
        {"name": "atlas", "cwd": "/tmp/atlas", "engine": "claude",
         "mission": "run it", "reports_to": None},
    ]}))
    return path


@pytest.fixture
def delivered():
    """Every (name, text) the daemon would have injected into a live session."""
    return []


@pytest.fixture
def surface(bus, roster_file, delivered):
    made = api_mod.Surface(
        snapshot=lambda: {
            "generated_at": 1_756_000_100.0,
            "sessions": [{
                "session_id": ATLAS_SID, "pid": 4242, "name": "atlas",
                "cwd": "/tmp/atlas", "project": "atlas", "state": "WORKING",
                "state_since": 1_756_000_000.0,
            }],
        },
        comms=comms_mod.CommsIndex(),
        deliver=lambda name, text: bool(delivered.append((name, text)) or True),
        roster_path=roster_file,
        prefs_path=bus / "agent_prefs.json",
        asks_path=bus / "asks.json",
        handoffs_path=bus / "handoffs.json",
    )
    made.refresh()
    return made


@pytest.fixture
def client(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    built = FastAPI()
    api_mod.register(built, surface=surface, background=False)
    return TestClient(built)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def raise_one(bus, *, agent="atlas", kind="login", needs=NEEDS, state=STATE,
              where=WHERE, evidence=""):
    return handoff.raise_handoff(bus / "handoffs.json", agent=agent, kind=kind,
                                 needs=needs, state=state, where=where,
                                 evidence=evidence)


def listed(client):
    response = client.get("/v1/handoffs", headers=auth())
    assert response.status_code == 200, response.text
    return response.json()["handoffs"]


# -- 1. it exists at all, and it names a desk ---------------------------------


def test_a_waiting_handoff_is_published_at_all(client, bus):
    """THE GOOD SIGNAL. The block a human must clear reaches the app, with
    everything the card needs to be drawn -- what is needed, the state of the
    work, and where to go."""
    row = raise_one(bus)

    rows = listed(client)

    assert [h["id"] for h in rows] == [row.id], (
        "a handoff nobody can see is the whole defect: the desk is stopped, "
        "the app draws nothing, and WhatsApp is the only channel")
    published = rows[0]
    assert published["needs"] == NEEDS
    assert published["state"] == STATE, (
        "a handoff that does not state the state of the work is a bug -- it "
        "is what decides whether he opens a laptop now or after dinner")
    assert published["where"] == WHERE
    assert published["kind"] == "login"
    assert published["status"] == "waiting"


@pytest.mark.parametrize("shape,agent,desk,known", [
    ("the desk named itself", "atlas", "atlas", True),
    ("a bare session uuid", ATLAS_SID, "atlas", True),
    ("the `session <id>` fallback", f"session {ATLAS_SID[:8]}", "atlas", True),
    ("nobody the board knows", STRANGER_SID, STRANGER_SID, False),
])
def test_the_published_agent_is_the_desk_that_is_blocked(
    client, bus, shape, agent, desk, known
):
    """Same join as `/v1/approvals`, same helper, so one tray can filter both
    against the desk whose conversation is open. And the same honesty when it
    cannot be resolved: say so rather than invent an owner."""
    raise_one(bus, agent=agent)

    row = listed(client)[0]

    assert row["agent"] == desk, (
        f"{shape}: published agent={row['agent']!r}, which the app cannot "
        f"match against any desk it draws")
    assert row["desk_known"] is known
    assert row["asked_by"] == agent


# -- 2. the two buttons come off the payload ----------------------------------


def test_both_options_arrive_with_their_own_words(client, bus):
    """The card draws two buttons and the deck owns what they mean. A client
    that hardcodes them cannot be corrected without shipping a new build."""
    raise_one(bus)

    options = listed(client)[0]["options"]

    assert [o["reply"] for o in options] == ["done", "skipped"]
    for option in options:
        assert option["available"] is True
        assert option["summary"].strip(), (
            f"{option['reply']} has no summary -- a button with nothing under "
            f"it is how somebody picks the wrong one")


# -- 3. nothing secret leaves --------------------------------------------------


def test_a_handoff_carrying_a_code_publishes_it_redacted(client, bus):
    """Rule 3 of that module, enforced at the door that reaches a phone. This
    payload syncs to a second device; a live one-time code in it is the exact
    failure a secure handoff exists to prevent."""
    raise_one(
        bus,
        kind="2fa",
        needs="Type the code 417293 into the Duo prompt",
        state="the deploy is paused; password is hunter2 in the vault",
        where="https://admin.example.com/login?token=" + "abcdef0123456789",
        evidence="$ gh auth login\n! paste the code 417293\nBearer sk-ant-abc123def456",
    )

    row = listed(client)[0]
    blob = json.dumps(row)

    assert handoff.REDACTED in row["needs"], (
        "the one-time code went out verbatim in `needs`")
    assert "417293" not in blob, (
        "a live one-time code reached the phone payload -- everything that "
        "leaves here must go through handoff.redact()")
    assert "hunter2" not in blob
    assert "abcdef0123456789" not in blob
    assert "Duo prompt" in row["needs"], (
        "redaction must not eat the instruction: he still has to know what "
        "he is being asked to do")


# -- 4. answering it instructs the desk, and the two verbs differ --------------


def answer(client, handoff_id, outcome):
    return client.post(f"/v1/handoffs/{handoff_id}",
                       json={"reply": outcome}, headers=auth())


def test_done_orders_the_desk_to_re_check_that_it_worked(client, bus, delivered):
    """`done` is a human's CLAIM, not a fact: the 2FA may have timed out, the
    payment may have been declined. The desk is told to verify."""
    row = raise_one(bus)

    response = answer(client, row.id, "done")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["handoff"]["status"] == "done"
    assert body["resumed"] is True, (
        "the desk was never told, so it is still sitting there blocked -- "
        "which is the approvals defect all over again, one layer down")
    assert delivered, "nothing reached the live session"
    name, text = delivered[-1]
    assert name == "atlas"
    assert row.id in text, "the desk must know WHICH block was cleared"
    assert "verify" in text.lower(), (
        f"the resume message does not order a re-check: {text!r}")


def test_skipped_orders_the_desk_to_abandon_that_path(client, bus, delivered):
    """The other instruction, and it must not be the same one. An agent that
    reads a skip as retry-with-a-pause hammers a login screen until it locks."""
    row = raise_one(bus)

    response = answer(client, row.id, "skipped")

    assert response.status_code == 200, response.text
    assert response.json()["handoff"]["status"] == "skipped"
    name, text = delivered[-1]
    assert name == "atlas"
    assert "abandon" in text.lower() and "not retry" in text.lower(), (
        f"a skipped handoff must end that path, not schedule it again: "
        f"{text!r}")


def test_done_and_skipped_are_different_instructions(client, bus, delivered):
    """Rule 2, held directly: they are different instructions, not different
    words for one. This is the assertion that stops a later refactor from
    collapsing them into a single 'resolved' nudge."""
    first, second = raise_one(bus), raise_one(bus)

    answer(client, first.id, "done")
    done_text = delivered[-1][1]
    answer(client, second.id, "skipped")
    skipped_text = delivered[-1][1]

    # Compared with the ids and the verb removed. Comparing the raw strings
    # would pass on two messages that say the identical thing about two
    # different handoffs -- which is exactly the collapse this test exists to
    # catch, so it has to be the INSTRUCTION that is compared.
    def instruction(text, row_id):
        return re.sub(r"resolved: \w+", "", text.replace(row_id, "<id>"))

    assert (instruction(done_text, first.id)
            != instruction(skipped_text, second.id)), (
        "`done` and `skipped` sent the same instruction with a different "
        "label on it -- rule 2 collapsed, and a skipped handoff becomes an "
        "infinite retry loop")
    assert "not retry" not in done_text.lower(), (
        "`done` told the desk to abandon the path -- it must re-check it")


def test_a_resolved_handoff_leaves_the_board(client, bus):
    """It is not a card any more. Leaving it there invites a second answer to
    a question that is settled."""
    row = raise_one(bus)
    answer(client, row.id, "done")

    assert listed(client) == []


def test_a_second_answer_is_refused_rather_than_silently_taken(client, bus):
    """Two taps, a retried request, a Baileys replay of one WhatsApp reply --
    all ordinary. The second must not read as though it decided anything."""
    row = raise_one(bus)
    answer(client, row.id, "done")

    again = answer(client, row.id, "skipped")

    assert again.status_code == 409, again.text
    assert again.json()["reason"] == "already_resolved"


def test_an_unknown_id_is_a_404_and_a_bad_verb_is_a_400(client, bus):
    """A mistyped id must not resolve the wrong block, and `taken_over` is not
    on offer at this door -- it is the WhatsApp verb for 'I have the keyboard',
    and a tray button for it would be a third state with nothing to draw."""
    row = raise_one(bus)

    assert answer(client, "nope", "done").status_code == 404
    bad = answer(client, row.id, "taken_over")
    assert bad.status_code == 400, bad.text
    assert bad.json()["reason"] == "bad_outcome"
    assert listed(client)[0]["id"] == row.id, (
        "a refused answer must leave the handoff live and answerable")
