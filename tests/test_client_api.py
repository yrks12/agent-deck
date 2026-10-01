"""The /v1 client surface: what a native macOS / iOS app is allowed to assume.

These tests are the contract. Two of them exist because they are the failures
that would only show up on a real phone, days later, as "messages sometimes
vanish" and "the app stalls every time I open it":

* `test_paging_backwards_while_messages_arrive_sees_every_message_exactly_once`
  fails for any offset-based pager. Offsets shift under an append, so page two
  skips exactly as many messages as arrived between the two requests.
* `test_the_sidebar_cost_does_not_scale_with_message_history` fails the moment
  `GET /v1/agents` starts reading transcripts or re-reading the whole message
  log. That endpoint runs on every app foreground.

Everything is hermetic: a tmp bus dir, a hand-written snapshot, no daemon, no
network, no real session, no write anywhere near the real ~/.claude/agent-bus.
"""

import asyncio
import json
import math
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"

CHIEF = {
    "name": "chief", "cwd": "/tmp/p", "engine": "claude", "mission": "run it",
    "label": "Negotiator", "charter": "Own the deal.", "reports_to": None,
}
HEMINGWAY = {
    "name": "hemingway", "cwd": "/tmp/p", "engine": "claude", "mission": "write",
    "label": "Researcher", "charter": "Own the words.", "reports_to": "chief",
}


@pytest.fixture
def bus(tmp_path, monkeypatch):
    """A private agent-bus: roster, prefs and message log, all under tmp_path."""
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_file(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [CHIEF, HEMINGWAY]}))
    return path


@pytest.fixture
def snapshot():
    """One session seated at `chief`; `hemingway` has nobody at its desk."""
    return {
        "generated_at": 1_756_000_100.0,
        "sessions": [{
            "session_id": "sid-chief", "pid": 4242, "name": "chief",
            "cwd": "/tmp/p", "project": "p", "state": "WORKING",
            "state_since": 1_756_000_000.0,
        }],
    }


@pytest.fixture
def surface(bus, roster_file, snapshot):
    return api_mod.Surface(
        snapshot=lambda: snapshot,
        comms=comms_mod.CommsIndex(),
        roster_path=roster_file,
        prefs_path=bus / "agent_prefs.json",
    )


