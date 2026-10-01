"""Who is allowed to talk to `/api` — the deck's own surface.

**The hole.** `/v1` has been behind a bearer token since it was built. `/api`
— the board, the org chart, the hire-a-desk route, and the three hook doors
every tool call in every hired session passes through — was behind nothing but
where the socket happened to be bound. Two things made that stop being
survivable in the same week:

1. Each desk now gets its own Docker container. MEASURED: a throwaway
   `alpine` container with `host.docker.internal` and `gateway.docker.internal`
   both blackholed — exactly as `server/sandbox.py` blackholes them — still
   pulled **112,835 bytes** of the live board out of
   `http://192.168.65.254:7788/api/state`. Docker Desktop's VM gateway goes
   around a `127.0.0.1` bind and offers no container-side flag that closes it
   without also killing the internet the desk's browser needs. It is a
   host-side problem, so this is the host side.
2. `deploy/install-deck.sh` binds `BIND_ADDR`, which can be a WireGuard
   address. `ufw` limits the port to `wg0`, so the exposure is the
   mesh rather than the internet, but "a firewall is the only thing in front of
   an unauthenticated board" is not an authorisation story.

**Why not a peer-address rule.** MEASURED and decisive: a probe server on
`127.0.0.1` logged the container's connection as `('127.0.0.1', 55491)` —
byte-for-byte what the host's own `curl` looks like. On Docker Desktop the VM's
proxy is the peer, so "only accept loopback" would have let the container
straight through while feeling like a fix. Only a secret separates the callers.

**Why `/api` mints instead of answering 503.** `server/api.py` returns 503 when
`AGENT_DECK_TOKEN` is unset, and it is right to: a phone's token has to be
handed over out of band, so an operator must be made to notice. `/api` has no
such caller. Everything that calls it — the board in his browser, `bin/deck`,
`bin/cdash`, `bin/deck-acceptance`, and the hooks in every hired session — runs
on this machine as the owner and can read a `0600` file. So the unconfigured
state is *removed* rather than refused: the daemon writes a token the first
time it needs one. Fail closed means "never open"; it does not mean "sometimes
a dead board and every desk on this Mac prompting at every tool call", which is
the worse outcome and the one a 503 here would produce.

**One secret per daemon, one place to read it.** `AGENT_DECK_TOKEN` still wins
when it is set — it already is, in the LaunchAgent plist on this Mac and in
`/etc/agent-deck/agentdeck.env` on the box — because two tokens would mean two
things to rotate. But neither of those is readable by a hook or a shell script,
so whatever the effective token is, it is mirrored to `TOKEN_PATH` at `0600`.
That file already existed on this machine, minted by hand and matching the
plist; this is the code that was missing behind it.

**Two ways to present it, for two kinds of caller.**

* `Authorization: Bearer <token>` — every programmatic caller.
* A cookie — the browser. `EventSource` cannot be given a header at all, so a
  header-only scheme would have meant rewriting the board's transport. Instead
  `bin/cdash` opens the page with `?t=<token>` once, the page route swaps that
  for an `HttpOnly` cookie, and every `fetch` and the SSE stream carry it
  afterwards without `web/app.js` knowing this happened.

The cookie is `HttpOnly` (no script on the page can read the token back out),
`SameSite=Strict` (a page on another origin cannot make his browser spend it),
and `Path=/` so the stream and the API share it.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading

from . import paths

#: The one env var. Shared with `server/api.py` deliberately: a daemon has one
#: secret, and `/v1` and `/api` are two doors into the same house.
TOKEN_ENV = "AGENT_DECK_TOKEN"

#: Where every local caller reads it. `0600`, in the bus directory, honouring
#: `CLAUDE_CONFIG_DIR` through `paths` so a test never lands on the real one.
TOKEN_PATH = paths.BUS_DIR / "deck-token.txt"

#: The browser's copy.
COOKIE = "deck_auth"
#: The query parameter that trades a token for that cookie, on the page routes
#: only. Never on `/api`: a token in a query string lands in logs, in shell
#: history and in whatever the next process reads off the URL bar, so it is
#: spent once at the door and never again.
QUERY_PARAM = "t"

#: Enough that guessing is not a strategy; `secrets.token_urlsafe(32)` is 43
#: characters, which is what the token already on this machine measures.
TOKEN_BYTES = 32

_lock = threading.Lock()


def _read_file() -> str:
    try:
        return TOKEN_PATH.read_text().strip()
    except OSError:
        return ""


def _write_file(value: str) -> None:
    """Put the token where a hook and a shell script can read it, and nobody
    else can. Never raises: a read-only bus costs the hooks their credential,
    which they already handle by asking, and must not cost the daemon its
    startup."""
    try:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Create at 0600 rather than write-then-chmod: the window between the
        # two is a window in which the secret is world-readable.
        fd = os.open(str(TOKEN_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o600)
        try:
            os.write(fd, (value + "\n").encode())
        finally:
            os.close(fd)
        os.chmod(TOKEN_PATH, 0o600)  # an existing file keeps its old mode
    except OSError:
        pass


def token() -> str:
    """The daemon's effective token, materialised where its callers look.

    Order: the environment, then the file, then a fresh one. Whichever wins is
    mirrored to `TOKEN_PATH` when what is on disk disagrees — that mirror is
    the whole distribution mechanism for the hooks and `bin/`.
    """
    with _lock:
        stated = (os.environ.get(TOKEN_ENV) or "").strip()
        current = _read_file()
        chosen = stated or current or secrets.token_urlsafe(TOKEN_BYTES)
        if chosen != current:
            _write_file(chosen)
        return chosen


def presented(request) -> str:
    """What this caller offered, header first.

    Header before cookie so a deliberate programmatic call is never overridden
    by a stale cookie the same browser happens to be carrying.
    """
    scheme, _, value = (
        request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return (request.cookies.get(COOKIE) or "").strip()


def matches(candidate: str) -> bool:
    """Constant-time, and empty is never a match.

    `hmac.compare_digest` on two empty strings is True, so the emptiness check
    is not decoration: without it a caller who sends `Authorization: Bearer `
    would be authorised the moment anything upstream produced an empty token.
    """
    expected = token()
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)


def authorised(request) -> bool:
    return matches(presented(request))
