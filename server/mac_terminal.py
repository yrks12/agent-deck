"""His own Mac's terminal, relayed by the deck: two outbound sockets, one pipe.

The owner asked for a Terminal tab on "Your Mac", like the desks have. A desk's
terminal is a PTY the deck owns (`terminal_stream.py`). His Mac's PTY is on
the Mac, and **the Mac never listens** -- so the deck cannot dial it. Both ends
dial the deck instead, and the deck joins them:

1. His viewer (phone or Mac app) opens `WS /v1/nodes/{id}/terminal/stream`
   with the `/v1` bearer. The deck checks the gate (`refusal`) and parks the
   viewer under a fresh random session id.
2. The Mac's long-poll answers with `terminal: {session, cols, rows, from}`
   (`Relay.offer`, polled through `mac_api._extras`; `generation` wakes the
   poll within one `WAKE_CHECK`).
3. The Mac dials `WS /v1/nodes/{id}/terminal/mac?session=` with the bearer
   AND its node secret (`X-Deck-Node`), over the same WireGuard link as every
   poll, re-checks Full access itself, and runs a login shell on a PTY.
4. The deck pipes the two sockets together until either goes.

**The wire is `terminal_stream`'s, unchanged**, so the desks' SwiftTerm view
drives it as-is: binary is raw bytes both ways; text `hello`/`exit`/`error`
from the Mac, `resize`/`ack` from the viewer. The deck passes only those
types through; anything else is dropped, never interpreted.

**The gate is his Mac's own Full access.** Paused, Ask mode, asleep or
unknown: refused before anything is offered, with the one sentence he can act
on. The Mac checks again before it spawns anything, and the deck re-checks
every `TICK` while the session lives. Agents have no route here: only the
owner's bearer opens a viewer, and the Mac's end needs its node secret.

**It ends** when the viewer leaves (the Mac's socket is closed, so its shell
is detached), when the Mac ends it (Stop, ⌃⌥⌘., exit), when Full access goes,
or after `IDLE_SECONDS` with no byte either way. Keystrokes are never logged
or kept: bytes are passed and forgotten.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
from typing import Awaitable, Callable

import anyio
from fastapi import APIRouter, WebSocket

from . import mac_nodes
from .api import Refused, _authorise

IDLE_SECONDS = 30 * 60
#: How long a parked viewer waits for the Mac to dial in.
JOIN_WAIT = 15.0
#: How often a live session re-checks the gate and the idle clock.
TICK = 5.0
MAX_COLS = 500
MAX_ROWS = 200

TERMINAL_COPY = ("Turn on Full access on your Mac (Shaliach → Settings → Mac) "
                 "to use its terminal.")
NO_ANSWER_COPY = ("Your Mac did not open its terminal. Make sure Shaliach is "
                  "open on it and up to date.")
IDLE_COPY = "Closed after 30 minutes with no activity."
#: Who is looking, for the Mac's banner. Anything else reads "another device".
ORIGINS = {"iphone": "iPhone", "mac": "Mac"}

#: The only text types each side may send through.
FROM_VIEWER = ("resize", "ack")
FROM_MAC = ("hello", "exit", "error")


def refusal(node: dict | None, now: float) -> tuple[str, str] | None:
    """(reason, sentence) when his Mac's terminal may not open now. PURE."""
    if node is None:
        return ("unknown_node", "That Mac is not paired with this deck.")
    name = node.get("name") or "your Mac"
    presence = mac_nodes.presence(node, now)
    if presence == "paused":
        return ("mac_paused", f"Shaliach is paused on {name}. " + TERMINAL_COPY)
    if presence != "online":
        return ("mac_asleep", f"{name} is asleep or offline. Wake it and make "
                "sure Shaliach is open on it.")
    if node.get("mode") != "full":
        return ("full_access_required", TERMINAL_COPY)
    return None


def check_size(cols, rows) -> tuple[int, int]:
    for value, top, what in ((cols, MAX_COLS, "cols"), (rows, MAX_ROWS, "rows")):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
            raise ValueError(f"{what} must be an integer 1-{top}")
    return cols, rows


def passes(text: str, allowed: tuple[str, ...]) -> str | None:
    """The message re-serialised if its type may cross, else None. PURE."""
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("type") not in allowed:
        return None
    return json.dumps(body)


def _error(reason: str, detail: str) -> str:
    return json.dumps({"type": "error", "reason": reason, "detail": detail})


class _Session:
    def __init__(self, node_id: str, cols: int, rows: int, origin: str, viewer) -> None:
        self.id = secrets.token_urlsafe(18)
        self.node_id = node_id
        self.cols, self.rows, self.origin = cols, rows, origin
        self.viewer = viewer
        self.mac = None
        self.joined = asyncio.Event()
        self.done = asyncio.Event()


