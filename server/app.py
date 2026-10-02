"""Agent Deck daemon: collector loop + SSE stream + static web workspace."""

from __future__ import annotations

import asyncio
import functools
import hmac
import ipaddress
import json
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

from . import api
from . import ask_recorder
from . import asking
from . import autoreview
from . import calls as calls_mod
from . import connectors as connectors_mod, connectors_api
from . import voice as voice_mod
from . import uploads as uploads_mod
from . import compaction
from . import deckauth
from . import deckconfig
from . import deskpage
from . import focus as focus_mod
from . import harvest as harvest_mod
from . import login_vault
from . import manager as manager_mod
from . import ntfy as ntfy_mod
from . import office
from . import pair_api
from . import usage_api
from . import mac_api, mac_nodes
from . import ratelimit
from . import ringing
from . import onboard
from . import paths
from . import phone as phone_mod
from . import roster
from . import rules
from . import routines
from . import spawn
from . import wake
from .pairing import DeviceStore
from .collector import Collector
from .sources import comms as comms_mod
from .sources.media import MediaIndex

TICK_SECONDS = 1.0
#: How often the login sync reads each running desk's browser for a sign-in made
#: by hand on its screen and fans it out (server/login_vault.py). ~30 s keeps it
#: cheap on the box; the owner's "within ~30 s" capability rests on this.
LOGIN_SYNC_SECONDS = float(os.environ.get("DECK_LOGIN_SYNC_SECONDS", "30"))
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
#: Where the harvester remembers how far it has read each transcript. Must
#: outlive the process: an in-memory offset re-reads every transcript from
#: byte 0 on every restart and re-applies every historical YOS_HIRE.
HARVEST_OFFSETS = paths.BUS_DIR / "harvest-offsets.json"

app = FastAPI(title="Agent Deck")
collector = Collector()
media = MediaIndex()

_state: dict = {"generated_at": 0, "sessions": [], "totals": {}}
_subscribers: set[asyncio.Queue] = set()

#: The socket his WhatsApp replies are injected into. Bound at startup; None
#: when there was nowhere to bind, which is a degraded deck, not a broken one.
_phone_listener = None

#: Built on first use, never at import: constructing it touches ~/.claude, and
#: the routes on this module must import cleanly wherever there is no bus.
_harvester: harvest_mod.Harvester | None = None


def _harvest(sessions: list[dict]) -> list[dict]:
    """Read back what every live session said about itself, and apply it.

    This is the return path. `server/onboard.py` could always parse a session's
    `YOS_DESK` (an agent naming itself) and `YOS_HIRE` (an agent asking for a
    colleague) and nothing ever called it, so the conversation only ran one way
    and an agent could not build a team. Blocking file I/O -- the caller keeps
    it off the event loop.
    """
    global _harvester
    if _harvester is None:
        # `seat` is what makes a hire staff rather than paperwork. Hiring
        # writes a desk and puts nobody at it, so a manager that hired two
        # people got two rows reading OFFLINE and went on doing the work
        # itself. `start_agent` is handed over rather than `spawn` directly
        # because it goes through `Surface._start`, which is where the
        # workspace-trust vouch and the `blocked` reason on the desk row live.
        _harvester = harvest_mod.Harvester(
            roster.DEFAULT_PATH, HARVEST_OFFSETS,
            seat=_client_surface.start_agent,
            # A desk's `YOS_ROUTINE` goes through the same Surface methods --
            # and the same write lock -- as the app's Routines panel.
            routines=_client_surface,
            # Its receipts ("Scheduled routine ...", "Hired: ...") are answers a
            # desk is waiting for: queued AND delivered, like every other door.
            tell=_deck_tell)
    return _harvester.poll(sessions)


def _ring_tick() -> None:
    ringing.sweep()
    _client_surface.push_rings()


async def _loop() -> None:
    global _state
    while True:
        try:
            # The collector is synchronous file I/O; keep it off the event loop
            # so a slow disk can't stall SSE delivery.
            _state = await asyncio.to_thread(collector.tick)
            payload = json.dumps(_state)
            for queue in list(_subscribers):
                if queue.full():
                    # Slow client: drop its backlog rather than the connection.
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(payload)
        except Exception as exc:  # never let one bad tick kill the daemon
            print(f"[agent-deck] tick failed: {exc!r}")

        # Hung off the same tick, in its own guard: the board is the product,
        # and a harvest that raises must cost one poll rather than the loop
        # that keeps every card on screen up to date.
        try:
            harvested = await asyncio.to_thread(_harvest, _state.get("sessions") or [])
            for record in harvested:
                print(
                    f"[agent-deck] harvest: {record['actor']} {record['kind']} "
                    f"{record['result']} {record['name']} {record['reason']}".rstrip()
                )
        except Exception as exc:
            print(f"[agent-deck] harvest failed: {exc!r}")

        # Third guard, same reason as the second: the board is the product. A
        # wedged WhatsApp bridge costs him a notification, never the screen he
        # is looking at. `_page_the_owner` blocks on a subprocess, so it goes
        # to a thread like everything else on this tick.
        try:
            await asyncio.to_thread(_page_the_owner)
        except Exception as exc:
            print(f"[agent-deck] paging failed: {exc!r}")

        # Fourth guard: his own ntfy topic, the everyday real-time channel
        # (WhatsApp is urgent-only). Off unless DECK_NTFY_URL is set.
        try:
            await asyncio.to_thread(_ntfy_pusher.tick)
        except Exception as exc:
            print(f"[agent-deck] ntfy failed: {exc!r}")

        # A desk calling him (server/ringing.py): pushed once, and a ring
        # nobody answered in 30 s becomes a line in his thread, app open or not.
        try:
            await asyncio.to_thread(_ring_tick)
        except Exception as exc:
            print(f"[agent-deck] rings failed: {exc!r}")

        # Fifth guard: the Mac bridge's clock. A job nobody claims expires and
        # a job whose Mac went quiet is `lost` even when no Mac and no desk is
        # polling, so a desk waiting on one is never left hanging.
        try:
            if _mac_store.root.is_dir():   # no Mac ever paired: nothing to sweep
                await asyncio.to_thread(_mac_store.sweep)
        except Exception as exc:
            print(f"[agent-deck] mac sweep failed: {exc!r}")

        await asyncio.sleep(TICK_SECONDS)


@app.on_event("startup")
async def _startup() -> None:
    # Before anything can be served: materialise the token where the hooks and
    # bin/ read it. Doing this lazily on the first /api call would mean the
    # first tool call in the first hired session after a restart races the file
    # into existence and asks instead of being decided.
    await asyncio.to_thread(deckauth.token)

    # The rules a running desk is caught up on (server/rules.py): published on
    # every start, so a deploy that changes a rule reaches every desk's next
    # prompt or resume instead of waiting for a re-hire.
    # Team memory starts from the lessons already learned (server/learning.py),
    # each added once ever; before `publish` so the index is in this version.
    try:
        from . import learning
        await asyncio.to_thread(learning.seed)
    except Exception as err:  # a seed must never stop the deck from starting
        print(f"[agent-deck] team memory seed failed: {err}", flush=True)
    await asyncio.to_thread(rules.publish)

    # Was this tree put here by bin/deploy-box, or rsynced over it by hand? A
    # hand rsync of an older checkout is a silent rollback (measured 2026-09-30);
    # say so on every start, where `journalctl -u agentdeck` shows it.
    try:
        from . import deploy_guard
        guard_ok, guard_said = deploy_guard.check(Path(__file__).resolve().parent.parent)
        if not guard_ok:
            print(f"[agent-deck] DEPLOY GUARD: {guard_said}", flush=True)
    except Exception as err:  # a guard must never stop the deck from starting
        print(f"[agent-deck] DEPLOY GUARD could not run: {err}", flush=True)

    # The way back in from his phone. Bound before the loop starts so a reply
    # to a question paged on the very first tick has somewhere to land.
    #
    # Loud on failure, and that matters more than it looks: without the socket
    # the deck still pages him perfectly and every answer he sends disappears,
    # which is a worse state than not paging him at all.
    global _phone_listener
    where = _deck_socket()
    if where is None:
        print("[agent-deck] no Claude socket directory — the phone can be "
              "paged but his replies have nowhere to arrive")
    else:
        _phone_listener = phone_mod.Listener(where, on_text=_phone_answer)
        if _phone_listener.start():
            print(f"[agent-deck] phone replies arrive on {where}")
        else:
            _phone_listener = None
            print(f"[agent-deck] COULD NOT bind {where} — he can be paged but "
                  "not answer; answers must be given on the board")

    asyncio.create_task(_loop())
    # Separate task on purpose: the board must keep painting at 1 Hz even while
    # a routine is being delivered, and a routine must fire even if a collector
    # tick is slow.
    asyncio.create_task(_routine_loop())
    # The login sync: a sign-in made by hand on any desk's own screen reaches
    # every other desk within ~30 s. Its own task so a slow docker exec never
    # stalls the board, and it starts only when desks are dockerised.
    try:
        if deckconfig.load().desks.docker:
            asyncio.create_task(_login_sync_loop())
    except Exception as err:  # noqa: BLE001 - a bad config must not stop startup
        print(f"[agent-deck] login sync not started: {err}", flush=True)


