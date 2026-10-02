"""The desk's computer, as tools in its own Claude session (MCP over stdio).

**Why MCP, and not a `YOS_*` line or a CLI.** A `YOS_ROUTINE`-style line is
harvested from finished turns and answered by a later message: right for "file
this schedule", useless for navigate -> read -> click, where every step needs
the last one's result now, and it cannot hand back a picture. A CLI on PATH
would take the desk's name as an argument the model types. Here the deck
writes the name into the spawn argv (`config`) and no tool has a `desk`
field, so nothing the model sends can aim it at another desk's container.

What it does NOT change: a desk's Claude runs as the box user, who is in the
`docker` group, so a desk that shells out to `docker exec deck-desk-<other>`
is not stopped by this module. This is the sanctioned road, not a wall; see
docs/the-agents-computer.md.

Newline-delimited JSON-RPC 2.0 on stdin/stdout, the MCP stdio transport.
Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import sys
import traceback
import types
from pathlib import Path

from . import browser, desk_computer, desk_mobile, desk_upload, sandbox

NAME = "computer"
ROOT = str(Path(__file__).resolve().parents[1])
PROTOCOL = "2025-06-18"

_XY = {"x": {"type": "integer", "minimum": 0, "maximum": sandbox.SIZE[0] - 1},
       "y": {"type": "integer", "minimum": 0, "maximum": sandbox.SIZE[1] - 1}}


def _tool(name: str, text: str, props: dict | None = None) -> dict:
    return {"name": name, "description": text, "inputSchema": {
        "type": "object", "properties": props or {},
        "required": sorted(props or {}), "additionalProperties": False}}


TOOLS = [
    _tool("navigate", "Open an http(s) URL in your own browser. Returns the "
          "page's title, URL and visible text.",
          {"url": {"type": "string"}}),
    _tool("read_page", "The title, URL and visible text of the page your "
          "browser is on now."),
    _tool("screenshot", "A picture of your computer's 1280x800 screen, as the "
          "owner sees it in the app."),
    _tool("click", "Left-click at x,y on the 1280x800 screen (the "
          "coordinates of a screenshot).", _XY),
    _tool("type_text", "Type text into whatever has focus.",
          {"text": {"type": "string"}}),
    _tool("press_key", "Press one key or chord: Return, Tab, Escape, "
          "ctrl+l, Page_Down.", {"key": {"type": "string"}}),
    _tool("type_password", "Type this site's password into the focused "
          "password field. The deck reuses the site's saved password or "
          "generates a strong one and saves it in the vault first; you never "
          "see it. Use it for sign-in AND sign-up (and the confirm field). "
          "`username` is recorded beside it (\"\" if none).",
          {"username": {"type": "string"}}),
    {"name": "upload_file", "description":
        "Put files into the page's upload (avatar, photo, video, document). "
        "`paths`: files on your machine (workspace, an `Attached file:` "
        "path) -- the deck copies them where your browser can read them. "
        "Target: `selector` = the file input or the upload button/area "
        "holding one (\"\" = the page's first file input), OR the x,y of "
        "the Upload button on the screen: the click opens the file picker "
        "and the deck fills it (no dialog appears). `drop`: true drags them "
        "onto `selector` (drop zones). Returns the file names the page holds.",
     "inputSchema": {"type": "object", "properties": {
         "paths": {"type": "array", "items": {"type": "string"},
                   "minItems": 1},
         "selector": {"type": "string"},
         "drop": {"type": "boolean"}, **_XY},
         "required": ["paths"], "additionalProperties": False}},
    _tool("mobile_mode", "on: true makes your browser a phone (Android "
          "Chrome, 390px touch screen) and reloads the page as mobile; it "
          "stays a phone until on: false. Use it for what a site only allows "
          "on mobile web -- Instagram avatar, posts/Reels, bio link, "
          "business-account switch (with upload_file for the media).",
          {"on": {"type": "boolean"}}),
]


# ── a deploy must reach a desk that is already running ─────────────────────
#
# MEASURED 2026-09-30: this process is started with the desk's session and
# lives as long as it does. a growth desk's was started at 22:14, the guard
# fix was deployed at 22:20, and at 22:22 the old guard -- which never read
# the owner's Allow -- was still refusing his desk. So the guard's modules are
# reloaded when their files change on disk.

_GUARD_MODULES = ("server.browser", "server.sandbox", "server.browser_cdp",
                  "server.handoff",
                  "server.vault", "server.login_vault", "server.browser_takeover",
                  "server.desk_computer", "server.desk_upload",
                  "server.desk_mobile")
_loaded_at: float | None = None


def _reaches(mod) -> list[str]:
    """The `server.*` modules `mod` uses: imported whole, or a name taken
    from one (`from .browser_cdp import CDPDriver`)."""
    out = []
    for value in vars(mod).values():
        name = getattr(value, "__name__", None) if isinstance(
            value, types.ModuleType) else getattr(value, "__module__", None)
        if isinstance(name, str) and name.startswith("server.") \
                and name != __name__ and name not in out:
            out.append(name)
    return out


def _closure() -> list[str]:
    """Every loaded module the tools reach, each after the ones it uses.

    MEASURED 2026-10-01: a hand-kept list missed `browser_reaper`; the
    reloaded `desk_computer` called `browser_reaper.admit` on the stale
    module, and the AttributeError ended atlas's computer server."""
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        mod = sys.modules.get(name)
        if mod is None:
            return
        for dep in _reaches(mod):
            visit(dep)
        order.append(name)

    for root in _GUARD_MODULES:
        visit(root)
    return order


