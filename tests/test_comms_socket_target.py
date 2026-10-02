"""A message sent to a socket belongs to the desk on the other end of it.

**The defect (D4).** `SendMessage` takes either a name or Claude Code's own
`uds:/tmp/cc-socks/<pid>.sock` form -- 17,167 sends on this machine use the
socket form. The extractor resolves the *sender* through that pid and has done
since it was written (`pid_from_sock` is right there), but the *target* was only
ever looked up by name, so the socket string became the peer's identity.

Measured live off `GET /api/comms`, before this change:

    "to": { "session_id": null,
            "name": "uds:/tmp/cc-socks/3926.sock",
            "pid": null }

Two things fall out of that, and both were measured on the running deck:

1. The board shows a correspondent called `uds:/tmp/cc-socks/3926.sock`. There
   is a desk on that socket with a name, and the owner is shown a path.
2. The thread id built from it is `peer:orion-46|uds:/tmp/cc-socks/3926.sock`.
   `GET /v1/threads` lists it and `GET /v1/threads/{id}/messages` answers 404 --
   raw *and* percent-encoded, because Starlette decodes `%2F` before matching
   and a path parameter cannot hold a `/`. A row the owner can see, tap, and
   never open.

**Why this is on the trail of the rename fix, not beside it.** The deck now
briefs every new hire with its boss's `uds:` address, because a name is not an
address once a desk can rename itself. That is correct and it multiplies this
fault: every junior that reports upward would file its reports under a socket
path. The address is the right thing to *send* to and the wrong thing to *file*
under, and this is the half that puts the name back.

**The GOOD signal.** The edge resolves to the desk's session and current name,
and the thread it lands in opens over HTTP and contains the message. Not "the
id has no slash in it" -- an id can be slash-free and still be the wrong desk.

The route/encoding fault is left alone here on purpose: a participant that is
neither a name nor a socket -- a subagent id, say -- still produces an id this
route cannot serve. Measured on the same board: 24 of 25 peer threads carry an
opaque participant and 23 of those are subagent ids, a different fault with a
different fix. This one closes the socket class, which is the class the rename
work creates.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

BOSS_PID = 3926
SOCKET = f"uds:/tmp/cc-socks/{BOSS_PID}.sock"
#: The boss after it named itself. The card carries the current name because
#: `onboard.reseat` has already run on the collector's tick.
BOSS = {"session_id": "sid-boss", "name": "drift-watch", "pid": BOSS_PID,
        "cwd": "/tmp", "state": "IDLE"}
JUNIOR = {"session_id": "sid-junior", "name": "branch-scout", "pid": 4001,
          "cwd": "/tmp", "state": "IDLE"}


def out_record(to_label, *, owner="sid-junior", text="reporting in", ts=100.0):
    """One outbound SendMessage, in the shape `TranscriptTail` emits."""
    return {"dir": "out", "owner": owner, "to_label": to_label,
            "text": text, "ts": ts, "key": "k1", "seen": False}


def edges_for(to_label, cards=(BOSS, JUNIOR)):
    index = comms_mod.CommsIndex()
    index.add(out_record(to_label))
    return index.edges(comms_mod.build_directory(list(cards)))


# -- the detector ------------------------------------------------------------


def test_a_message_sent_to_a_socket_is_filed_against_the_desk_on_it():
    """THE detector. The pid is right there in the address the CLI itself
    published; the desk it belongs to is on the board."""
    edge = edges_for(SOCKET)[0]

    assert edge["to"]["name"] == "drift-watch"
    assert edge["to"]["session_id"] == "sid-boss"
    assert edge["to"]["pid"] == BOSS_PID


def test_a_socket_nobody_is_on_keeps_its_label_rather_than_vanishing():
    """The rule this extractor lives by: an endpoint we cannot resolve keeps
    whatever it came with. Dropping it would silently shrink the graph, which
    is the one thing a view of who-said-what-to-whom must never do."""
    edge = edges_for("uds:/tmp/cc-socks/999999.sock")[0]

    assert edge["to"]["name"] == "uds:/tmp/cc-socks/999999.sock"


def test_a_plain_name_still_resolves_exactly_as_it_did():
    """The blast radius. Most traffic addresses a name, and it must be
    untouched by teaching this the socket form."""
    edge = edges_for("drift-watch [83310a]")[0]

    assert edge["to"]["name"] == "drift-watch"
    assert edge["to"]["session_id"] == "sid-boss"


def test_the_thread_it_lands_in_can_actually_be_opened(tmp_path, monkeypatch):
    """The consequence, end to end. A row the owner can see and tap and never
    open is worse than no row: it reads as a bug in the app."""
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    (tmp_path / "roster.json").write_text(json.dumps({"version": 1,
                                                      "agents": []}))
    index = comms_mod.CommsIndex()
    index.add(out_record(SOCKET))

    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": [BOSS, JUNIOR]},
        comms=index, roster_path=tmp_path / "roster.json",
        prefs_path=tmp_path / "agent_prefs.json")
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    client = TestClient(app)

    listed = [t["id"] for t in client.get("/v1/threads", headers=AUTH).json()
              ["threads"] if t["id"].startswith("peer:")]
    assert listed, "the message produced no peer thread at all"
    thread_id = listed[0]
    assert "/" not in thread_id, thread_id

    opened = client.get(f"/v1/threads/{thread_id}/messages", headers=AUTH)
    assert opened.status_code == 200, opened.text
    assert any("reporting in" in m["text"]
               for m in opened.json()["messages"]), opened.text
