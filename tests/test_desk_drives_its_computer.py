"""A desk must KNOW it has a computer and be ABLE to drive it -- its own only.

MEASURED on the box, 2026-09-28: desk atlas has had `deck-desk-atlas` (Xvfb
:99 + Chromium) up for 21 days on the same page, and across every transcript
on the box there are **zero** uses of xdotool, `docker exec` or chromium.
`hire.brief` never mentioned the computer, and nothing gave a desk a way in:
its Claude runs on the HOST and the DevTools port lives in the container's
private network namespace. The owner: "do they know they can use it?"

The way in is a per-desk MCP server the deck writes into the spawn argv, with
the desk's name bound THERE -- not a tool argument, so no call the model makes
can aim it at another desk's container. Everything it runs leaves through
`sandbox.exec_argv` (one container, uid 1000, display :99), and DevTools is
reached over `docker exec` to 127.0.0.1 inside that container, never published.

Hermetic: no Docker, no Chromium. The one fake is the byte pipe / runner.
"""

import json
from pathlib import Path

import pytest

from server import computer_mcp, desk_computer, hire, sandbox, spawn
from server.roster import Desk

DESK = "acme"


def a_desk(name=DESK):
    return Desk(name=name, cwd="/tmp", engine="claude", mission="m",
                label="Ops", charter="You run ops.", reports_to=None)


# ── it KNOWS ────────────────────────────────────────────────────────────────


def test_the_brief_says_it_has_a_computer_and_how_to_drive_it():
    text = hire.brief(a_desk())
    assert "Your computer" in text
    for tool in ("navigate", "read_page", "screenshot", "click", "type_text",
                 "press_key"):
        assert f"mcp__computer__{tool}" in text, tool
    lowered = text.lower()
    assert "login" in lowered and "payment" in lowered
    assert "take it over" in lowered or "take over" in lowered


# ── it CAN, and only its own ─────────────────────────────────────────────────


def _mcp_config(argv):
    i = argv.index("--mcp-config")
    # The value is a FILE (tests/test_connectors_reload.py): its content.
    return json.loads(Path(argv[i + 1]).read_text()), argv[i + 2]


def test_a_claude_desk_is_spawned_with_its_computer_bound_to_its_own_name():
    argv = spawn.build_argv(a_desk(), background=False, seed="hello")
    config, after = _mcp_config(argv)
    server = config["mcpServers"]["computer"]
    assert server["args"][-2:] == ["--desk", DESK]
    # `--mcp-config` is variadic: a positional after it would be eaten as a
    # second config. A flag must follow, and the seed must still be last.
    assert after.startswith("--")
    assert argv[-1] == "hello"


def test_no_tool_takes_a_desk_argument():
    tools = computer_mcp.handle(DESK, {"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/list"})["result"]
    names = {t["name"] for t in tools["tools"]}
    assert names == {"navigate", "read_page", "screenshot", "click",
                     "type_text", "press_key", "type_password",
                     "upload_file", "mobile_mode"}
    for tool in tools["tools"]:
        assert "desk" not in json.dumps(tool["inputSchema"]).lower()


def test_a_call_naming_another_desk_still_acts_only_on_its_own(monkeypatch):
    ran = []
    monkeypatch.setattr(desk_computer, "_guard", lambda desk: None)
    monkeypatch.setattr(sandbox, "_run", lambda argv, **kw: ran.append(argv)
                        or type("P", (), {"returncode": 0, "stdout": b"",
                                          "stderr": b""})())
    computer_mcp.handle(DESK, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": "click", "arguments": {
                                   "x": 10, "y": 10, "desk": "harbor"}}})
    assert ran, "the click never ran"
    for argv in ran:
        assert "deck-desk-acme" in argv
        assert not any("harbor" in part for part in argv)


