"""The owner reads ONE thread: the desk's dispatch and the reply, inline.

Measured against the owner's real Grok Bot data on this Mac
(`~/Library/Application Support/Grok Bot/sand-client-persistence`,
`transcript.replicas.<agent-id>`). His COS conversation interleaves, in one
thread, in order:

    [19:26] COS:         Checking Villas opens and Acme ads.
    [19:26] to Villas:   Status now: any new page_views since event 307?
    [19:27] from Villas: page_views new: 0  replies: 0  max_event_id: 307
    [19:28] COS:         Villas just now: no new opens since River Cottage.

He never opens a second thread and never chases a worker. Our deck files that
traffic in `peer:` threads and shows the direct thread a "Messaged X: ..."
preview line instead, so the middle two lines above are unreachable from the
conversation he is reading.

These are presence detectors, not absence-of-error detectors: every one of
them asserts the dispatch AND the reply are *there*, attributed, in order.

The wire attribution under test is deliberately NOT a new vocabulary. A
relayed message keeps `thread_id` pointing at the `peer:` thread it belongs
to, which differs from the page's own `thread_id`; with `author` that is
everything a client needs to draw `to hemingway` / `from hemingway`. See
`docs/client-api.md` §6.2.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"

CHIEF = {"name": "chief", "cwd": "/tmp/p", "engine": "claude", "mission": "run it",
         "label": "Chief", "charter": "Run the board.", "reports_to": None}
HEMINGWAY = {"name": "hemingway", "cwd": "/tmp/p", "engine": "claude",
             "mission": "write", "label": "Villas", "charter": "Own the villas.",
             "reports_to": "chief"}
SCOUT = {"name": "scout", "cwd": "/tmp/p", "engine": "claude", "mission": "scout",
         "label": "Acme", "charter": "Own acme.", "reports_to": "chief"}


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_file(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps(
        {"version": 1, "agents": [CHIEF, HEMINGWAY, SCOUT]}))
    return path


@pytest.fixture
def surface(bus, roster_file):
    snapshot = {"generated_at": 1_756_000_100.0, "sessions": [
        {"session_id": "sid-chief", "pid": 4242, "name": "chief", "cwd": "/tmp/p",
         "project": "p", "state": "WORKING", "state_since": 1_756_000_000.0}]}
    return api_mod.Surface(
        snapshot=lambda: snapshot, comms=comms_mod.CommsIndex(),
        roster_path=roster_file, prefs_path=bus / "agent_prefs.json")


@pytest.fixture
def client(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    built = FastAPI()
    api_mod.register(built, surface=surface, background=False)
    return TestClient(built)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def queue(bus, sender, to, text, ts):
    """One office record, with a timestamp we choose.

    `office.send` stamps `time.time()`, which would sort every queued message
    after the fixed-timestamp transcript edges and make an ordering assertion
    meaningless. The record shape is `office.send`'s, field for field.
    """
    record = {"ts": ts, "id": f"m{int(ts)}", "to": to, "from": sender,
              "text": text}
    with (bus / "messages.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def peer_edge(index, sender, target, text, ts):
    """One agent-to-agent message as `TranscriptTail` emits it: both copies."""
    index.add({"dir": "out", "owner": f"sid-{sender}", "ts": ts,
               "to_label": target, "text": text})
    index.add({"dir": "in", "owner": f"sid-{target}", "ts": ts,
               "from_sock": f"uds:/tmp/cc-socks/{abs(hash(sender)) % 90000}.sock",
               "from_name": sender, "text": text})


def page(client, thread_id):
    response = client.get(f"/v1/threads/{thread_id}/messages", headers=auth())
    assert response.status_code == 200, response.text
    return response.json()


def sweep(bus, surface):
    """The COS sweep from the owner's real transcript, timestamps ours."""
    queue(bus, "owner", "chief", "any updates?", 1_756_000_010.0)
    queue(bus, "chief", "owner", "Checking Villas opens and Acme ads.",
          1_756_000_020.0)
    queue(bus, "chief", "hemingway",
          "Status now: any new page_views since event 307? Counts only.",
          1_756_000_030.0)
    peer_edge(surface.comms, "hemingway", "chief",
              "page_views new: 0  replies: 0  max_event_id: 307",
              1_756_000_040.0)
    queue(bus, "chief", "owner", "Villas just now: no new opens. No replies.",
          1_756_000_050.0)


# -- 1. the headline ---------------------------------------------------------


def test_the_dispatch_and_the_reply_are_in_the_managers_own_thread_in_order(
        bus, surface, client):
    """What the owner reads in `direct:chief` is the whole sweep, in order.

    Fails on current code: the two peer lines are filed under
    `peer:chief|hemingway` and never reach this thread.
    """
    sweep(bus, surface)
    surface.refresh()

    body = page(client, "direct:chief")
    got = [(m["author"], m["text"]) for m in body["messages"]]

    assert got == [
        ("owner", "any updates?"),
        ("chief", "Checking Villas opens and Acme ads."),
        ("chief",
         "Status now: any new page_views since event 307? Counts only."),
        ("hemingway", "page_views new: 0  replies: 0  max_event_id: 307"),
        ("chief", "Villas just now: no new opens. No replies."),
    ]


