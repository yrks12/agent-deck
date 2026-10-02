"""Pixel mate test for OpenCode sessions.

OpenCode sessions carry source: "opencode". The sprite renderer should draw a
non-human robot sprite for them, while Claude Code sessions keep the existing
brown humanoid. This tests the renderer accepts source and that an opencode
card produces non-zero canvas pixels.
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
    ],
    "totals": {"sessions": 1, "working": 1, "needs_you": 0, "agents_running": 0},
    "plan": {},
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _StubHandler(http.server.SimpleHTTPRequestHandler):
    """Serve the deck assets with the same routes as the FastAPI app."""

    def do_GET(self):
        if self.path == "/api/state":
            self._send_json(CURRENT_STATE)
        elif self.path.startswith("/api/standing-approvals/audit"):
            self._send_json({"audit": []})
        elif self.path.startswith("/api/standing-approvals"):
            self._send_json({"policies": []})
        elif self.path == "/api/approvals":
            self._send_json({"approvals": []})
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


def test_mates_js_is_source_aware():
    """Static check: the renderer accepts source and branches for opencode."""
    js = (WEB / "mates.js").read_text()
    assert "function drawMate(g, u, ox, oy, p, source)" in js
    assert "source === \"opencode\"" in js
    assert "s.source" in js, "Cubicle/Floor must pass s.source"
    assert "seat.source" in js, "Office must pass seat.source"


def test_opencode_mate_canvas_has_ink(page, deck_url):
    """An opencode card renders a non-empty canvas sprite."""
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.goto(deck_url, wait_until="networkidle")
    page.wait_for_timeout(1200)

    card = page.query_selector('.card[data-session="op-1"]')
    assert card is not None, "opencode card not found"

    canvas = card.query_selector(".card__scene")
    assert canvas is not None, "opencode card has no canvas"

    pixels = canvas.evaluate(
        "c => {"
        "  const ctx = c.getContext('2d');"
        "  const d = ctx.getImageData(0, 0, c.width, c.height).data;"
        "  let n = 0;"
        "  for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;"
        "  return n;"
        "}"
    )
    assert pixels > 0, f"opencode mate canvas has no ink ({pixels} non-zero alpha pixels)"
    assert not errors, f"console errors: {errors}"
