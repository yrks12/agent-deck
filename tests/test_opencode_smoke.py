"""Smoke test for the OpenCode source badge in the Agent Deck UI.

The backend is adding source: "opencode" to opencode cards. This test verifies
that the frontend renders a source badge on both Claude and OpenCode sessions.
"""

from __future__ import annotations

import http.server
import json
import socket
import socketserver
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"

CURRENT_STATE = {
    "sessions": [
        {
            "session_id": "op-1",
            "name": "OpenCoder",
            "pid": 9001,
            "state": "WORKING",
            "source": "opencode",
            "project": "demo",
            "git_branch": "develop",
            "model": "kimi-k2.7-code",
            "activity": {"tool": "build", "running": True},
            "last_prompt": "",
            "last_assistant": "",
            "subagents": [],
            "usage": {"output": 12345},
            "state_since": 0,
        },
        {
            "session_id": "cc-1",
            "name": "Claude",
            "pid": 9002,
            "state": "IDLE",
            "project": "demo",
            "git_branch": "develop",
            "model": "claude-opus-5",
            "activity": {},
            "last_prompt": "",
            "last_assistant": "",
            "subagents": [],
            "usage": {"output": 0},
            "state_since": 0,
        },
    ],
    "totals": {"sessions": 2, "working": 1, "needs_you": 0, "agents_running": 0},
    "plan": {},
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _StubHandler(http.server.SimpleHTTPRequestHandler):
    """Serve the deck HTML/CSS/JS with the same routes as the FastAPI app.

    The real daemon mounts `/static` to the `web/` directory. The stub must do
    the same so the browser can load `app.js`, `style.css`, and `mates.js`.
    """

    def do_GET(self):
        if self.path == "/api/state":
            self._send_json(CURRENT_STATE)
        elif self.path == "/api/stream":
            self._send_sse(CURRENT_STATE)
        elif self.path == "/" or self.path == "/index.html":
            self._send_file(WEB / "index.html", "text/html")
        elif self.path.startswith("/static/"):
            self._send_file(WEB / self.path[len("/static/"):], None)
        else:
            self.send_error(404)

    def _send_file(self, path: Path, content_type: str | None):
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        if content_type:
            self.send_header("Content-Type", content_type)
        elif path.suffix == ".css":
            self.send_header("Content-Type", "text/css")
        elif path.suffix == ".js":
            self.send_header("Content-Type", "application/javascript")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, data):
        payload = f"data: {json.dumps(data)}\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)
        # Keep the connection open so the page does not reconnect; the test will
        # close the browser before the timeout.
        self.wfile.flush()

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def deck_url():
    port = _free_port()
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


@pytest.fixture
def page():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1200, "height": 800})
        yield pg
        browser.close()


def test_opencode_source_badge_renders(page, deck_url):
    """An opencode card from /api/stream shows the OpenCode source badge,
    while a legacy Claude card shows the Claude badge.
    """
    page.goto(deck_url, wait_until="networkidle")
    page.wait_for_timeout(1000)

    cards = page.query_selector_all(".card")
    assert len(cards) == 2, f"expected 2 cards, got {len(cards)}"

    opencode = page.query_selector('.card[data-session="op-1"]')
    assert opencode is not None, "opencode card not found"
    badge = opencode.query_selector(".card__source")
    assert badge is not None, "opencode card has no source badge"
    assert badge.text_content() == "OpenCode", (
        f"opencode badge text is {badge.text_content()!r}"
    )

    claude = page.query_selector('.card[data-session="cc-1"]')
    assert claude is not None, "Claude card not found"
    badge = claude.query_selector(".card__source")
    assert badge is not None, "Claude card has no source badge"
    assert badge.text_content() == "Claude", (
        f"Claude badge text is {badge.text_content()!r}"
    )


def test_opencode_card_handles_empty_text_fields(page, deck_url):
    """Opencode cards may arrive with empty prompt/assistant/subagents; the
    card must still render without looking broken.
    """
    sparse = {
        "sessions": [
            {
                "session_id": "op-2",
                "name": "OpenCoder-sparse",
                "pid": 9003,
                "state": "IDLE",
                "source": "opencode",
                "project": "",
                "git_branch": "",
                "model": "",
                "activity": {"tool": "opencode"},
                "last_prompt": "",
                "last_assistant": "",
                "subagents": [],
                "usage": {"output": 0},
                "state_since": 0,
            }
        ],
        "totals": {"sessions": 1, "working": 0, "needs_you": 0, "agents_running": 0},
        "plan": {},
    }

    # Replace the global stub so the server serves the sparse card.
    global CURRENT_STATE
    CURRENT_STATE = sparse

    page.goto(deck_url, wait_until="networkidle")
    page.wait_for_timeout(1000)

    card = page.query_selector('.card[data-session="op-2"]')
    assert card is not None, "sparse opencode card not found"
    assert card.query_selector(".card__source") is not None
    assert card.query_selector(".card__source").text_content() == "OpenCode"


def test_markup_mentions_generic_sessions():
    """The empty-state text should not assume the only source is Claude."""
    js = (WEB / "app.js").read_text()
    assert "no live sessions" in js, "empty state still mentions Claude explicitly"
    assert "no live claude sessions" not in js, "empty state still says 'claude'"
