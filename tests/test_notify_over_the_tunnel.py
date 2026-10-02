"""The box's route to a phone: HTTP to a bridge it does not have locally.

MEASURED 2026-09-02 on 10.99.0.1:

    $ ls /home/deckop/Projects/comunicate_with_me/bin/wa-send.js
    No such file or directory

`notify.send` shells out to that script, so on the box it returns
`no such script` forever and nothing the deck does there ever reaches him. The
box cannot have its own bridge either: pairing is interactive, and a second
Baileys client on the same number is the configuration most likely to get the
number banned.

What it CAN do is talk HTTP to the Mac's bridge across the WireGuard tunnel --
the bridge's `POST /send` is an ordinary bearer-authenticated JSON route.

These tests run a real HTTP server on loopback and speak the bridge's actual
protocol to it. No WhatsApp message is sent, and nothing here touches the box.

Whether the ROUTE is open is a separate question and the answer today is no:
the Mac's bridge listens on 127.0.0.1:7799 only (measured -- `lsof -nP
-iTCP:7799` shows one loopback listener, and its .env says BOT_HOST=127.0.0.1),
so `curl http://10.99.0.4:7799/status` from the box fails to connect. This
module makes the deck ready for that link; it does not pretend the link is up.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from server import notify


class Bridge:
    """A stand-in speaking the real bridge's `/send` contract."""

    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else {"ok": True, "chunks": 1}
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length).decode()
                outer.seen.append({
                    "path": self.path,
                    "auth": self.headers.get("authorization"),
                    "body": json.loads(raw) if raw else {},
                })
                payload = json.dumps(outer.body).encode()
                self.send_response(outer.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_):
        self._server.shutdown()
        self._server.server_close()


def env_for(bridge, token="tunnel-token-not-a-real-one"):
    return {notify.ENV_URL: bridge.url, notify.ENV_TOKEN: token}


def test_with_a_url_configured_the_message_goes_over_http(tmp_path):
    """The good signal: the bridge receives the text on its own route, with
    the bearer it expects. No node, no local script, nothing on disk."""
    with Bridge() as bridge:
        result = notify.send("acme-growth is blocked", env=env_for(bridge))

    assert result.ok is True
    assert len(bridge.seen) == 1
    assert bridge.seen[0]["path"] == "/send"
    assert bridge.seen[0]["auth"] == "Bearer tunnel-token-not-a-real-one"
    assert bridge.seen[0]["body"]["text"] == "acme-growth is blocked"


def test_the_return_address_rides_the_session_block(tmp_path):
    """`src/server.js#normalizeSession` reads the reply address off the JSON
    body over HTTP, where the CLI reads it from env. Same routing, different
    door -- and without it a reply from the phone is unroutable again."""
    with Bridge() as bridge:
        notify.send("blocked", env=env_for(bridge),
                    reply_to={"socket": "/tmp/cc-socks/agentdeck.sock",
                              "session_id": "agent-deck"})

    session = bridge.seen[0]["body"]["session"]
    assert session["socket"] == "/tmp/cc-socks/agentdeck.sock"
    assert session["session_id"] == "agent-deck"


def test_a_bridge_that_refuses_is_never_reported_as_sent(tmp_path):
    """Same rule as the CLI's exit codes, over a different transport. A 401 is
    a misconfigured token, and calling it sent is how a question disappears."""
    with Bridge(status=401, body={"ok": False, "error": "unauthorized"}) as bridge:
        result = notify.send("anything", env=env_for(bridge))

    assert result.ok is False
    assert "401" in result.detail or "unauthorized" in result.detail.lower()


def test_an_unlinked_bridge_reads_as_unlinked_not_as_a_generic_failure(tmp_path):
    """The bridge answers 503 when its WhatsApp socket is unlinked. That is a
    re-pair-the-phone problem, not a deck problem, and the two need telling
    apart or the wrong thing gets debugged."""
    with Bridge(status=503, body={"ok": False, "state": "unlinked"}) as bridge:
        result = notify.send("anything", env=env_for(bridge))

    assert result.ok is False
    assert result.code == 4


def test_a_bridge_that_is_not_there_is_reported_as_the_daemon_being_down():
    """What the box gets TODAY if it is pointed at the Mac: the tunnel is up
    and nothing is listening, because that bridge binds loopback. It has to
    read as 'no daemon' (3), which is the code the runbook keys on."""
    # Port 1 on loopback: nothing binds it, and the refusal is immediate.
    result = notify.send("anything",
                         env={notify.ENV_URL: "http://127.0.0.1:1",
                              notify.ENV_TOKEN: "x"},
                         timeout=5)
    assert result.ok is False
    assert result.code == 3


def test_without_a_url_the_local_script_is_still_the_path(tmp_path):
    """The Mac must not change behaviour. It has the bridge locally and the
    subprocess path is the one that is proven there."""
    result = notify.send("anything",
                         env={notify.ENV_SCRIPT: str(tmp_path / "nope.js"),
                              notify.ENV_NODE: "/bin/sh"})
    assert result.ok is False
    assert "no such script" in result.detail