# -- 2. the wire attribution -------------------------------------------------


def test_a_relayed_line_names_the_peer_thread_it_came_from(bus, surface,
                                                           client):
    """`to hemingway` / `from hemingway` is derivable without a new field.

    A relayed message's own `thread_id` is the peer thread; the page's is
    `direct:chief`. Counterpart = the participant that is not this desk,
    direction = out when `author` is this desk. Nothing else in the payload
    changes, and the peer thread id is a deep link to the full record.
    """
    sweep(bus, surface)
    surface.refresh()

    body = page(client, "direct:chief")
    assert body["thread_id"] == "direct:chief"

    relayed = [m for m in body["messages"]
               if m["thread_id"] != body["thread_id"]]
    assert [(m["author"], m["thread_id"]) for m in relayed] == [
        ("chief", "peer:chief|hemingway"),
        ("hemingway", "peer:chief|hemingway"),
    ]
    # Everything the desk says to the owner still carries the page's own id,
    # so "distinct from what the desk says to the owner" is decidable.
    own = [m for m in body["messages"] if m["thread_id"] == "direct:chief"]
    assert [m["author"] for m in own] == ["owner", "chief", "chief"]


def test_the_relay_reaches_the_worker_thread_too_with_the_direction_flipped(
        bus, surface, client):
    """`direct:hemingway` carries the same two lines. Same ids, one record."""
    sweep(bus, surface)
    surface.refresh()

    body = page(client, "direct:hemingway")
    assert [(m["author"], m["thread_id"]) for m in body["messages"]] == [
        ("chief", "peer:chief|hemingway"),
        ("hemingway", "peer:chief|hemingway"),
    ]
    chief_side = {m["id"] for m in page(client, "direct:chief")["messages"]
                  if m["thread_id"] == "peer:chief|hemingway"}
    assert {m["id"] for m in body["messages"]} == chief_side


# -- 3. the firehose fence ---------------------------------------------------


def test_a_desk_thread_carries_only_its_own_two_party_traffic(bus, surface,
                                                              client):
    """Two of chief's reports talking to each other stays out of chief's chat.

    This is what stops a busy board becoming a firehose: a desk's thread grows
    with what that desk itself sent and received, not with its subtree's.
    """
    sweep(bus, surface)
    queue(bus, "hemingway", "scout", "what did the ad spend do?", 1_756_000_060.0)
    peer_edge(surface.comms, "scout", "hemingway", "flat, no change",
              1_756_000_070.0)
    surface.refresh()

    texts = [m["text"] for m in page(client, "direct:chief")["messages"]]
    assert "what did the ad spend do?" not in texts
    assert "flat, no change" not in texts
    # ...and it is not lost: it is inline for the two desks that are party.
    assert "what did the ad spend do?" in [
        m["text"] for m in page(client, "direct:scout")["messages"]]


def test_the_badge_equals_what_is_actually_unread_in_the_desks_own_thread(
        bus, surface, client):
    """The number on the card is the number of lines he has not read there.

    One peer message now lives in three of chief's threads -- the transcript
    and both parties' chats -- so a badge that adds buckets up counts a single
    dispatch three times. Expectation is computed from the thread the owner
    opens, never hard-coded, so this detector still bites if the relay changes
    shape.

    Fails on current code from the other side: today the peer traffic is
    counted in the badge but is nowhere in the thread that badge points at, so
    the two numbers are 4 and 2.
    """
    sweep(bus, surface)
    surface.refresh()

    agents = {a["name"]: a for a in
              client.get("/v1/agents", headers=auth()).json()["agents"]}
    thread = page(client, "direct:chief")["messages"]
    visible = [m for m in thread if m["author"] != "owner"]

    assert agents["chief"]["unread"] == len(visible)
    assert len(visible) == 4  # 2 to the owner, 1 dispatch, 1 reply


# -- 4. the peer thread is untouched ----------------------------------------


def test_the_peer_thread_is_still_the_full_record(bus, surface, client):
    """The separate transcript stays exactly as it was, and stays read-only."""
    sweep(bus, surface)
    surface.refresh()

    body = page(client, "peer:chief|hemingway")
    assert body["read_only"] is True
    assert [(m["author"], m["text"]) for m in body["messages"]] == [
        ("chief",
         "Status now: any new page_views since event 307? Counts only."),
        ("hemingway", "page_views new: 0  replies: 0  max_event_id: 307"),
    ]
    assert client.post("/v1/threads/peer:chief|hemingway/messages",
                       json={"text": "hi"},
                       headers=auth()).json()["reason"] == "thread_is_read_only"