class Relay:
    """Parked viewers by Mac, and the pipe once the Mac joins."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 idle: float = IDLE_SECONDS, tick: float = TICK,
                 join_wait: float = JOIN_WAIT) -> None:
        self.clock, self.idle, self.tick, self.join_wait = clock, idle, tick, join_wait
        self._sessions: dict[str, _Session] = {}
        #: Bumped on every new offer: the long-poll re-reads `offer` on change.
        self.generation = 0

    def offer(self, node_id: str) -> dict | None:
        s = self._sessions.get(node_id)
        if s is None or s.joined.is_set():
            return None
        return {"session": s.id, "cols": s.cols, "rows": s.rows, "from": s.origin}

    def is_open(self, node_id: str) -> bool:
        s = self._sessions.get(node_id)
        return s is not None and s.joined.is_set()

    async def serve_viewer(self, ws, node_id: str, *, cols: int, rows: int,
                           origin: str,
                           gate: Callable[[], Awaitable[tuple[str, str] | None]]) -> None:
        """An ACCEPTED viewer socket, until either side goes."""
        why = await gate()
        if why is not None:
            await _close(ws, _error(*why), 4409)
            return
        old = self._sessions.pop(node_id, None)
        if old is not None:   # one viewer per Mac: the newest wins
            await _close(old.viewer, _error("opened_elsewhere", "This Mac's "
                         "terminal was opened somewhere else."), 1000)
            if old.mac is not None:
                await _close(old.mac, None, 1000)
            old.done.set()
        s = _Session(node_id, cols, rows, ORIGINS.get(origin, "another device"), ws)
        self._sessions[node_id] = s
        self.generation += 1
        try:
            try:
                await asyncio.wait_for(s.joined.wait(), self.join_wait)
            except asyncio.TimeoutError:
                await _close(ws, _error("mac_no_answer", NO_ANSWER_COPY), 4409)
                return
            await self._pipe(s, gate)
        finally:
            if self._sessions.get(node_id) is s:
                del self._sessions[node_id]
            s.done.set()

    async def join(self, ws, node_id: str, session: str) -> bool:
        """The Mac's ACCEPTED socket for `session`; False when it is not one."""
        s = self._sessions.get(node_id)
        if (s is None or s.joined.is_set()
                or not hmac.compare_digest(s.id.encode(), (session or "").encode())):
            return False
        s.mac = ws
        s.joined.set()
        await s.done.wait()
        return True

    async def _pipe(self, s: _Session, gate) -> None:
        last = [self.clock()]

        async def carry(src, dst, allowed) -> None:
            while True:
                message = await src.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if data is not None:
                    last[0] = self.clock()
                    await dst.send_bytes(data)
                    continue
                text = passes(message.get("text") or "", allowed)
                if text is not None:
                    await dst.send_text(text)

        async def watch() -> None:
            while True:
                await asyncio.sleep(self.tick)
                if self.clock() - last[0] >= self.idle:
                    await _close(s.viewer, _error("idle", IDLE_COPY), 1000)
                    return
                why = await gate()
                if why is not None:
                    await _close(s.viewer, _error(*why), 4409)
                    return

        tasks = [asyncio.create_task(carry(s.viewer, s.mac, FROM_VIEWER)),
                 asyncio.create_task(carry(s.mac, s.viewer, FROM_MAC)),
                 asyncio.create_task(watch())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            # Shielded: Starlette cancels the handler when the viewer goes,
            # and the Mac's end must still be closed so its shell detaches.
            with anyio.CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)
                await _close(s.mac, None, 1000)
                await _close(s.viewer, None, 1000)


async def _close(ws, text: str | None, code: int) -> None:
    """Say `text` (if any) and close; a socket already gone is fine."""
    try:
        if text is not None:
            await ws.send_text(text)
        await ws.close(code=code)
    except Exception:  # noqa: BLE001 - the other side is already gone
        pass


def build_router(store: mac_nodes.Store, relay: Relay):
    """The two sockets. Auth is checked BEFORE accept; refusals close with
    4000 + the status (4401, 4404, 4400), as the desks' terminal does."""
    router = APIRouter(prefix="/v1")

    def _node(node_id: str) -> dict | None:
        try:
            return store.node(node_id)
        except mac_nodes.MacError:
            return None

    def _gate(node_id: str):
        async def gate():
            node = await asyncio.to_thread(_node, node_id)
            return refusal(node, store.now())
        return gate

    @router.websocket("/nodes/{node_id}/terminal/stream")
    async def viewer(websocket: WebSocket, node_id: str, cols: int = 80,
                     rows: int = 24) -> None:
        """His viewer. `from=iphone|mac` names it on the Mac's banner."""
        try:
            _authorise(websocket)
            check_size(cols, rows)
        except Refused as exc:
            await websocket.close(code=4000 + exc.status_code)
            return
        except ValueError:
            await websocket.close(code=4400)
            return
        if await asyncio.to_thread(_node, node_id) is None:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        await relay.serve_viewer(websocket, node_id, cols=cols, rows=rows,
                                 origin=websocket.query_params.get("from", ""),
                                 gate=_gate(node_id))

    @router.websocket("/nodes/{node_id}/terminal/mac")
    async def mac(websocket: WebSocket, node_id: str, session: str = "") -> None:
        """The Mac's end: bearer AND its own node secret."""
        try:
            _authorise(websocket)
            proven = await asyncio.to_thread(
                store.check_node, websocket.headers.get("x-deck-node"))
        except Refused as exc:
            await websocket.close(code=4000 + exc.status_code)
            return
        except mac_nodes.MacError as err:
            await websocket.close(code=4000 + err.status)
            return
        if proven != node_id:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        if not await relay.join(websocket, node_id, session):
            await _close(websocket, _error("no_session", "No terminal is "
                         "waiting for this Mac."), 4404)

    return router