def _stamp() -> float:
    newest = 0.0
    for mod in _closure() or _GUARD_MODULES:
        path = Path(ROOT) / (mod.replace(".", "/") + ".py")
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def _fresh() -> None:
    """Reload the tools' modules if a deploy changed them. Never raises."""
    global _loaded_at
    now = _stamp()
    if _loaded_at is None:
        _loaded_at = now
        return
    if now == _loaded_at:
        return
    _loaded_at = now
    for mod in _closure():
        loaded = sys.modules.get(mod)
        if loaded is None:
            continue
        try:
            importlib.reload(loaded)
        except Exception:  # noqa: BLE001 - a bad deploy must not kill the tools
            continue


def stdio_server(module: str, desk: str) -> dict:
    """One stdio MCP server entry: this interpreter running `module` for
    `desk`. PURE. The shared builder -- `server.deck_mcp` binds its name the
    same way, so the two servers cannot drift on how a desk is named."""
    return {"type": "stdio", "command": sys.executable,
            "args": ["-m", module, "--desk", desk],
            "env": {"PYTHONPATH": ROOT}}


def mcp_config(servers: dict[str, dict]) -> str:
    """The `--mcp-config` value carrying `servers`. PURE."""
    return json.dumps({"mcpServers": servers})


def server(desk: str) -> dict:
    """This server's entry for `desk`. Raises ValueError for a name no
    container can carry."""
    return stdio_server("server.computer_mcp", browser._check_desk(desk))


def config(desk: str) -> str:
    """The `--mcp-config` value for `desk`. PURE. The name is bound here."""
    return mcp_config({NAME: server(desk)})


#: What a desk is told when a call fails for a reason that is not a refusal.
#: MEASURED 2026-10-01: after one crash atlas told the owner "it comes back
#: when the desk restarts" -- while he was watching that browser. The tools
#: start a stopped browser themselves; the next call is the fix.
RETRY = ("This was one failed call, not a lost computer: try the same call "
         "again -- it starts your browser if it was stopped. Do not tell the "
         "owner your browser is gone.")


def _page(page: dict) -> str:
    stop = ""
    if page.get("needs_human"):
        stop = f"STOP: {desk_computer.card_note(page['needs_human'], page.get('card', ''))}\n\n"
    cut = "\n[text truncated]" if page.get("truncated") else ""
    return f"{stop}{page['title']}\n{page['url']}\n\n{page['text']}{cut}"


def call(desk: str, name: str, args: dict) -> list[dict]:
    """Run one tool against `desk` -- the bound name, never one in `args`."""
    if name == "navigate":
        return [{"type": "text",
                 "text": _page(desk_computer.navigate(desk, args.get("url", "")))}]
    if name == "read_page":
        return [{"type": "text", "text": _page(desk_computer.read_page(desk))}]
    if name == "screenshot":
        blob = desk_computer.screenshot(desk)
        return [{"type": "image", "mimeType": "image/jpeg",
                 "data": base64.b64encode(blob).decode()}]
    if name == "click":
        desk_computer.act(desk, {"action": "click", "x": args.get("x"),
                                 "y": args.get("y")})
    elif name == "type_text":
        desk_computer.act(desk, {"action": "type", "text": args.get("text")})
    elif name == "press_key":
        desk_computer.act(desk, {"action": "key", "key": args.get("key")})
    elif name == "type_password":
        return [{"type": "text", "text": desk_computer.type_password(
            desk, str(args.get("username") or ""))}]
    elif name == "upload_file":
        return [{"type": "text", "text": desk_upload.upload_file(
            desk, args.get("paths") or [], str(args.get("selector") or ""),
            args.get("x"), args.get("y"), bool(args.get("drop")))}]
    elif name == "mobile_mode":
        return [{"type": "text", "text": desk_mobile.mobile_mode(
            desk, bool(args.get("on")))}]
    else:
        raise ValueError(f"no such tool: {name!r}")
    return [{"type": "text", "text": "done -- take a screenshot or read_page "
             "to see the result"}]


def handle(desk: str, msg: dict) -> dict | None:
    """One JSON-RPC message in, one reply out (None for a notification)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        result = {"protocolVersion": asked, "capabilities": {"tools": {}},
                  "serverInfo": {"name": NAME, "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        _fresh()
        params = msg.get("params") or {}
        try:
            content = call(desk, str(params.get("name")),
                           params.get("arguments") or {})
            result = {"content": content, "isError": False}
        except (ValueError, PermissionError, sandbox.SandboxError,
                browser.BrowserError) as exc:
            result = {"content": [{"type": "text", "text": browser.safe(
                f"{getattr(exc, 'reason', 'refused')}: {exc}")}],
                "isError": True}
        except Exception as exc:  # noqa: BLE001 - see `RETRY`
            traceback.print_exc(file=sys.stderr)
            result = {"content": [{"type": "text", "text": browser.safe(
                f"computer_error: {exc!r}"[:300]) + "\n" + RETRY}],
                "isError": True}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"no method {method!r}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="computer_mcp")
    parser.add_argument("--desk", required=True)
    desk = browser._check_desk(parser.parse_args(argv).desk)
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            reply = handle(desk, msg) if isinstance(msg, dict) else None
        except Exception as exc:  # noqa: BLE001 - one bad call must not end the server
            reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32603, "message": repr(exc)[:300]}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