def test_devtools_is_reached_only_on_loopback_inside_its_own_container():
    argv = desk_computer.relay_argv(DESK)
    assert argv[:3] == ["docker", "exec", "--interactive"]
    assert "deck-desk-acme" in argv
    script = argv[-1]
    assert "/dev/tcp/127.0.0.1/9222" in script
    sandbox._sweep(argv)  # no publish, no docker.sock
    assert "--env" in argv and "DISPLAY=:99" in argv  # never :0


def test_a_target_that_is_not_its_own_loopback_page_is_refused():
    good = "ws://127.0.0.1:9222/devtools/page/AB12-cd"
    assert desk_computer.target_path(good) == "/devtools/page/AB12-cd"
    for bad in ("ws://10.0.0.5:9222/devtools/page/AB",
                "ws://127.0.0.1:9333/devtools/page/AB",
                "ws://127.0.0.1:9222/devtools/browser/AB",
                "ws://127.0.0.1:9222/devtools/page/AB\r\nX: y"):
        with pytest.raises(Exception):
            desk_computer.target_path(bad)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)",
                                 "chrome://settings", "data:text/html,x", ""])
def test_navigate_refuses_anything_but_the_web_before_touching_docker(
        url, monkeypatch):
    monkeypatch.setattr(sandbox, "_run", lambda *a, **k: pytest.fail("ran"))
    with pytest.raises(ValueError):
        desk_computer.navigate(DESK, url)


def test_a_key_is_still_guarded_against_xdotool_options(monkeypatch):
    monkeypatch.setattr(desk_computer, "_guard", lambda desk: None)
    monkeypatch.setattr(sandbox, "_run", lambda *a, **k: pytest.fail("ran"))
    reply = computer_mcp.handle(DESK, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "press_key", "arguments": {"key": "--file"}}})
    assert reply["result"]["isError"] is True


def test_it_stops_at_a_login_rather_than_typing_into_it(monkeypatch):
    monkeypatch.setattr(desk_computer, "_needs_human", lambda desk: "login")
    monkeypatch.setattr(desk_computer, "ensure", lambda d: (_ for _ in ()).throw(
        sandbox.SandboxError("cdp_unreachable", "no browser")))
    monkeypatch.setattr(sandbox, "_run", lambda *a, **k: pytest.fail("typed"))
    reply = computer_mcp.handle(DESK, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "type_text", "arguments": {"text": "hunter2"}}})
    assert reply["result"]["isError"] is True
    assert "owner" in reply["result"]["content"][0]["text"].lower()


def test_a_screenshot_comes_back_as_an_image_it_can_see(monkeypatch):
    monkeypatch.setattr(desk_computer, "ensure", lambda desk: None)
    monkeypatch.setattr(sandbox, "frame", lambda desk: b"\xff\xd8jpeg")
    reply = computer_mcp.handle(DESK, {
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "screenshot", "arguments": {}}})
    block = reply["result"]["content"][0]
    assert block["type"] == "image" and block["mimeType"] == "image/jpeg"


# ── the wire ─────────────────────────────────────────────────────────────────


class FakePipe:
    """Replays server bytes; records what the client wrote."""

    def __init__(self, incoming: bytes):
        self.buf, self.out = incoming, b""

    def write(self, data):
        self.out += data

    def read(self, n, timeout=None):
        if len(self.buf) < n:
            raise desk_computer.sandbox.SandboxError("cdp_unreachable", "eof")
        head, self.buf = self.buf[:n], self.buf[n:]
        return head

    def close(self):
        pass


def test_client_frames_are_masked_and_server_frames_are_read_back():
    raw = desk_computer.frame(b'{"id":1}', mask=b"\x01\x02\x03\x04")
    assert raw[0] == 0x81 and raw[1] == 0x80 | 8
    assert raw[2:6] == b"\x01\x02\x03\x04"
    assert bytes(b ^ raw[2 + i % 4] for i, b in enumerate(raw[6:])) == b'{"id":1}'
    body = b'{"id":1,"result":{}}'
    pipe = FakePipe(bytes([0x81, len(body)]) + body)
    assert desk_computer.read_message(pipe) == body.decode()