@pytest.fixture
def app(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    built = FastAPI()
    api_mod.register(built, surface=surface, background=False)
    return built


@pytest.fixture
def client(app):
    return TestClient(app)


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def peer_edge(index, sender, target, text, ts):
    """One agent-to-agent message, in the shape `TranscriptTail` emits.

    Both copies, because both really are on disk: the sender's `SendMessage`
    tool_use and the receiver's `<cross-session-message>` block. Feeding only
    one leaves the sender unidentifiable whenever its session is not live,
    which is exactly the case an OFFLINE desk puts us in.
    """
    index.add({"dir": "out", "owner": f"sid-{sender}", "ts": ts,
               "to_label": target, "text": text})
    index.add({"dir": "in", "owner": f"sid-{target}", "ts": ts,
               "from_sock": f"uds:/tmp/cc-socks/{abs(hash(sender)) % 90000}.sock",
               "from_name": sender, "text": text})


# -- auth: fail closed -------------------------------------------------------


def test_v1_fails_closed_when_no_token_is_configured(surface, monkeypatch):
    monkeypatch.delenv(api_mod.TOKEN_ENV, raising=False)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    response = TestClient(app).get("/v1/agents", headers=auth())
    assert response.status_code == 503
    assert response.json()["reason"] == "auth_not_configured"


def test_an_unauthenticated_request_is_refused(client):
    response = client.get("/v1/agents")
    assert response.status_code == 401
    assert response.json()["reason"] == "unauthorized"
    assert client.get("/v1/agents", headers=auth("wrong")).status_code == 401


def test_a_correct_token_really_does_get_the_sidebar(client):
    response = client.get("/v1/agents", headers=auth())
    assert response.status_code == 200
    names = [a["name"] for a in response.json()["agents"]]
    assert names == ["chief", "hemingway"]


# -- the sidebar -------------------------------------------------------------


def test_a_desk_with_nobody_at_it_still_appears_offline(client):
    agents = {a["name"]: a
              for a in client.get("/v1/agents", headers=auth()).json()["agents"]}
    assert agents["chief"]["state"] == "WORKING"
    assert agents["hemingway"]["state"] == "OFFLINE"


def test_every_sidebar_row_carries_what_the_row_draws(client):
    row = client.get("/v1/agents", headers=auth()).json()["agents"][0]
    for key in ("name", "label", "section", "avatar", "state", "unread",
                "last_activity_at", "preview", "thread_id", "pinned",
                "notifications"):
        assert key in row, key
    assert row["label"] == "Negotiator"
    # A desk row has to carry the live session's project too. `occupancy` joins
    # only state/session_id/pid, so this is the field that silently comes back
    # empty if the live card is not consulted.
    assert row["project"] == "p"


def test_the_preview_line_is_derived_from_agent_to_agent_traffic(surface, client):
    peer_edge(surface.comms, "chief", "hemingway", "draft the counter-offer",
              1_756_000_050.0)
    agents = {a["name"]: a
              for a in client.get("/v1/agents", headers=auth()).json()["agents"]}
    assert agents["chief"]["preview"] == "Messaged hemingway: draft the counter-offer"
    assert agents["hemingway"]["preview"] == "Message from chief: draft the counter-offer"


def test_a_message_from_yair_previews_as_the_owners_own_line(bus, client):
    office.send("chief", "ship it")
    agents = {a["name"]: a
              for a in client.get("/v1/agents", headers=auth()).json()["agents"]}
    assert agents["chief"]["preview"] == "You: ship it"


# -- settings ----------------------------------------------------------------


def test_the_settings_payload_is_what_the_settings_panel_draws(client):
    body = client.get("/v1/agents/hemingway", headers=auth()).json()
    assert body["name"] == "hemingway"
    assert body["label"] == "Researcher"
    assert body["charter"] == "Own the words."
    assert body["boss"] == "chief"
    assert body["reports"] == []
    assert body["notifications"] is True
    assert "avatar" in body


def test_an_unknown_agent_is_a_404(client):
    response = client.get("/v1/agents/nobody", headers=auth())
    assert response.status_code == 404
    assert response.json()["reason"] == "unknown_agent"


def test_patch_persists_label_section_and_the_notifications_toggle(client, roster_file):
    response = client.patch(
        "/v1/agents/hemingway", headers=auth(),
        json={"label": "Designer", "section": "Personal", "notifications": False,
              "avatar": "hemingway.png"},
    )
    assert response.status_code == 200
    again = client.get("/v1/agents/hemingway", headers=auth()).json()
    assert again["label"] == "Designer"
    assert again["notifications"] is False
    assert again["avatar"] == "hemingway.png"
    saved = {d["name"]: d for d in json.loads(roster_file.read_text())["agents"]}
    assert saved["hemingway"]["label"] == "Designer"
    row = {a["name"]: a
           for a in client.get("/v1/agents", headers=auth()).json()["agents"]}
    assert row["hemingway"]["section"] == "Personal"


def test_patch_refuses_a_reports_to_change_because_that_is_the_org_chart(client):
    response = client.patch("/v1/agents/hemingway", headers=auth(),
                            json={"reports_to": "nobody"})
    assert response.status_code == 409
    assert response.json()["reason"] == "reports_to_is_not_a_setting"
    assert client.get("/v1/agents/hemingway", headers=auth()).json()["boss"] == "chief"


# -- threads -----------------------------------------------------------------


def test_a_peer_thread_is_flagged_read_only_in_its_own_payload(surface, client):
    peer_edge(surface.comms, "chief", "hemingway", "your turn", 1_756_000_050.0)
    threads = {t["id"]: t
               for t in client.get("/v1/threads", headers=auth()).json()["threads"]}
    peer = threads["peer:chief|hemingway"]
    assert peer["kind"] == "peer"
    assert peer["read_only"] is True
    assert peer["title"] == "chief ⇄ hemingway"
    assert sorted(peer["participants"]) == ["chief", "hemingway"]
    assert threads["direct:chief"]["read_only"] is False


def test_posting_to_a_peer_thread_is_409_not_a_silent_drop(surface, client):
    peer_edge(surface.comms, "chief", "hemingway", "your turn", 1_756_000_050.0)
    response = client.post("/v1/threads/peer:chief|hemingway/messages",
                           headers=auth(), json={"text": "let me in"})
    assert response.status_code == 409
    assert response.json()["reason"] == "thread_is_read_only"


def test_posting_to_a_direct_thread_appends_a_real_message(bus, client):
    response = client.post("/v1/threads/direct:chief/messages", headers=auth(),
                           json={"text": "status please"})
    assert response.status_code == 201
    created = response.json()["message"]
    assert created["author"] == "owner"
    assert created["text"] == "status please"
    listed = client.get("/v1/threads/direct:chief/messages",
                        headers=auth()).json()
    assert [m["id"] for m in listed["messages"]] == [created["id"]]
    assert bus.joinpath("messages.jsonl").read_text().strip() != ""


def test_an_empty_send_is_a_400(client):
    response = client.post("/v1/threads/direct:chief/messages", headers=auth(),
                           json={"text": "   "})
    assert response.status_code == 400
    assert response.json()["reason"] == "empty_text"


def test_messages_come_back_oldest_first_with_stable_ids(client):
    for n in range(3):
        client.post("/v1/threads/direct:chief/messages", headers=auth(),
                    json={"text": f"m{n}"})
    first = client.get("/v1/threads/direct:chief/messages",
                       headers=auth()).json()["messages"]
    second = client.get("/v1/threads/direct:chief/messages",
                        headers=auth()).json()["messages"]
    assert [m["text"] for m in first] == ["m0", "m1", "m2"]
    assert [m["id"] for m in first] == [m["id"] for m in second]
    assert [m["cursor"] for m in first] == sorted(m["cursor"] for m in first)


# -- pagination: the mobile-client killer ------------------------------------


def test_paging_backwards_while_messages_arrive_sees_every_message_exactly_once(client):
    """The detector for an offset-based pager.

    A client scrolls up through history while the agent keeps talking. With
    offsets, every append shifts the window and page N+1 skips exactly as many
    messages as arrived. With a cursor it cannot: the cursor names a message,
    not a position.
    """
    for n in range(12):
        client.post("/v1/threads/direct:chief/messages", headers=auth(),
                    json={"text": f"old-{n:02d}"})
    page = client.get("/v1/threads/direct:chief/messages?limit=4",
                      headers=auth()).json()
    seen = [m["id"] for m in page["messages"]]
    assert len(set(seen)) == 4

    arrived = 0
    # Bounded on purpose: an offset-based pager never runs out of "before",
    # so without this the bug shows up as a hung test instead of a failing one.
    for _ in range(10):
        if not page["has_more_before"]:
            break
        cursor = page["messages"][0]["cursor"]
        # A new message lands between the two page requests.
        client.post("/v1/threads/direct:chief/messages", headers=auth(),
                    json={"text": f"new-{arrived}"})
        arrived += 1
        page = client.get(
            f"/v1/threads/direct:chief/messages?limit=4&before={cursor}",
            headers=auth(),
        ).json()
        seen.extend(m["id"] for m in page["messages"])
        assert page["messages"] == sorted(page["messages"],
                                          key=lambda m: m["cursor"])
    else:
        raise AssertionError(
            "paging backwards never reached the start of the thread -- the "
            "window is moving with the appends, which is what an offset does"
        )

    assert arrived >= 2, "the test must actually append while paging"
    assert len(seen) == len(set(seen)), "a message was returned twice"
    listed = client.get("/v1/threads/direct:chief/messages?limit=200",
                        headers=auth()).json()["messages"]
    olds = {m["id"] for m in listed if m["text"].startswith("old-")}
    assert olds <= set(seen), "a message was skipped while paging backwards"


def test_since_is_a_cursor_that_never_replays_and_never_skips(client):
    for n in range(5):
        client.post("/v1/threads/direct:chief/messages", headers=auth(),
                    json={"text": f"a{n}"})
    head = client.get("/v1/threads/direct:chief/messages?limit=2",
                      headers=auth()).json()
    cursor = head["next_since"]
    seen = [m["id"] for m in head["messages"]]
    for n in range(3):
        client.post("/v1/threads/direct:chief/messages", headers=auth(),
                    json={"text": f"b{n}"})
        page = client.get(
            f"/v1/threads/direct:chief/messages?since={cursor}&limit=2",
            headers=auth()).json()
        seen.extend(m["id"] for m in page["messages"])
        if page["messages"]:
            cursor = page["next_since"]
    assert len(seen) == len(set(seen))
    assert len(seen) >= 5


def test_a_malformed_cursor_is_a_400_not_a_500(client):
    response = client.get("/v1/threads/direct:chief/messages?since=banana",
                          headers=auth())
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_cursor"


@pytest.mark.parametrize("limit", ["0", "9999", "-3"])
def test_an_out_of_range_limit_is_clamped_not_a_foreign_error_shape(client, limit):
    """Every refusal on this API is {ok, reason, detail}. A 422 from the
    framework's own validator is a shape no documented client handles."""
    for n in range(3):
        client.post("/v1/threads/direct:chief/messages", headers=auth(),
                    json={"text": f"m{n}"})
    response = client.get(f"/v1/threads/direct:chief/messages?limit={limit}",
                          headers=auth())
    assert response.status_code == 200
    assert 1 <= len(response.json()["messages"]) <= 3


def test_a_cursor_that_is_not_even_a_string_is_a_400(client):
    response = client.post("/v1/agents/chief/read", headers=auth(),
                           json={"cursor": {"nice": "try"}})
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_cursor"


def test_an_unknown_thread_is_a_404(client):
    response = client.get("/v1/threads/direct:nobody/messages", headers=auth())
    assert response.status_code == 404
    assert response.json()["reason"] == "unknown_thread"


# -- cost: this runs on every app foreground ---------------------------------


def _spy_on_bus_reads(monkeypatch, bus):
    """Record every file opened under the private bus dir, by filename."""
    opened: list[str] = []
    real_open = pathlib.Path.open

    def spy_open(self, *args, **kwargs):
        if str(bus) in str(self):
            opened.append(self.name)
        return real_open(self, *args, **kwargs)

    # `Path.read_text` goes through `Path.open`, so one spy catches both and
    # counts each logical read once.
    monkeypatch.setattr(pathlib.Path, "open", spy_open)
    return opened


@pytest.mark.parametrize("history", [4, 400])
def test_the_sidebar_cost_does_not_scale_with_message_history(
    client, bus, monkeypatch, history
):
    """`GET /v1/agents` must cost the same with 4 messages and with 400.

    The failing shape this pins: computing a preview by reading each agent's
    transcript, or re-reading the whole message log per request. Both are
    invisible on a laptop and a visible stall on a phone.
    """
    for n in range(history):
        office.send("chief", f"line {n}")
    client.get("/v1/agents", headers=auth())  # warm the incremental reader

    opened = _spy_on_bus_reads(monkeypatch, bus)
    assert client.get("/v1/agents", headers=auth()).status_code == 200

    assert "messages.jsonl" not in opened, (
        "the message log was re-read from the top on a warm request"
    )
    assert len(opened) <= 2, f"too many file reads for one sidebar: {opened}"


# -- unread ------------------------------------------------------------------


def test_read_clears_unread_up_to_a_message_id(surface, client):
    peer_edge(surface.comms, "hemingway", "chief", "first", 1_756_000_010.0)
    peer_edge(surface.comms, "hemingway", "chief", "second", 1_756_000_020.0)
    agents = {a["name"]: a
              for a in client.get("/v1/agents", headers=auth()).json()["agents"]}
    assert agents["chief"]["unread"] == 2

    messages = client.get("/v1/threads/peer:chief|hemingway/messages",
                          headers=auth()).json()["messages"]
    response = client.post("/v1/agents/chief/read", headers=auth(),
                           json={"up_to": messages[0]["id"]})
    assert response.status_code == 200
    agents = {a["name"]: a
              for a in client.get("/v1/agents", headers=auth()).json()["agents"]}
    assert agents["chief"]["unread"] == 1


def test_the_owners_own_messages_never_count_as_unread(bus, client):
    office.send("chief", "one")
    office.send("chief", "two")
    agents = {a["name"]: a
              for a in client.get("/v1/agents", headers=auth()).json()["agents"]}
    assert agents["chief"]["unread"] == 0


# -- serialisation -----------------------------------------------------------


def _reject(token):
    raise AssertionError(f"non-finite constant in the payload: {token}")


def test_no_payload_carries_nan_or_infinity(snapshot, client):
    """A strict mobile JSON parser rejects NaN. Python's default emits it."""
    snapshot["sessions"][0]["state_since"] = float("nan")
    body = client.get("/v1/agents", headers=auth()).text
    assert "NaN" not in body and "Infinity" not in body
    json.loads(body, parse_constant=_reject)


def test_timestamps_are_epoch_seconds_as_floats(client):
    client.post("/v1/threads/direct:chief/messages", headers=auth(),
                json={"text": "hi"})
    message = client.get("/v1/threads/direct:chief/messages",
                         headers=auth()).json()["messages"][0]
    assert isinstance(message["ts"], float)
    assert 1_600_000_000 < message["ts"] < 4_000_000_000
    assert math.isfinite(message["ts"])


def test_a_message_exposes_its_artefacts_for_the_bubble(client):
    client.post("/v1/threads/direct:chief/messages", headers=auth(),
                json={"text": "see /tmp/shot.png and https://example.com/x"})
    message = client.get("/v1/threads/direct:chief/messages",
                         headers=auth()).json()["messages"][0]
    kinds = {(a["kind"], a["value"]) for a in message["attachments"]}
    assert ("image", "/tmp/shot.png") in kinds
    assert ("link", "https://example.com/x") in kinds


# -- the stream --------------------------------------------------------------


async def _drain_stream(app, seconds=0.4):
    """Read `/v1/stream` by driving the ASGI app directly.

    Not TestClient: this Starlette's test transport buffers a whole response
    before returning, so it can never return from an endpoint that is
    deliberately endless. Talking ASGI is the only way to observe an SSE frame
    in-process, and it is also closer to what a phone does -- it hangs up, and
    the server has to notice.
    """
    start: dict = {}
    events: list[dict] = []

    async def receive():
        await asyncio.sleep(seconds)
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            for line in message.get("body", b"").decode().splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "path": "/v1/stream", "raw_path": b"/v1/stream",
        "root_path": "", "scheme": "http", "query_string": b"",
        "headers": [(b"host", b"testserver"),
                    (b"authorization", f"Bearer {TOKEN}".encode())],
        "client": ("127.0.0.1", 5000), "server": ("testserver", 80),
    }, receive, send)
    return start, events