# The client surface the macOS and iOS apps talk to. Mounted at import time, not
# in startup: the routes must exist on the app object whether or not the daemon
# ever runs, or a client's 404 is indistinguishable from the daemon being down.
# It fails closed -- with AGENT_DECK_TOKEN unset every /v1 route answers 503.
#: Kept, not discarded. `_harvest` needs the one door that can put a session at
#: a desk the harvester just hired, and it must be the SAME surface the client
#: talks to -- a second one would seat desks the board's own state never saw.
_client_surface = api.register(
    app,
    surface=api.Surface(
        snapshot=lambda: _state,
        comms=collector.comms,
        deliver=lambda name, text: _deliver_or_wake(name, text),
        # K3: an unseated desk with a resumable session reads ASLEEP, and a
        # message to it reads "waking" rather than "nobody is there".
        asleep=wake.is_asleep,
    ),
)

# Spoken replies in the call's voice and recorded voice messages: the OpenAI
# key stays here; the apps get audio bytes and a transcript, never the key.
app.include_router(voice_mod.build_router(
    _client_surface, roster_path=lambda: Path(ROSTER_PATH)))

# His screenshots and files from the phone: stored here, read by the desk.
app.include_router(uploads_mod.build_router(_client_surface))


#: Reads the SAME `owner_alerts` the app polls, so ntfy never buzzes for a
#: thing the app stays quiet about. Cursor beside the other bus state.
_ntfy_pusher = ntfy_mod.Pusher(
    paths.BUS_DIR / "ntfy.json",
    fetch=lambda since: _client_surface.owner_alerts(since))


class _DeckGate(pair_api.BearerGate):
    """`pair_api.BearerGate`, except the owner's own machine is never banned.

    The Mac-local deck has no `deck.toml`, so it runs `network.mode = "public"`
    and the limiter would count every wrong token from `127.0.0.1` -- including
    `bin/deck` and the hooks of every hired desk -- then ban them all for 30
    minutes. A *local caller* is one whose peer is loopback (or not a network
    address at all) and that carries no `X-Forwarded-For`: Caddy, the only
    loopback caller that is not the machine itself, always adds one. Local
    callers still need a valid token; their failures are just not counted or
    banned.
    """

    @staticmethod
    def is_local(request) -> bool:
        if request.headers.get("x-forwarded-for"):
            return False
        peer = request.client.host if request.client else None
        try:
            return ipaddress.ip_address(peer).is_loopback if peer else True
        except ValueError:
            return True

    def check(self, request) -> str:
        if not self.is_local(request):
            return super().check(request)
        expected = self._master()
        if not expected:
            raise api.Refused(503, "auth_not_configured",
                              f"{api.TOKEN_ENV} is unset; /v1 is closed until it is set")
        scheme, _, presented = (request.headers.get("authorization") or "").partition(" ")
        presented = presented.strip()
        if scheme.lower() == "bearer" and presented:
            if hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
                return "master"
            device = self.devices.verify(presented)
            if device is not None:
                return device.id
        raise api.Refused(401, "unauthorized", "bearer token missing or wrong",
                          headers={"WWW-Authenticate": "Bearer"})


def _mount_pairing(config: deckconfig.DeckConfig) -> _DeckGate:
    """K3: `/healthz`, `POST /v1/pair`, `GET /v1/version`, and the gate every
    `/v1` route (this file's and `calls`'s) authorises through."""
    gate = _DeckGate(devices=DeviceStore(config.deck.state_dir),
                     limiter=ratelimit.RateLimiter(), mode=config.network.mode)
    api.set_gate(gate)
    pair_api.mount(app, config, gate=gate, devices=gate.devices, limiter=gate.limiter)
    return gate


#: Read once at import. A missing deck.toml is all defaults (the owner's Mac);
#: a damaged one stops the daemon here, loudly, rather than serving on guesses.
_deck_config = deckconfig.load()
_deck_gate = _mount_pairing(_deck_config)


@app.get("/api/state")
async def state() -> JSONResponse:
    return JSONResponse(_state)


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    _subscribers.add(queue)

    async def gen():
        try:
            yield f"data: {json.dumps(_state)}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {payload}\n\n"
        finally:
            _subscribers.discard(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/focus/{pid}")
async def focus(pid: int) -> JSONResponse:
    ok, detail = await asyncio.to_thread(focus_mod.focus, pid)
    return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 409)


def _try_inject(target: str, text: str) -> bool:
    """Deliver now through the session's own socket, if it has a live one.

    Returns True only when the bytes went out AND a person could read them, so
    the caller can ack the queued record. Without that ack the office hook
    delivers the same text again on the session's next turn.

    A live socket is not a readable screen. `inject()` raising is a property of
    the transport; whether anyone will ever see the text is a property of what
    is drawn over the composer. With a dialog up the injected line lands
    UNDERNEATH it, unsubmitted, and returning True there is the deck stating a
    delivery it never established.
    """
    if target == "*":
        # A broadcast belongs to the hook: injecting would reach one session and
        # silently drop it for everyone else.
        return False

    for card in _state.get("sessions", []):
        if card.get("session_id") != target and card.get("name") != target:
            continue
        if card.get("attention"):
            # The whole class, not one member. `attention.kind` is closed in
            # sources/bus.ATTENTION_TYPES -- `permission_prompt` and
            # `agent_needs_input` -- and both mean the same thing: Claude Code
            # has a dialog up and the dialog owns the keyboard. `idle_prompt` is
            # deliberately NOT in that set, so a session merely sitting at its
            # own composer still gets the fast path.
            #
            # Report, don't reroute: we do not inject at all. Bytes now plus the
            # hook later would put one copy in the dead composer for him to
            # delete and one in the conversation. The queue already delivers on
            # the next turn -- which is precisely when the dialog is gone.
            return False
        pid = card.get("pid")
        if not pid:
            continue
        try:
            # inject() picks the transport: Claude Code's socket, else the
            # OpenCode proxy for sessions wrapped by bin/opencode-sock. Too long
            # for the socket is a nudge that starts the turn and False, so the
            # record stays queued for the hook to attach to that turn.
            return manager_mod.inject_or_nudge(pid, text)
        except manager_mod.InjectError:
            continue  # another card may own the same name; else fall back to the queue

    return False


#: THE waker. One per daemon, so its per-desk lock covers every door: his
#: message, a routine and a peer arriving in the same minute wake a desk once.
_waker = wake.Waker(
    desk_of=lambda name: _find_desk(name),
    restart=lambda desk: wake.restart_headless(desk, roster_path=Path(ROSTER_PATH)),
    record=lambda event: _bus_append(event),
)


def _wake_later(name: str, reason: str) -> None:
    """Wake `name` off the request thread. A resume answers in about a second
    but a restart can take thirty, and his POST must not wait for either: the
    message is already queued, and the wake acks it when it is carried."""
    def run() -> None:
        try:
            _waker.ensure_awake(name, reason=reason)
        except Exception as exc:  # a wake must never take the deck down
            print(f"[agent-deck] wake {name!r} failed: {exc!r}")

    threading.Thread(target=run, name=f"wake-{name}", daemon=True).start()


def _deliver_or_wake(target: str, text: str, *,
                     reason: str = "owner_message") -> bool:
    """The `/v1` surface's delivery: the socket if there is one, else a wake.

    Before K3 a miss here was the end of it -- the record sat on the queue for
    a session nobody would start, and a desk the CLI's daemon had retired for
    idling never heard from him again. False either way on a miss: a wake is
    not a delivery, and the ack comes from whoever actually carries the text.
    """
    if _try_inject(target, text):
        return True
    if target and target != "*":
        _wake_later(target, reason)
    return False


# K6: live voice calls. The deck line rides the same waker as every other door
# (reason "call"); a live socket gets it at once, a sleeping desk is woken.
app.include_router(calls_mod.build_router(
    roster_path=lambda: Path(ROSTER_PATH),
    wake=lambda name: _waker.ensure_awake(name, reason="call"),
    inject=lambda name, text: _try_inject(name, text),
))

# Connectors & Skills (docs/connectors.md): the store the app and desks use.
app.include_router(connectors_api.build_router(connectors_mod.default_store()))

# The Mac bridge (docs/mac-bridge.md, MB4): the routes the owner's Mac app
# dials out to. No route here creates a job -- only a desk's `mac` MCP process
# enqueues, into this same store on disk -- and the Mac enforces every grant.
_mac_store = mac_nodes.Store(paths.BUS_DIR / "mac")


def _mac_tell(desk: str, text: str) -> bool:
    """A grant answer or "the Mac is back" for `desk`: queued first, from
    `deck`, so a failed inject still reaches it on the turn the wake starts;
    then straight into the live session, else a wake with reason `mac`."""
    queued = office.send(desk, text, sender="deck")
    if not queued.get("ok"):
        return False
    if _deliver_or_wake(desk, office.attribute(text, "deck"), reason="mac"):
        office.ack(queued["id"])
    return True