def test_the_stream_opens_with_a_snapshot_and_keeps_a_heartbeat(app, monkeypatch):
    monkeypatch.setattr(api_mod, "HEARTBEAT_SECONDS", 0.05)
    start, events = asyncio.run(_drain_stream(app))
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert events[0]["type"] == "hello"
    assert [a["name"] for a in events[0]["agents"]] == ["chief", "hemingway"]
    assert [t["id"] for t in events[0]["threads"]]
    assert len(events) >= 2, "a quiet stream must still prove it is alive"
    assert all(e["type"] == "heartbeat" for e in events[1:])
    assert all(isinstance(e["ts"], float) for e in events)


def test_a_refresh_reports_what_changed_so_the_stream_can_be_incremental(
    bus, surface
):
    """The SSE event shapes, without a socket in the way."""
    surface.refresh()
    office.send("chief", "look at this")
    events = surface.refresh()
    kinds = {e["type"] for e in events}
    assert "message" in kinds
    message = next(e for e in events if e["type"] == "message")
    assert message["thread_id"] == "direct:chief"
    assert message["read_only"] is False
    assert message["message"]["text"] == "look at this"
    assert surface.refresh() == [], "a quiet tick must emit nothing"


def test_an_agent_going_offline_is_an_event_not_a_silent_disappearance(
    bus, surface, snapshot
):
    surface.refresh()
    snapshot["sessions"] = []
    events = surface.refresh()
    assert {"type": "agent_state", "name": "chief", "state": "OFFLINE"}.items() <= (
        next(e for e in events if e["type"] == "agent_state").items()
    )


def test_the_stream_is_behind_the_same_token(client):
    with client.stream("GET", "/v1/stream") as response:
        assert response.status_code == 401