def _deck_tell(desk: str, text: str) -> bool:
    """A receipt from the deck to `desk` (the harvester's routine and hire
    answers). Queued first, so a failed inject still reaches it on the turn a
    wake starts; then into the live session, else a wake. Acked only when this
    call carried it -- a wake carries the queue itself and acks there.

    MEASURED 2026-10-01: queued with no delivery, two such receipts sat unread
    for an hour on the box (deckdoctor `stuck_messages`)."""
    queued = office.send(desk, text, sender="deck")
    if not queued.get("ok"):
        return False
    if _deliver_or_wake(desk, office.attribute(text, "deck"), reason="deck_receipt"):
        office.ack(queued["id"])
    return True


def _mac_reload_stale(scope: str) -> None:
    """He turned Mac control on: live desks whose `mac` process predates the
    control tools reload as themselves after their turn (connectors' reload),
    with a note that control is on. Detached: the poll answers at once."""
    from . import mac_mcp

    def run() -> None:
        try:
            desks = [(d.name, d.engine) for d in roster.load_roster(ROSTER_PATH)]
            names = mac_api.desks_to_reload(
                scope, desks, stale=mac_mcp.stale,
                live=lambda n: connectors_mod._live_job(n) is not None)
            node = next((n for n in _mac_store.nodes()
                         if (n.get("control") or {}).get("scope") == scope), {})
            note = mac_api.reload_note(node.get("name", "his Mac"),
                                       (node.get("control") or {}).get("until"))
            for name in names:
                connectors_mod.schedule_reload(name, note)
        except Exception as exc:  # noqa: BLE001 - a reload never breaks a poll
            print(f"[agent-deck] mac: reloading desks for control failed: {exc!r}")
    threading.Thread(target=run, daemon=True).start()


app.include_router(mac_api.build_router(
    _mac_store,
    tell=_mac_tell,
    owner_line=lambda text: office.send(office.OWNER_INBOX, text, sender="deck"),
    # A revoke is news, not a reason to start a turn: queued, no wake.
    queue=lambda desk, text: office.send(desk, text, sender="deck", extra={"notice": True}),
    reload_stale=_mac_reload_stale,
))

# The Claude plan meter (5-hour + weekly) both apps draw; reads the collector's
# own readers, so there is one poller, not two. One set of per-account meters
# serves both the usage route and the accounts list.
from .sources.usage import AccountMeters  # noqa: E402
from . import accounts_api, mover as mover_mod  # noqa: E402

_meters = AccountMeters(collector.usage)
app.include_router(usage_api.build_router(
    reader=collector.usage, opencode=collector.opencode_usage, meters=_meters))


def _card_of(name: str) -> dict | None:
    """The desk's live board card, for the move gate (idle, subagents, prompt)."""
    for card in (_state or {}).get("sessions") or []:
        if isinstance(card, dict) and card.get("name") == name:
            return card
    return None


# Move a desk to another Claude account (docs/plans/2026-10-01-two-accounts.md).
_mover = mover_mod.Mover(card_of=_card_of)
# Signing an account in from the app runs the CLI's own login (S7c).
from . import account_login  # noqa: E402

app.include_router(accounts_api.build_router(
    meters=_meters, mover=_mover, logins=account_login.LoginManager()))


def _sweep_accounts_forever() -> None:
    """Apply the account policy (S9) every LIMIT_SECONDS: a desk whose last
    turn hit a usage limit moves on its next idle tick (S9c), one over the
    threshold within one tick of the meter seeing it. Under a `fixed` policy
    -- every install where `failover_allowed` is false -- this reads deck.toml
    and does nothing else."""
    from . import limits, policy
    while True:
        time.sleep(policy.LIMIT_SECONDS)
        try:
            for row in policy.tick(load_cfg=deckconfig.load, meters=_meters.poll_all,
                                   desks=lambda: roster.load_roster(roster.DEFAULT_PATH),
                                   mover=_mover, limited=limits.desk_limited,
                                   held=_held):
                if row["outcome"] == "moved":  # a refusal is `_held`'s, logged once
                    print(f"accounts: {row['desk']} {row['from']} -> {row['to']}: "
                          f"moved ({row.get('why', 'threshold')})", flush=True)
        except Exception as exc:  # noqa: BLE001 - try again next round
            print(f"accounts: sweep failed: {exc!r}", flush=True)


def _card_to_atlas(text: str) -> None:
    """One deck notice in Atlas's thread: queued, no wake, not the phone."""
    chief = _chief_name()
    if chief:
        office.send(chief, f"[Agent Deck] {text}", sender="deck", extra={"notice": True})
    print(f"accounts: {text}" + ("" if chief else " (no chief to tell)"), flush=True)


from . import policy as _policy  # noqa: E402

#: Desks the account policy wants moved that are mid-turn (one per daemon).
_held = _policy.Held(log=lambda line: print(line, flush=True), card=_card_to_atlas)


def _watch_turn_ends_forever() -> None:
    from . import policy
    while True:
        time.sleep(policy.BOUNDARY_SECONDS)
        if _held.rows:
            try:
                _held.at_boundary(_mover)
            except Exception as exc:  # noqa: BLE001 - try again next round
                print(f"accounts: turn-end watch failed: {exc!r}", flush=True)


def _refresh_oauth_forever() -> None:
    """Refresh OAuth connector tokens before they expire, so a desk's next
    connection finds a live one. Never logs a token: the store returns ids."""
    while True:
        time.sleep(600)
        try:
            connectors_mod.default_store().refresh_oauth_due()
        except Exception:  # noqa: BLE001 - try again next round
            pass


_oauth_refresher: list[threading.Thread] = []


@app.on_event("startup")
def _start_oauth_refresher() -> None:
    if _oauth_refresher:
        return
    _oauth_refresher.append(threading.Thread(
        target=_refresh_oauth_forever, name="store-oauth-refresh", daemon=True))
    _oauth_refresher[0].start()
    threading.Thread(target=_sweep_accounts_forever, name="accounts-sweep",
                     daemon=True).start()
    threading.Thread(target=_watch_turn_ends_forever, name="accounts-turn-end",
                     daemon=True).start()
    # The policy at wake and start too, not only every tick (S9d).
    from . import limits, policy, wake as wake_mod
    policy.PLACER = policy.Placer(
        load_cfg=deckconfig.load, meters=_meters.poll_all, mover=_mover,
        live=office.live_session_ids, desk_of=wake_mod._desk_of,
        save_account=mover_mod.save_account, limited=limits.desk_limited)


@app.post("/api/message")
async def message(payload: dict) -> JSONResponse:
    to = str(payload.get("to") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not to or not text:
        return JSONResponse({"ok": False, "detail": "need to + text"}, status_code=400)
    result = await asyncio.to_thread(office.send, to, text)
    if not result["ok"]:
        return JSONResponse(result, status_code=500)

    # Record first, then inject: the queue entry is what the graph draws, and
    # acking it is what stops the hook delivering the same text a second time.
    #
    # The RECORD keeps `text`; the WIRE carries `office.attribute`. This route
    # is the deck's own board -- him typing at 127.0.0.1:7788 -- and it takes
    # `office.send`'s default sender, `office.OWNER_HANDLE`. Injecting the bare text is what
    # made a desk read him as a peer relaying for him and refuse him twice; the
    # framing that says otherwise lives in `hooks/cc-office.js`, which this
    # fast path deliberately skips.
    if await asyncio.to_thread(_try_inject, to, office.attribute(text, office.OWNER_HANDLE)):
        await asyncio.to_thread(office.ack, result["id"])
        result = {**result, "delivered": True}
    return JSONResponse(result, status_code=200)


@app.get("/api/comms")
async def comms_graph() -> JSONResponse:
    """Every message that passed between sessions, plus the chart it implies."""
    cards = _state.get("sessions", [])
    crown = await asyncio.to_thread(manager_mod.read_crown)
    edges = await asyncio.to_thread(
        collector.comms.edges, comms_mod.build_directory(cards)
    )
    chart = comms_mod.groups(edges, crown.get("session_id"))
    # The office draws each desk in its session's live state, so ship the state
    # alongside the graph rather than making the page join two endpoints.
    sessions = {
        card["session_id"]: {
            "name": card.get("name", ""),
            "state": card.get("state", "IDLE"),
            "project": card.get("project", ""),
            "git_branch": card.get("git_branch", ""),
            "pid": card.get("pid"),
        }
        for card in cards
    }
    return JSONResponse(
        {"edges": edges, "manager": crown or None, "sessions": sessions, **chart}
    )


@app.post("/api/manager/crown")
async def crown(payload: dict) -> JSONResponse:
    """Make one session the manager. An empty session_id clears the crown."""
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return JSONResponse(
            {"ok": True, "manager": await asyncio.to_thread(manager_mod.uncrown)}
        )
    card = next(
        (c for c in _state.get("sessions", []) if c["session_id"] == session_id), None
    )
    if card is None:
        return JSONResponse({"ok": False, "reason": "unknown_session"}, status_code=404)
    record = await asyncio.to_thread(
        manager_mod.crown, session_id, card["pid"], card["name"]
    )
    return JSONResponse({"ok": True, "manager": record})


@app.post("/api/manager/say")
async def manager_say(payload: dict) -> JSONResponse:
    """The one write path. Takes no pid: it always reads the crown.

    Every refusal is a 409 carrying a slug, never a 500 -- "your manager went
    away" is an answer, not a fault.
    """
    crown_record = await asyncio.to_thread(manager_mod.read_crown)
    if not crown_record.get("pid"):
        return JSONResponse({"ok": False, "reason": "no_manager"}, status_code=409)

    text = str(payload.get("text") or "")
    try:
        await asyncio.to_thread(manager_mod.inject, crown_record["pid"], text)
    except manager_mod.InjectError as exc:
        return JSONResponse(
            {"ok": False, "reason": exc.reason, "detail": exc.detail}, status_code=409
        )

    # Armed only now: the reply to this message is the one we may speak.
    collector.speaker.arm(crown_record["session_id"], muted=bool(payload.get("mute")))
    return JSONResponse({"ok": True, "pid": crown_record["pid"]})


@app.get("/api/media")
async def media_list() -> JSONResponse:
    runs = await asyncio.to_thread(media.scan)
    return JSONResponse({"runs": runs, "summary": media.summary()})


@app.get("/media/{slug}/{kind}")
async def media_file(slug: str, kind: str):
    if kind not in ("video", "audio"):
        return JSONResponse({"detail": "bad kind"}, status_code=404)
    path = media.resolve(slug, kind)
    if path is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    # FileResponse honours Range requests, so the player can seek.
    return FileResponse(path, media_type="video/mp4" if kind == "video" else "audio/wav")


# ── Auto Review ──────────────────────────────────────────────────────────────

AUTOREVIEW_PATH = autoreview.DEFAULT_PATH
#: Where an unanswered question lives until somebody answers it.
ASKS_PATH = asking.DEFAULT_PATH
#: Which of those questions his phone has already been buzzed about. Durable
#: because the alternative is re-paging every open ask on every deploy.
PAGED_PATH = paths.BUS_DIR / "paged.json"
BUS_FILE = paths.BUS_FILE
#: Where each live Claude session writes `<pid>.json` with its id and `--name`.
#: Read only when the board has not yet seen a session id -- the seconds right
#: after a wake, which is exactly when a desk retries what it was denied.
SESSIONS_DIR = paths.SESSIONS_DIR
# Same ceiling hooks/cc-bus.js enforces, so one writer cannot grow the log past
# what the other one keeps trimming.
BUS_MAX_BYTES = 5 * 1024 * 1024

# The rule file is parsed once and re-read only when it changes on disk. This
# endpoint runs before EVERY tool call in every session under the hook, so a
# JSON parse per call is a tax on the whole machine, not on one request.
_rules_cache: dict = {"path": None, "stamp": None, "rules": []}


# ── the phone loop ───────────────────────────────────────────────────────────
#
# Everything below is the answer to "he walked away from the desk". The board
# is only a product for someone sitting in front of it; away from it he needs
# to be TOLD he is needed and to be able to ANSWER. See server/phone.py for
# why the reply arrives on a UNIX socket rather than an HTTP callback -- short
# version, the bridge has no webhook and cannot POST anywhere.


def _deck_socket() -> str | None:
    """Where the bridge writes his replies. None when there is nowhere to bind.

    Beside the real sessions' sockets because `src/inject.js` refuses to write
    outside that directory -- containment we WANT, and the price of it is that
    the deck has to live there too.
    """
    stated = (os.environ.get("DECK_PHONE_SOCKET") or "").strip()
    if stated:
        # A different path, named on purpose: tests (conftest), or a second
        # deck on the same account that must not contend for the owner's.
        return stated
    base = manager_mod.sock_dir()
    return None if base is None else str(base / phone_mod.DECK_SOCKET_NAME)


def _session_socket(pid) -> str | None:
    try:
        path = manager_mod.socket_path(int(pid))
    except (TypeError, ValueError, manager_mod.InjectError):
        return None
    return None if path is None else str(path)


#: Byte offset into the office queue. The urgent reader wants what was said
#: SINCE the last tick; the rest of the file is history, and history is not
#: news.
_said_at: dict = {"offset": 0, "inode": None}


def _owner_inbox_since() -> list:
    """Records put in his thread since the last tick.

    A first read, or a rotated file, starts at the END. Starting at byte zero
    would re-send every urgent line ever written the first time the daemon
    comes up.
    """
    path = office.MESSAGES_FILE
    try:
        stat = path.stat()
    except OSError:
        return []
    if stat.st_ino != _said_at["inode"]:
        _said_at["inode"] = stat.st_ino
        _said_at["offset"] = stat.st_size
        return []
    if stat.st_size < _said_at["offset"]:
        _said_at["offset"] = 0  # truncated in place
    try:
        with path.open("r") as fh:
            fh.seek(_said_at["offset"])
            lines = fh.readlines()
            _said_at["offset"] = fh.tell()
    except OSError:
        return []

    out = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn last line; the next tick reads from after it
        if isinstance(record, dict) and record.get("to") == office.OWNER_INBOX:
            out.append(record)
    return out


def _chief_name() -> str | None:
    """Atlas, per `roster.chief`. None when it cannot be told apart -- and the
    safe answer to "who speaks for the deck?" being unclear is "nobody reaches
    his phone"."""
    try:
        return roster.chief(roster.load_roster(Path(ROSTER_PATH)))
    except Exception:
        return None


#: Atlas's urgent lines the rate limit or a dead bridge has not let out yet.
#: Capped: a desk looping on `say(urgent=True)` must not grow this forever.
_urgent_backlog: list = []
URGENT_BACKLOG_MAX = 20


def _page_urgent_from_chief() -> None:
    """Atlas's urgent lines, to WhatsApp. Every other record stays in the app.

    The gate is HERE, on the server, not only in the `say` tool: a record in
    his thread flagged urgent by any other sender -- a desk that wrote the
    queue directly, a bug in a tool -- is not sent. Other desks' urgent lines
    are routed to Atlas by `deck_mcp.say`, and Atlas decides.
    """
    fresh = [r for r in _owner_inbox_since() if r.get("urgent")]
    if fresh:
        chief = _chief_name()
        for record in fresh:
            sender = str(record.get("from") or "")
            if not chief or sender != chief:
                print(f"[agent-deck] urgent flag from {sender!r} ignored: only "
                      "the chief of staff reaches WhatsApp")
                continue
            if len(_urgent_backlog) >= URGENT_BACKLOG_MAX:
                print("[agent-deck] urgent backlog full; line kept in the app only")
                continue
            _urgent_backlog.append(
                f"*{asking.redact(sender)}*: "
                f"{asking.redact(str(record.get('text') or ''))}")
    while _urgent_backlog:
        try:
            result = phone_mod.page_urgent(_urgent_backlog[0],
                                           ledger=Path(PAGED_PATH))
        except Exception as exc:
            print(f"[agent-deck] urgent page failed: {exc!r}")
            return
        if not result.ok:
            # Held by the rate limit or refused by the bridge: kept, and tried
            # again next tick. It is in his thread in the app either way.
            print(f"[agent-deck] an urgent line is waiting for the phone: "
                  f"{result.detail}")
            return
        _urgent_backlog.pop(0)


def _page_the_owner() -> None:
    """WhatsApp him for the urgent things only. Called on the collector tick.

    OWNER RULING 2026-09-30: the app is where he is told things. From this
    tick only two things leave for his phone, both through
    `phone.page_urgent`'s rate limit: a blocking ask left unanswered in the
    app past `notify.quiet_seconds()` (30 min by default), and a line Atlas
    marked urgent.

    On the TICK rather than inside `/api/permission` and friends, and that is
    the design rather than laziness: a question recorded by any door -- the
    approve hook, the prompt hook, an MCP elicitation, the handoff floor, or
    whatever door is added next -- is a row in the same ledger, so reading the
    ledger sweeps the class. Instrumenting call sites sweeps the three that
    exist today and silently misses the fourth.

    `phone.page_ask` owns once-only, so re-reading every pending ask at 1 Hz
    costs one file read and no messages.

    Guarded PER ITEM, not per tick. `_loop` already wraps the whole call, but a
    single unpageable ask aborting the loop would take every other question
    down with it -- and the one he never hears about would then be chosen
    silently, by list order.
    """
    reply_to = _deck_socket()
    for ask in asking.pending(Path(ASKS_PATH)):
        try:
            result = phone_mod.page_ask(ask, ledger=Path(PAGED_PATH),
                                        reply_socket=reply_to)
        except Exception as exc:
            print(f"[agent-deck] ask {ask.id} could not be paged: {exc!r}")
            continue
        if not result.ok:
            # Never silent. A question he was never told about looks, from the
            # board, exactly like one he chose to ignore.
            print(f"[agent-deck] ask {ask.id} did NOT reach the phone: "
                  f"{result.detail}")

    # OWNER RULING 2026-09-30: desks' news and his own session finishing are
    # app-only now -- they are on the board and in his thread already. The
    # only other thing this tick sends is what Atlas marked urgent.
    _page_urgent_from_chief()


#: The one path to his phone that has no natural gap: a reply to something he
#: typed ON WhatsApp, so it only ever fires when he chose that channel. A
#: receipt cannot wait, so it gets a refilling CAP instead of the urgent gap. Bounded by his own thumb today and
#: unbounded the moment anything else calls `_say_to_phone` -- which is exactly
#: how the ask path measurably reached 285 sends in 60 seconds.
_receipt_ceiling = deskpage.Ceiling()


def _say_to_phone(text: str) -> None:
    """One line back to him, with no return address.

    No `reply_to`: this is a receipt, not a question, and recording it as
    routable would make his next bare word land on the deck instead of
    wherever he meant it.

    Loud when the ceiling holds it back, because a receipt he never got and a
    thing that never happened look identical from a phone.
    """
    if not _receipt_ceiling.allow():
        print(f"[agent-deck] receipt held back by the ceiling: {text[:60]!r}")
        return
    result = phone_mod.notify.send(text)
    if not result.ok:
        print(f"[agent-deck] could not confirm to the phone: {result.detail}")


def _desk_names() -> list:
    """Every desk he can address by name, in the roster's own spelling.

    Read fresh per reply rather than cached: a desk hired a minute ago must be
    reachable from his phone without a daemon restart, and this runs once per
    message he sends -- a cost measured in one file read, at human pace.
    """
    try:
        return [d.name for d in roster.load_roster(Path(ROSTER_PATH))]
    except Exception:
        return []


def _send_to_desk(desk: str, text: str) -> None:
    """His words, onto that desk, down the SAME door the app uses.

    `Surface.send` and not `office.send` directly, and that is the whole point
    of routing it here: that method is where the owner's attribution frame, the
    live-socket fast path, and the ack that stops the office hook delivering
    the same text twice all live. A second way to say something to a desk would
    drift from the one the app uses, and the half that drifted would be the
    half nobody watches.

    He is always told what happened, and told it in the SAME words the app
    puts on the thread -- `delivery.what`, off the message the send returns,
    computed once in `api.delivery_of`. The receipt used to be written here,
    and it said "queued for its next turn" over every failure it could have:
    a desk with nobody at it read exactly like a desk that was merely busy.
    That is the phone half of the hours he lost to five unanswered messages,
    and a second wording of "what happened" is how the two halves drift.
    """
    try:
        outcome = _client_surface.send(api.direct_id(desk), text)
    except api.Refused as refused:
        body = refused.detail if isinstance(refused.detail, dict) else {}
        why = str(body.get("detail") or body.get("reason") or refused.detail)
        _say_to_phone(f"Not sent to *{desk}*: {why}")
        return
    except Exception as exc:
        print(f"[agent-deck] sending to {desk} failed: {exc!r}")
        _say_to_phone(f"Not sent to *{desk}*: the deck errored.")
        return

    delivery = ((outcome.get("message") or {}).get("delivery")) or {}
    what = str(delivery.get("what") or "")
    if not what:
        # Only when the thread could not name the message back -- a group id,
        # or a send that queued but folded to nothing. Never invents a state.
        what = ("It is on the queue." if not outcome.get("delivered")
                else "It went straight into the live session.")
    # Bold on the desk, because the desk is the thing he has to decide about.
    _say_to_phone(f"*{desk}*: {what}")


def _phone_answer(text: str) -> None:
    """His words off the phone, turned into a decision. Never raises.

    Down `Surface.answer_ask` -- the same call the board makes -- so writing
    the rule, spending a `once`, and nudging the parked desk all happen exactly
    as they do when he taps Approve. A second implementation of "answered"
    would drift, and the half that drifted would be the half nobody watches.
    """
    try:
        got = phone_mod.resolve(text, asking.pending(Path(ASKS_PATH)),
                                desks=_desk_names())
    except Exception as exc:
        print(f"[agent-deck] phone reply not understood: {exc!r}")
        return

    # Before the ask branch, because `resolve` has already decided between them
    # and an open ask id wins there. Reaching a desk is the OTHER half of the
    # product: the desk pager pushes a desk's news to his phone, and without
    # this that push is a broadcast rather than a conversation.
    if got.desk:
        _send_to_desk(got.desk, got.reply or "")
        return

    if got.ask_id is None:
        # A refusal is a sentence he can act on; `None` is ordinary
        # conversation and gets no answer at all, or this channel becomes
        # unusable for talking to sessions.
        if got.refusal:
            _say_to_phone(got.refusal)
        return

    try:
        outcome = _client_surface.answer_ask(got.ask_id, got.reply)
    except api.Refused as refused:
        # `Refused.detail` is the deck's `{ok, reason, detail}` body, not a
        # string. Interpolating it raw would put a dict on his lock screen.
        body = refused.detail if isinstance(refused.detail, dict) else {}
        why = str(body.get("detail") or body.get("reason") or refused.detail)
        _say_to_phone(f"Not applied ({got.ask_id}): {why}")
        return
    except Exception as exc:
        print(f"[agent-deck] answering {got.ask_id} failed: {exc!r}")
        _say_to_phone(f"Not applied ({got.ask_id}): the deck errored.")
        return

    # Say which desk was restarted, or say it was only recorded. "Done" over an
    # ask whose desk had already gone would be the deck claiming an effect it
    # never had.
    tail = ("and the agent was told to carry on"
            if outcome.get("resumed") else "recorded — no live agent to resume")
    _say_to_phone(f"*{got.reply}* applied to ask {got.ask_id} — {tail}.")


def _bus_append(record: dict) -> None:
    """Append one line to events.jsonl, in the shape hooks/cc-bus.js writes.

    Same file, same one-object-per-line format, same truncation ceiling -- the
    reader already tailing this log needs no second format. Every failure is
    swallowed: a full disk must never turn into a blocked tool call.
    """
    path = Path(BUS_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > BUS_MAX_BYTES:
            path.open("w").close()  # truncate in place; the reader resets on it
        with path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _autoreview_rules() -> list[autoreview.Rule]:
    """The current rules, from memory unless the file changed underneath us."""
    path = Path(AUTOREVIEW_PATH)
    try:
        stat = os.stat(path)
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None  # no rule file is no rules, which evaluates to "ask"
    cache = _rules_cache
    if cache["path"] == str(path) and cache["stamp"] == stamp:
        return cache["rules"]
    rules = autoreview.load_rules(path) if stamp is not None else []
    cache.update({"path": str(path), "stamp": stamp, "rules": rules})
    return rules


def _desk_of(payload: dict) -> str:
    """The DESK NAME a hook call comes from, or "" when it cannot be said.

    A standing "always" is keyed by desk name, and the hooks send a session id,
    which a wake replaces. The board already holds the join (a desk is started
    with `--name <desk>`, and cards are renamed to the name the desk goes by
    now); failing that, the session's own `<pid>.json`. Never a guess from a
    folder: two desks can share one, and a rule must not leak between them.
    """
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    for card in (_state or {}).get("sessions") or []:
        if isinstance(card, dict) and card.get("session_id") == session_id:
            return str(card.get("name") or "")
    from . import accounts

    entries: list[Path] = []
    for _ident, folder in [("main", Path(SESSIONS_DIR)), *accounts.other_dirs("sessions")]:
        try:
            entries += list(folder.glob("*.json"))
        except OSError:
            continue
    for entry in entries:
        try:
            raw = json.loads(entry.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and raw.get("sessionId") == session_id:
            name = str(raw.get("name") or "")
            return _canonical_agent(name) if name else ""
    return ""


def _approve(payload: dict) -> dict:
    """One verdict plus its audit line. Synchronous: called in a worker thread."""
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    desk = _desk_of(payload)
    verdict = autoreview.evaluate(
        _autoreview_rules(),
        tool_name=tool_name,
        tool_input=tool_input,
        cwd=str(payload.get("cwd") or ""),
        desk=desk,
    )
    _bus_append(
        {
            "ts": time.time(),
            "event": "approval",
            "session_id": str(payload.get("session_id") or ""),
            "tool": tool_name,
            "decision": verdict.decision,
            "rule_id": verdict.rule_id,
        }
    )

    # Write the question down. Without this the endpoint answers correctly and
    # tells nobody: asks.json stays empty, /v1/approvals returns [] whatever
    # happens, and the agent sits on a prompt that only the one person who could
    # unstick it never hears about. `on_verdict` records ONLY on `ask`, and
    # honours the flood guard, so a retry loop is one question and not forty.
    try:
        ask_recorder.on_verdict(
            Path(ASKS_PATH),
            verdict=verdict,
            tool_name=tool_name,
            tool_input=tool_input,
            cwd=str(payload.get("cwd") or ""),
            session_id=str(payload.get("session_id") or ""),
            agent=str(payload.get("agent") or desk
                      or payload.get("session_id") or ""),
        )
    except Exception as exc:  # recording must never break the hot path
        print(f"[agent-deck] ask not recorded: {exc!r}")

    return {
        "decision": verdict.decision,
        "rule_id": verdict.rule_id,
        "reason": verdict.reason,
    }


@app.post("/api/approve")
async def approve(payload: dict) -> JSONResponse:
    """Decide a tool call before its permission prompt is ever drawn.

    `hooks/cc-approve.js` calls this on the hot path of every tool call in every
    session running under the hook, so two rules govern the shape of it:

    * the work happens in a thread. A stat, a parse or an append on the event
      loop stalls every SSE client and every other request on the deck.
    * anything unexpected answers "ask". The hook already fails safe on a
      timeout or a non-200, and this keeps the same floor for a bug in here:
      silence never widens permission.
    """
    try:
        verdict = await asyncio.to_thread(_approve, payload)
    except Exception as exc:  # a bug here must not auto-approve anything
        verdict = {"decision": "ask", "rule_id": None,
                   "reason": f"auto review failed: {exc!r}"}
    return JSONResponse(verdict)


# ── the prompt-time door ─────────────────────────────────────────────────────
#
# `/api/approve` answers allow / deny / ask, and that "ask" is the whole defect:
# MEASURED on Claude Code 2.1.252 in an interactive pty, a PreToolUse "ask"
# makes the CLI draw its own modal and the session sits on it forever. The
# deck's authed socket injection into that session reported ok/authed/145 bytes
# and the text landed in the composer UNDERNEATH the modal -- delivered, and
# useless. `--permission-mode dontAsk` did not suppress it either.
#
# `PermissionRequest` fires immediately before that prompt resolves and takes
# only allow or deny, so this endpoint has no third word to fall through to.


def _blocked_message(ask_id: str) -> str:
    """What the agent is told instead of being shown a dialog it cannot answer.

    This string is the entire difference between a denial and a stall. It has
    to name the row -- an answer the owner gives lands against an id, and an
    agent that cannot quote the id cannot tell its boss what is outstanding --
    and it has to say the retry is pointless until he has answered, or an agent
    in a loop turns one question into a hundred.
    """
    return (
        f"Blocked pending the owner's approval. Nothing was run. The Agent "
        f"Deck has put this action to him as ask {ask_id}; he answers it there, "
        f"not in this session. Do not retry it in a loop -- say in your reply "
        f"that you are waiting on ask {ask_id} and carry on with something else."
    )


def _permission(payload: dict) -> dict:
    """Allow, or deny with a sentence. Synchronous: called in a worker thread.

    Re-evaluates rather than trusting whatever `/api/approve` said a moment
    ago, because the interesting case is precisely that the owner answered in
    between: he says "always" on the deck, `asking.answer` writes the rule, and
    the very next request is allowed with no retry from the agent.
    """
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    cwd = str(payload.get("cwd") or "")
    session_id = str(payload.get("session_id") or "")

    desk = _desk_of(payload)
    verdict = autoreview.evaluate(_autoreview_rules(), tool_name=tool_name,
                                  tool_input=tool_input, cwd=cwd, desk=desk)

    asks_path = Path(ASKS_PATH)
    subject = autoreview.tool_subject(tool_name, tool_input)
    # Did `/api/approve` already write this question down? That is the same
    # question as "did cc-approve.js reach the deck at all", and it is the one
    # this project has had to answer by reasoning. Read before recording.
    preask = asking.suppressed(asks_path, tool=tool_name, subject=subject,
                               cwd=cwd, window=ask_recorder.DEFAULT_WINDOW)

    body: dict
    if verdict.decision == "allow":
        body = {"behavior": "allow", "rule_id": verdict.rule_id}
    elif (once := asking.grant(asks_path, tool=tool_name, subject=subject,
                               cwd=cwd)) is not None:
        # He tapped "once" on this exact question and this is the retry it was
        # for. Without this branch the whole approve loop dead-ends here: the
        # answer wrote no rule, `evaluate` finds none, and the desk is denied a
        # second time -- he taps approve and nothing happens.
        #
        # It sits BELOW `allow` and ABOVE everything else on purpose. Below,
        # because a standing rule already covers the call and spending a
        # one-shot grant on it would burn his answer for nothing. Above the
        # handoff floor and above a `deny` rule, because both of those mean
        # "put it to the owner" and he is the one who just answered.
        #
        # `grant` marks it spent before returning, so the next retry is denied.
        body = {"behavior": "allow", "rule_id": f"once:{once.id}",
                "ask_id": once.id}
    else:
        ask = None
        try:
            ask = ask_recorder.on_verdict(
                asks_path,
                # A deny rule is still a question here: the agent is being
                # stopped and the owner is the only one who can lift it.
                verdict={"decision": "ask"},
                tool_name=tool_name, tool_input=tool_input, cwd=cwd,
                session_id=session_id,
                agent=str(payload.get("agent") or desk or session_id),
            )
        except Exception as exc:  # recording must never become a stall
            print(f"[agent-deck] ask not recorded: {exc!r}")
        if ask is None:
            ask = next((a for a in asking.pending(asks_path)
                        if a.tool == tool_name and a.cwd == cwd
                        and a.subject == asking.redact(subject)), None)
        ask_id = ask.id if ask is not None else ""
        body = {"behavior": "deny", "ask_id": ask_id,
                "message": _blocked_message(ask_id or "(unrecorded)")}

    _bus_append({
        "ts": time.time(),
        "event": "permission_request",
        "session_id": session_id,
        "tool": tool_name,
        "behavior": body["behavior"],
        "ask_id": body.get("ask_id", ""),
        # True: the deck decided "no rule" and the question was already on the
        # board. False: cc-approve.js never got here -- a timeout, or a deck
        # that was down. Two different faults, told apart by reading.
        "preask": bool(preask),
    })
    return body


@app.post("/api/permission")
async def permission(payload: dict) -> JSONResponse:
    """Answer the prompt Claude Code is about to draw. Never "ask".

    `hooks/cc-permission.js` calls this with a permission prompt half-drawn, so
    the budget is the same as `/api/approve`'s and the floor is inverted: a bug
    in here denies, because a denial is a sentence the agent can read and a
    fall-through is a modal only a human at that window can clear.
    """
    try:
        body = await asyncio.to_thread(_permission, payload)
    except Exception as exc:  # never allow, and never leave a prompt standing
        print(f"[agent-deck] permission request failed: {exc!r}")
        body = {"behavior": "deny", "ask_id": "",
                "message": _blocked_message("(unrecorded)")}
    return JSONResponse(body)


# ── the MCP elicitation dialog ───────────────────────────────────────────────
#
# The third door in the same wall. `/api/approve` answers a tool call before it
# prompts, `/api/permission` answers the prompt itself, and this answers the
# dialog an MCP SERVER raises to ask the desk a question directly. Claude Code
# names the event itself -- "Fired when an MCP server requests user input. Hooks
# can auto-respond (accept/decline) instead of showing the dialog" -- and the
# deck registered nothing for it, so those dialogs were drawn on desks with
# nobody at them.


#: The pseudo-tool an elicitation is filed under in the ask ledger. It is not a
#: real tool name and there is no `tool_input` behind it; what makes it worth a
#: row is that the owner is the only one who can move it along.
ELICITATION_TOOL = asking.ELICITATION_TOOL


def _elicitation_message(ask_id: str, server: str) -> str:
    """What the agent is left holding. Claude Code turns the hook's top-level
    `reason` into its `blockingError`, so this is the whole of what the model
    reads about why the form was not filled in."""
    return (
        f"Declined: the MCP server \"{server}\" asked for input, and no human "
        f"is at this desk to answer it. Nothing was supplied. The Agent Deck "
        f"has put it to the owner as ask {ask_id}. Do not retry it in a loop -- "
        f"say in your reply that you are waiting on ask {ask_id} and carry on "
        "with something else."
    )


def _elicitation(payload: dict) -> dict:
    """Decline, and put the question on the board. Synchronous: worker thread.

    There is no branch that accepts. `hooks/cc-elicit.js` never sends `content`
    and this never asks it to: accepting would mean inventing the value a form
    asked for, and a made-up answer handed to an MCP server is worse than a
    refusal the owner can see and act on.

    So the whole product value is the recorded ask. Without it this would just
    be a faster way to fail.
    """
    server = str(payload.get("mcp_server_name") or "") or "(unnamed server)"
    message = str(payload.get("message") or "").strip() or "(no message)"
    cwd = str(payload.get("cwd") or "")
    session_id = str(payload.get("session_id") or "")

    # `record` redacts, so a schema or a prompt carrying a token never lands.
    subject = f"{server}: {message}"

    ask = None
    try:
        ask = ask_recorder.on_verdict(
            Path(ASKS_PATH),
            verdict={"decision": "ask"},
            tool_name=ELICITATION_TOOL,
            tool_input={"subject": subject},
            cwd=cwd,
            session_id=session_id,
            agent=str(payload.get("agent") or session_id),
        )
    except Exception as exc:  # recording must never become a hung dialog
        print(f"[agent-deck] elicitation not recorded: {exc!r}")
    if ask is None:
        # Suppressed as a repeat, or the write failed. Either way the dialog
        # still has to be answered; find the row so the sentence can name it.
        ask = next((a for a in asking.pending(Path(ASKS_PATH))
                    if a.tool == ELICITATION_TOOL and a.cwd == cwd
                    and a.subject == asking.redact(subject)), None)
    ask_id = ask.id if ask is not None else ""

    _bus_append({
        "ts": time.time(),
        "event": "elicitation",
        "session_id": session_id,
        "mcp_server": server,
        "action": "decline",
        "ask_id": ask_id,
    })
    return {"action": "decline",
            "reason": _elicitation_message(ask_id or "(unrecorded)", server)}


@app.post("/api/elicitation")
async def elicitation(payload: dict) -> JSONResponse:
    """Answer the dialog an MCP server is about to draw. Only ever "decline".

    Same inverted floor as `/api/permission`, for a sharper reason: MEASURED on
    2.1.252, a hook that returns nothing does not fail safe -- the CLI falls
    through and shows the dialog. So a bug in here must still produce a decline,
    or it reintroduces exactly the stall this endpoint exists to remove.
    """
    try:
        body = await asyncio.to_thread(_elicitation, payload)
    except Exception as exc:  # never leave a dialog standing
        print(f"[agent-deck] elicitation failed: {exc!r}")
        body = {"action": "decline",
                "reason": _elicitation_message("(unrecorded)", "an MCP server")}
    return JSONResponse(body)


def _compact(payload: dict) -> dict:
    """Record one compaction. Synchronous: called in a worker thread.

    Total by construction. This runs while a session is mid-compaction and the
    hook that calls it is contractually obliged to swallow every failure, so a
    payload that is not the shape we expected must still produce a line -- a
    dropped record here is indistinguishable from the silence this whole layer
    exists to end.
    """
    session_id = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or "")
    trigger = str(payload.get("trigger") or "auto")
    agent = ""
    try:
        desks = roster.load_roster(Path(ROSTER_PATH))
        agent = next((d.name for d in desks if d.cwd == cwd), "")
    except Exception:  # the roster is a nicety here; the record is not
        pass
    line = compaction.record(Path(BUS_FILE), session_id=session_id,
                             agent=agent, trigger=trigger)
    return {"ok": True, "recorded": line["ts"]}


@app.post("/api/compact")
async def compact(payload: dict) -> JSONResponse:
    """A hired session just forgot most of itself. Put it on the board.

    `hooks/cc-compact.js` has posted here since it was written and this endpoint
    did not exist, so every compaction 404'd into a hook that is required to
    stay quiet. That is layer two of the fix in `server/compaction.py` -- the
    layer that tells you when layer one (the brief in `--append-system-prompt`)
    stopped holding.

    Registered on `PostCompact`, not `PreCompact`: measured on 2.1.252,
    `PreCompact` fires even when the CLI then refuses to compact, so a record
    written from there would be a compaction that never happened.
    """
    try:
        body = await asyncio.to_thread(_compact, payload)
    except Exception as exc:  # never 500 at a session that is already confused
        print(f"[agent-deck] compaction not recorded: {exc!r}")
        body = {"ok": False, "reason": "not_recorded"}
    return JSONResponse(body)


# ── the roster ───────────────────────────────────────────────────────────────

ROSTER_PATH = roster.DEFAULT_PATH
ENGINES = ("claude", "opencode", "codex")


def _find_desk(name: str) -> roster.Desk | None:
    return next((d for d in roster.load_roster(Path(ROSTER_PATH)) if d.name == name), None)


@app.get("/api/roster")
async def roster_list() -> JSONResponse:
    """Every desk, each in the state of whoever is sitting at it.

    A desk nobody is at comes back OFFLINE rather than missing -- that is the
    whole point of a roster, and the thing a closing Terminal tab breaks today.
    """
    desks = await asyncio.to_thread(roster.load_roster, Path(ROSTER_PATH))
    return JSONResponse({"desks": roster.occupancy(desks, _state.get("sessions", []))})


@app.post("/api/roster")
async def roster_upsert(payload: dict) -> JSONResponse:
    """Add a desk, or replace the one with the same name."""
    name = str(payload.get("name") or "").strip()
    cwd = str(payload.get("cwd") or "").strip()
    engine = str(payload.get("engine") or "").strip()
    if not name or not cwd or not engine:
        return JSONResponse(
            {"ok": False, "reason": "need name + cwd + engine"}, status_code=400
        )
    if engine not in ENGINES:
        return JSONResponse(
            {"ok": False, "reason": "unknown_engine", "detail": engine}, status_code=400
        )

    existing = await asyncio.to_thread(_find_desk, name)
    desk = roster.Desk(
        name=name,
        cwd=cwd,
        engine=engine,
        mission=str(payload.get("mission") or ""),
        model=str(payload.get("model") or ""),
        # A rename must not reset the desk's age, so an existing entry keeps its
        # original created_at.
        created_at=float(existing.created_at if existing else 0) or time.time(),
    )
    await asyncio.to_thread(roster.upsert, Path(ROSTER_PATH), desk)
    return JSONResponse({"ok": True, "desk": desk.__dict__})


@app.delete("/api/roster/{name}")
async def roster_remove(name: str) -> JSONResponse:
    if await asyncio.to_thread(_find_desk, name) is None:
        return JSONResponse({"ok": False, "reason": "unknown_desk"}, status_code=404)
    await asyncio.to_thread(roster.remove, Path(ROSTER_PATH), name)
    return JSONResponse({"ok": True})


@app.post("/api/roster/{name}/start")
async def roster_start(name: str) -> JSONResponse:
    """Put a process at this desk: a Terminal window running its engine."""
    desk = await asyncio.to_thread(_find_desk, name)
    if desk is None:
        return JSONResponse({"ok": False, "reason": "unknown_desk"}, status_code=404)
    try:
        # The third door. It has no desk-prefs store to write a reason onto,
        # but it must still clear the same gate: `spawn.start` vouches for the
        # workspace, and the verdict rides back in the response body.
        #
        # `spawn.start`, not `spawn_terminal`. Picking the channel in the
        # caller is exactly how this deck ended up with one install that can
        # hire and one that cannot -- the Linux box reached `osascript` on
        # every hire and died there. The choice lives in spawn now, so a
        # fourth door added later inherits it instead of forgetting it.
        result = await asyncio.to_thread(
            functools.partial(spawn.start, desk,
                              roster_path=Path(ROSTER_PATH)))
    except spawn.SpawnError as exc:
        return JSONResponse(
            {"ok": False, "reason": exc.reason, "detail": exc.detail}, status_code=409
        )
    except ValueError as exc:  # an engine build_argv does not know
        return JSONResponse(
            {"ok": False, "reason": "unknown_engine", "detail": str(exc)},
            status_code=409,
        )
    return JSONResponse(result)


# ── routines ─────────────────────────────────────────────────────────────────

ROUTINES_PATH = routines.DEFAULT_PATH
# The scheduler's resolution. Cron fires on a minute boundary, so this only has
# to be small compared to a minute -- not to a tick of the collector loop.
ROUTINE_TICK_SECONDS = 5.0


def _find_routine(routine_id: str) -> routines.Routine | None:
    path = Path(ROUTINES_PATH)
    return next((r for r in routines.load_routines(path) if r.id == routine_id), None)


def _canonical_agent(name: str) -> str:
    """The name that desk answers to NOW, after any renames it has made.

    Read at delivery time, not when the routine was written: a routine keeps
    the agent name it was created with, and the desk may have named itself at
    any point since.
    """
    aliases = onboard.load_aliases(onboard.aliases_path(Path(ROSTER_PATH)))
    return onboard.resolve(aliases, name) if aliases else name


def _deliver_routine(routine: routines.Routine) -> tuple[bool, str]:
    """Put the routine's prompt in front of its agent.

    Reuses the office path `/api/message` uses: queue it, then inject if the
    agent has a live socket, and ack what was injected so the office hook does
    not deliver the same text a second time on its next turn. An agent that is
    not up right now is not a failure -- the queue is where it waits.

    The agent name is resolved through the rename map first. Without it a desk
    that named itself would have every future firing queued for a name nobody
    occupies -- and it fails the quiet way, because `office.send` accepts any
    string: no error, and a run history full of successes.
    """
    target = _canonical_agent(routine.agent)
    result = office.send(target, routine.prompt, sender="routine")
    if not result.get("ok"):
        return False, str(result.get("detail") or "could not queue")
    # Marked `routine`, matching the `sender` on the record above: it is a
    # schedule HE set arriving, not a third party talking, which is why
    # `office.OWNER_SENDERS` holds it and why the desk must read it as his.
    if _try_inject(target, office.attribute(routine.prompt, "routine")):
        office.ack(result["id"])
        return True, "delivered"
    # Nobody at the socket: wake the desk rather than leave its report on the
    # queue until someone restarts it by hand. The MEASURED case: atlas's
    # 07:57 morning report fired into a desk the daemon had retired.
    woke = _waker.ensure_awake(target, reason="routine")
    if woke.state in ("woken", "restarted"):
        return True, woke.state
    if woke.state == "live":
        return True, "queued"
    return True, f"queued ({woke.detail})"


async def _fire_due_routines(now: float) -> list[str]:
    """Fire everything due at `now`. Returns the ids that fired."""
    path = Path(ROUTINES_PATH)
    loaded = await asyncio.to_thread(routines.load_routines, path)
    fired: list[str] = []

    for routine in routines.due(loaded, now):
        # ADVANCE FIRST. A delivery that raises must not leave this routine due
        # forever: advancing afterwards means every tick re-fires it, which is
        # one message per tick into a session that is trying to work.
        await asyncio.to_thread(routines.advance, path, routine.id, now=now)
        try:
            ok, detail = await asyncio.to_thread(_deliver_routine, routine)
        except Exception as exc:  # one bad routine must not stop the others
            ok, detail = False, repr(exc)
        await asyncio.to_thread(
            routines.record_run, path, routine.id, ts=now, ok=ok, detail=detail
        )
        await asyncio.to_thread(
            _bus_append,
            {"ts": now, "event": "routine", "id": routine.id,
             "agent": routine.agent, "ok": ok},
        )
        fired.append(routine.id)

    return fired


async def _routine_loop() -> None:
    while True:
        try:
            await _fire_due_routines(time.time())
        except Exception as exc:  # never let one bad tick kill the scheduler
            print(f"[agent-deck] routine tick failed: {exc!r}")
        await asyncio.sleep(ROUTINE_TICK_SECONDS)


async def _login_sync_loop() -> None:
    """Read each running desk's browser on a timer and fan a hand-made sign-in
    out to the others. Off the main tick: its docker execs must never stall the
    board, and one bad poll must cost a tick, not the loop."""
    while True:
        await asyncio.sleep(LOGIN_SYNC_SECONDS)
        try:
            report = await asyncio.to_thread(login_vault.sync_running_desks)
            if report.get("synced"):
                print(f"[agent-deck] login sync: {report.get('cookies', 0)} "
                      f"cookie(s), {report.get('origins', 0)} origin(s) to "
                      f"{report.get('desks', 0)} desk(s)", flush=True)
        except Exception as exc:  # noqa: BLE001 - one bad poll is not the loop
            print(f"[agent-deck] login sync failed: {exc!r}", flush=True)


@app.get("/api/routines")
async def routines_list() -> JSONResponse:
    path = Path(ROUTINES_PATH)
    loaded = await asyncio.to_thread(routines.load_routines, path)
    rows = [
        {
            "id": r.id,
            "agent": r.agent,
            "prompt": r.prompt,
            "trigger": r.trigger,
            "next_run_at": r.next_run_at,
            "enabled": r.enabled,
            "runs": await asyncio.to_thread(routines.runs, path, r.id),
        }
        for r in loaded
    ]
    return JSONResponse({"routines": rows})


@app.post("/api/routines")
async def routines_upsert(payload: dict) -> JSONResponse:
    """Add or replace a routine, and schedule its first fire.

    `next_run_at` is computed here and persisted by the module, because the
    LaunchAgent restarts this process and an in-memory schedule dies with it.
    """
    routine_id = str(payload.get("id") or "").strip()
    agent = str(payload.get("agent") or "").strip()
    prompt = str(payload.get("prompt") or "").strip()
    trigger = payload.get("trigger")
    if not routine_id or not agent or not prompt or not isinstance(trigger, dict):
        return JSONResponse(
            {"ok": False, "reason": "need id + agent + prompt + trigger"},
            status_code=400,
        )

    next_run_at = None
    if trigger.get("kind") == "cron":
        try:
            next_run_at = routines.next_fire(
                str(trigger.get("spec", "")), str(trigger.get("tz", "UTC")), time.time()
            )
        except (ValueError, KeyError) as exc:
            # Refuse now rather than write a routine the scheduler can never fire.
            return JSONResponse(
                {"ok": False, "reason": "bad_cron", "detail": str(exc)}, status_code=400
            )

    routine = routines.Routine(
        id=routine_id,
        agent=agent,
        prompt=prompt,
        trigger=trigger,
        next_run_at=next_run_at,
        enabled=bool(payload.get("enabled", True)),
    )
    path = Path(ROUTINES_PATH)
    kept = [r for r in await asyncio.to_thread(routines.load_routines, path)
            if r.id != routine_id]
    await asyncio.to_thread(routines.save_routines, path, kept + [routine])
    return JSONResponse({"ok": True, "routine": {
        "id": routine.id, "agent": routine.agent, "prompt": routine.prompt,
        "trigger": routine.trigger, "next_run_at": routine.next_run_at,
        "enabled": routine.enabled,
    }})


@app.delete("/api/routines/{routine_id}")
async def routines_remove(routine_id: str) -> JSONResponse:
    path = Path(ROUTINES_PATH)
    loaded = await asyncio.to_thread(routines.load_routines, path)
    kept = [r for r in loaded if r.id != routine_id]
    if len(kept) == len(loaded):
        return JSONResponse({"ok": False, "reason": "unknown_routine"}, status_code=404)
    await asyncio.to_thread(routines.save_routines, path, kept)
    return JSONResponse({"ok": True})


@app.get("/feed")
async def feed() -> FileResponse:
    return FileResponse(WEB_DIR / "feed.html")


@app.get("/manager")
async def manager_page() -> FileResponse:
    return FileResponse(WEB_DIR / "manager.html")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


#: How long the browser keeps the cookie `bin/cdash` hands it. A year, so a
#: bookmark he opens next month still shows a board rather than nothing.
COOKIE_MAX_AGE = 365 * 24 * 3600


def _mint_cookie(response, token: str):
    """Trade `?t=<token>` on a page route for a cookie the board replays.

    `HttpOnly` so no script on the page -- ours or anything that ends up
    embedded in it -- can read the token back out. `SameSite=strict` so a page
    on another origin cannot make his browser spend it. `Path=/` because the
    same credential has to cover `/api/stream`.
    """
    response.set_cookie(deckauth.COOKIE, token, max_age=COOKIE_MAX_AGE,
                        httponly=True, samesite="strict", path="/")
    return response


@app.middleware("http")
async def authorise_api(request, call_next):
    """`/api/*` requires the deck's token. Everything else is untouched.

    A middleware rather than a dependency on each route, and that is the point:
    the class of defect here is "a surface protected only by where it is
    bound", and a per-route decorator closes the routes that exist today while
    leaving the next one somebody adds open by default. Three other slices are
    adding routes to this file this week.

    What is deliberately NOT gated:

    * `/v1`, which has carried its own bearer check since it was built and
      answers 503 rather than 401 when no token is configured. That asymmetry
      is on purpose and `tests/test_api_auth.py` pins it.
    * `/`, `/feed`, `/manager`, `/static` and `/media` -- HTML, CSS and JS that
      carry no session data. Gating the page would mean a bookmark returns a
      raw 401 instead of a board, and would buy nothing: the data behind it is
      already refused.

    The page routes are where the browser gets its credential. `bin/cdash`
    opens `/?t=<token>`; this swaps it for a cookie and redirects to the clean
    path so the secret does not stay in the URL bar, in history, or in whatever
    reads it next.
    """
    path = request.url.path

    if path in ("/", "/feed", "/manager"):
        offered = (request.query_params.get(deckauth.QUERY_PARAM) or "").strip()
        if offered and deckauth.matches(offered):
            return _mint_cookie(RedirectResponse(path, status_code=303),
                                offered)
        return await call_next(request)

    if path.startswith("/api/") and not deckauth.authorised(request):
        # The same shape `server/api.py` refuses in, so one client-side branch
        # handles both surfaces.
        return JSONResponse(
            {"ok": False, "reason": "unauthorized",
             "detail": "present the deck token as a bearer header, or open the "
                       "board through bin/cdash"},
            status_code=401, headers={"WWW-Authenticate": "Bearer"})

    return await call_next(request)


#: Response header naming the owner's wire id (`office.OWNER_HANDLE`). The app
#: filters him out of a thread's participants and posts `as` him; it learns the
#: id here instead of shipping one deck's spelling. Not a secret: every thread
#: this token can read already carries it.
OWNER_HEADER = "X-Deck-Owner"


#: His display name (`owner.name()`: `DECK_OWNER_NAME`, then deck.toml), so the
#: apps can say "Sam" where they said "Owner". Percent-encoded because a
#: header is ASCII; absent when no name is configured, so the app keeps its
#: own fallback instead of drawing "the owner".
OWNER_NAME_HEADER = "X-Deck-Owner-Name"


@app.middleware("http")
async def name_the_owner(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/v1/"):
        response.headers[OWNER_HEADER] = office.OWNER_HANDLE
        from . import owner as owner_mod
        from urllib.parse import quote
        named = owner_mod.name()
        if named and named != owner_mod.DEFAULT:
            response.headers[OWNER_NAME_HEADER] = quote(named, safe="")
    return response


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Never let a browser keep yesterday's CSS.

    The pages link /static/style.css with no version, so without this an open
    tab silently runs old styles after an update -- which is exactly how the
    manager page first shipped looking unstyled.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["cache-control"] = "no-cache, must-revalidate"
    return response


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
