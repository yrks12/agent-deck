"""`/api/*` must authenticate — and must still answer the people it is for.

**The defect.** `/v1` has been behind a bearer token since it was built. The
deck's own `/api/*` — the board, the org chart, the hire-a-desk route, and the
three hook doors every tool call in every hired session passes through — was
behind nothing at all. Its only protection was where the socket was bound, and
that is not a protection:

* MEASURED on this Mac, with a throwaway `alpine` container and both hostname
  aliases blackholed exactly as `server/sandbox.py` blackholes them:
  `wget -O- http://192.168.65.254:7788/api/state` returned **112,835 bytes** of
  the owner's live board. Docker Desktop's VM gateway goes around a 127.0.0.1
  bind.
* MEASURED, and the reason a peer-address rule is not the fix: a probe server
  on 127.0.0.1 logged that same container's connection as
  `('127.0.0.1', 55491)` — byte-identical to the host's own curl. "Only accept
  loopback" would have let the container straight through.
* `deploy/install-deck.sh` binds `10.99.0.1` (BIND_ADDR), a WireGuard address,
  so on the Linux box the board is offered to every peer on the mesh and only
  `ufw` stands in front of it.

**Both halves, deliberately.** A route that answered 401 to everybody would
pass a refusal-only test and leave him with a dead board and every hired desk
prompting on every tool call. So each door here is pinned twice: refused
without a credential, and *serving real data* with one.

**Never 503.** `/v1` answers 503 when no token is configured because a phone's
token has to be handed over out of band and an operator must notice. `/api` has
no such state: every one of its callers runs on this machine as the owner and
can read a 0600 file, so the daemon MINTS a token rather than locking him out.
Fail closed means "never open", not "sometimes bricked" —
`test_api_is_closed_and_usable_with_no_token_in_the_environment` is that pin.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod

try:  # the module this change adds; absent, every test below must still FAIL
    from server import deckauth   # on behaviour, with a readable message,
except ImportError:               # never on a collection error.
    deckauth = None

HOOKS = Path(__file__).resolve().parent.parent / "hooks"

TOKEN = "test-token-not-a-real-one"
#: The contract, restated here rather than imported: a test that reads the
#: cookie name off the code under test cannot fail when the code renames it.
COOKIE = "deck_auth"
TOKEN_ENV = "AGENT_DECK_TOKEN"

#: Every `/api` door, and what a caller who reaches it gets. `/api/state` is
#: the 110 KB the container pulled; the three hook doors are the ones that can
#: answer a permission prompt in somebody else's session.
READ_DOORS = ["/api/state", "/api/comms", "/api/roster", "/api/routines",
              "/api/media", "/api/money"]
WRITE_DOORS = [
    ("/api/approve", {"tool_name": "Bash", "tool_input": {"command": "ls"},
                      "session_id": "s", "cwd": "/x"}),
    ("/api/permission", {"tool_name": "Bash", "tool_input": {"command": "ls"},
                         "session_id": "s", "cwd": "/x"}),
    ("/api/message", {"to": "nobody", "text": "hello"}),
]


@pytest.fixture
def token_file(short_tmp, monkeypatch):
    """A throwaway token file. Never the owner's real `deck-token.txt`:
    rewriting that would strand `bin/deck` and every hired desk's hooks."""
    path = short_tmp / "deck-token.txt"
    if deckauth is not None:
        monkeypatch.setattr(deckauth, "TOKEN_PATH", path)
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    return path


def bare(client):
    """Strip the credential `conftest.the_suite_is_an_authorised_caller` hands
    every `TestClient`, because this is the file that tests the credential.

    That fixture exists so seventeen unrelated test files did not have to grow
    an Authorization header. Here it would hide the entire defect: a client
    that always authorises can never show that a caller without a token is
    refused.
    """
    client.headers.pop("authorization", None)
    return client


@pytest.fixture
def hermetic(short_tmp, monkeypatch):
    """Every product-state path `/api` writes through, pointed at tmp.

    `POST /api/approve` records the question and `POST /api/message` queues it,
    so without this the refusal tests below evict rows from the approval board
    he reads on his phone. `tests/conftest.py`'s autouse guard catches that and
    it is not disabled here — this is how a test satisfies it.
    """
    for attr in ("AUTOREVIEW_PATH", "ASKS_PATH", "BUS_FILE", "ROSTER_PATH",
                 "ROUTINES_PATH"):
        monkeypatch.setattr(app_mod, attr, short_tmp / f"{attr.lower()}.json")
    from server import office
    monkeypatch.setattr(office, "BUS_DIR", short_tmp)
    monkeypatch.setattr(office, "OFFICE_FILE", short_tmp / "office.json")
    monkeypatch.setattr(office, "MESSAGES_FILE", short_tmp / "messages.jsonl")


@pytest.fixture
def client(token_file, hermetic):
    with TestClient(app_mod.app) as c:
        yield bare(c)


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# ── the refusal, on every door ───────────────────────────────────────────────


@pytest.mark.parametrize("path", READ_DOORS)
def test_a_credentialless_reader_is_refused(client, path):
    """The container's exact request. 200 here is the defect."""
    r = client.get(path, headers={})
    assert r.status_code == 401, f"{path} -> {r.status_code} {r.text[:120]}"


@pytest.mark.parametrize("path,payload", WRITE_DOORS)
def test_a_credentialless_writer_is_refused(client, path, payload):
    """The hook doors matter more than the board: `/api/approve` decides
    whether a tool call in somebody else's session runs, and `/api/permission`
    answers a prompt that is already on screen."""
    r = client.post(path, json=payload, headers={})
    assert r.status_code == 401, f"{path} -> {r.status_code} {r.text[:120]}"


def test_the_stream_is_refused_too(token_file, hermetic):
    """`/api/stream` is the whole board, once a second, forever. A guard that
    covers the snapshot and not the stream closes nothing.

    A real uvicorn on a real socket, and one raw status line read with a socket
    timeout — not `TestClient`. `TestClient` drives the app in-process through a
    portal that ignores httpx timeouts, so an unguarded SSE route hangs the
    suite there instead of failing it, and a detector that hangs is a detector
    that gets deleted. This is also the shape the container used.
    """
    import socket
    import threading

    import uvicorn

    config = uvicorn.Config(app_mod.app, host="127.0.0.1", port=0,
                            log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn never came up"
        port = server.servers[0].sockets[0].getsockname()[1]

        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b"GET /api/stream HTTP/1.0\r\n"
                         b"Host: 127.0.0.1\r\n\r\n")
            status = sock.recv(64).split(b"\r\n")[0]
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert b"401" in status, f"/api/stream answered {status!r}"


def test_a_wrong_token_is_refused(client):
    r = client.get("/api/state", headers=auth("not-the-token"))
    assert r.status_code == 401


# ── the good signal: it still serves the people it is for ────────────────────


@pytest.mark.parametrize("path", READ_DOORS)
def test_the_bearer_token_still_gets_the_data(client, path):
    """`bin/deck`, `bin/cdash` and `bin/deck-acceptance` present this."""
    r = client.get(path, headers=auth())
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:120]}"


def test_the_board_is_real_data_and_not_an_empty_shell(client):
    """A guard that returned `{}` to everyone would pass every test above."""
    body = client.get("/api/state", headers=auth()).json()
    assert "sessions" in body and "generated_at" in body


def test_the_approve_door_still_decides(client):
    """The hot path: a decision, in the shape `hooks/cc-approve.js` parses."""
    r = client.post("/api/approve", headers=auth(), json={
        "tool_name": "Bash", "tool_input": {"command": "git status"},
        "session_id": "s", "cwd": "/x"})
    assert r.status_code == 200
    assert r.json().get("decision") in {"allow", "deny", "ask", "abstain"}


def test_the_browser_gets_a_cookie_and_then_the_board(hermetic, token_file):
    """His board has never sent a header in its life and `EventSource` cannot
    be made to send one. So the page route takes the token once, in the URL
    `bin/cdash` opens, and hands back a cookie the browser replays on every
    fetch — `web/app.js` is untouched by this change.
    """
    with TestClient(app_mod.app) as c:
        bare(c)
        landing = c.get(f"/?t={TOKEN}", follow_redirects=False)
        assert landing.status_code in (200, 303, 307)
        assert COOKIE in landing.cookies, (
            "the page route did not mint a cookie; the board stays blank")
        # Same client, no header anywhere — exactly what the browser does.
        assert c.get("/api/state").status_code == 200


def test_a_wrong_token_in_the_url_mints_nothing(hermetic, token_file):
    with TestClient(app_mod.app) as c:
        bare(c)
        c.get("/?t=wrong", follow_redirects=False)
        assert c.get("/api/state").status_code == 401


# ── fail closed without bricking him ─────────────────────────────────────────


def test_api_is_closed_and_usable_with_no_token_in_the_environment(hermetic, short_tmp, monkeypatch):
    """The state `/v1` answers 503 in. `/api` must not: the daemon runs from a
    LaunchAgent that may carry no environment at all, and 503 there is a dead
    board plus every hired desk prompting on every tool call.

    So it mints one, 0600, and stays closed to anyone who cannot read it.
    """
    assert deckauth is not None, "server/deckauth.py does not exist yet"
    path = short_tmp / "minted.txt"
    monkeypatch.setattr(deckauth, "TOKEN_PATH", path)
    monkeypatch.delenv(TOKEN_ENV, raising=False)

    with TestClient(app_mod.app) as c:
        bare(c)
        assert c.get("/api/state").status_code == 401
        minted = path.read_text().strip()
        assert len(minted) >= 32, "a guessable token is not a guard"
        assert path.stat().st_mode & 0o077 == 0, (
            "the token file is readable by other users on this machine")
        assert c.get("/api/state", headers=auth(minted)).status_code == 200


def test_the_env_token_is_mirrored_to_the_file_the_hooks_read(hermetic, token_file):
    """One secret per daemon, one place to read it. The box's token arrives in
    the systemd environment and the Mac's in the LaunchAgent plist; neither is
    readable by a hook, so the daemon writes it where they look."""
    with TestClient(app_mod.app):
        pass
    assert token_file.read_text().strip() == TOKEN
    assert token_file.stat().st_mode & 0o077 == 0


def test_v1_keeps_its_own_503(hermetic, short_tmp, monkeypatch):
    """The asymmetry is deliberate and this change must not erase it."""
    if deckauth is not None:
        monkeypatch.setattr(deckauth, "TOKEN_PATH", short_tmp / "t.txt")
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with TestClient(app_mod.app) as c:
        assert bare(c).get("/v1/agents").status_code == 503


def test_the_pages_and_static_files_are_not_gated(client):
    """Only `/api` moves. The HTML shell carries no session data, and gating it
    would mean a bookmark returns a raw 401 instead of a board."""
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200


# ── the hook budget ──────────────────────────────────────────────────────────


def _run_hook(name, payload, env_extra, expect_token=TOKEN):
    """Run a real hook against a server that REQUIRES the bearer token, so a
    hook that forgot to send it fails here rather than in production."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = {}

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            seen["auth"] = self.headers.get("Authorization")
            ok = seen["auth"] == f"Bearer {expect_token}"
            body = json.dumps(
                {"decision": "allow", "rule_id": "a"} if ok
                else {"ok": False, "reason": "unauthorized"}).encode()
            self.send_response(200 if ok else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            """pytest output is not an access log."""

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        env = {**os.environ,
               "DECK_URL": f"http://127.0.0.1:{httpd.server_address[1]}",
               **env_extra}
        started = time.monotonic()
        proc = subprocess.run(["node", str(HOOKS / name)],
                              input=json.dumps(payload), capture_output=True,
                              text=True, env=env, timeout=10)
        return proc, time.monotonic() - started, seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


@pytest.fixture
def hook_token(short_tmp):
    """The file a hired desk's hook reads. `server/approval.py` names it in the
    generated settings' `env`; the secret itself never enters that file."""
    path = short_tmp / "deck-token.txt"
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("name", ["cc-approve.js", "cc-permission.js",
                                 "cc-elicit.js", "cc-compact.js"])
def test_every_http_hook_presents_the_token(name, hook_token):
    """All four doors, not just the loud one. A hook that stopped sending the
    header would 401 and — for the two on the permission path — turn every
    tool call into a prompt."""
    proc, _, seen = _run_hook(
        name,
        {"tool_name": "Bash", "tool_input": {"command": "ls"},
         "session_id": "s", "cwd": "/x", "trigger": "manual"},
        {"DECK_TOKEN_FILE": str(hook_token)})
    assert proc.returncode == 0
    assert seen.get("auth") == f"Bearer {TOKEN}", (
        f"{name} sent {seen.get('auth')!r}")


def test_the_approver_still_allows_and_stays_inside_budget(hook_token):
    """The GOOD signal on the hot path, plus the cost. This runs before every
    Bash/Read/Write/Edit in every hired session; a token lookup that added a
    round trip would turn the whole machine into prompts."""
    proc, elapsed, _ = _run_hook(
        "cc-approve.js",
        {"tool_name": "Bash", "tool_input": {"command": "git status"},
         "session_id": "s", "cwd": "/x"},
        {"DECK_TOKEN_FILE": str(hook_token)})
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert elapsed < 2.0, f"the approver took {elapsed:.2f}s"


def test_the_approver_asks_rather_than_hangs_when_the_token_is_missing(
        short_tmp):
    """The control. An unreadable token file must fail the way an unreachable
    daemon already does — one `ask`, inside budget — not an exception, not a
    stall, and never an `allow`."""
    proc, elapsed, _ = _run_hook(
        "cc-approve.js",
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"},
         "session_id": "s", "cwd": "/x"},
        {"DECK_TOKEN_FILE": str(short_tmp / "no-such-file")})
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert elapsed < 4.0


def test_the_generated_settings_name_the_token_file_but_never_the_secret(
        short_tmp, monkeypatch):
    """What a hired desk is told. The path, so the hook can find it; not the
    token, because that file sits in the bus beside world-readable siblings and
    a rotated token would strand every desk hired before the rotation."""
    from server import approval

    assert deckauth is not None, "server/deckauth.py does not exist yet"
    monkeypatch.setattr(deckauth, "TOKEN_PATH", short_tmp / "deck-token.txt")
    doc = approval.settings_document("/usr/bin/node")
    assert doc["env"]["DECK_TOKEN_FILE"] == str(short_tmp / "deck-token.txt")
    assert TOKEN not in json.dumps(doc)
    assert "DECK_TOKEN" not in doc["env"]


# ── K3: device tokens, the rate limit, and the pair door on the real daemon ──
#
# `server/pair_api.py` has the gate; these pin that the daemon actually USES it.

from server import api as api_mod  # noqa: E402
from server import ratelimit  # noqa: E402
from server.pairing import DeviceStore  # noqa: E402

REMOTE = ("203.0.113.9", 51000)     # a real peer: not loopback
LOCAL = ("127.0.0.1", 51000)        # the owner's own Mac, no proxy in front
WRONG = "not-the-token"


@pytest.fixture
def gated(token_file, hermetic, tmp_path, monkeypatch):
    """The daemon's real /v1 gate with its state pointed at tmp."""
    gate = api_mod._GATE
    assert gate is not None, "server/app.py never wired pair_api's gate into api._authorise"
    monkeypatch.setattr(gate, "devices", DeviceStore(tmp_path / "state"))
    monkeypatch.setattr(gate, "limiter", ratelimit.RateLimiter())
    return gate


def _peer(peer):
    client = TestClient(app_mod.app, client=peer)
    return bare(client)


def test_the_pair_door_and_healthz_are_mounted_on_the_daemon(gated):
    c = _peer(REMOTE)
    assert c.get("/healthz").json() == {"ok": True}
    r = c.post("/v1/pair", json={})
    assert r.status_code == 400 and r.json()["reason"] == "pair_malformed"


def test_a_device_token_opens_v1_until_it_is_revoked(gated):
    token, device = gated.devices.add("Dan's phone")
    c = _peer(REMOTE)
    assert c.get("/v1/agents", headers=auth(token)).status_code == 200
    gated.devices.revoke(device.id)
    assert c.get("/v1/agents", headers=auth(token)).status_code == 401


def test_the_master_token_answers_exactly_as_before(gated):
    c = _peer(REMOTE)
    assert c.get("/v1/agents", headers=auth()).status_code == 200
    r = c.get("/v1/agents", headers=auth(WRONG))
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert r.json()["reason"] == "unauthorized"


def test_ten_wrong_tokens_ban_the_address_even_for_the_right_one(gated):
    c = _peer(REMOTE)
    for _ in range(10):
        assert c.get("/v1/agents", headers=auth(WRONG)).status_code == 401
    r = c.get("/v1/agents", headers=auth())
    assert r.status_code == 429 and r.json()["reason"] == "rate_limited"
    assert int(r.headers["retry-after"]) > 0
    # a different address is untouched
    assert _peer(("203.0.113.10", 1)).get("/v1/agents", headers=auth()).status_code == 200


def test_a_missing_token_is_not_a_guess_and_never_bans(gated):
    c = _peer(REMOTE)
    for _ in range(25):
        assert c.get("/v1/agents").status_code == 401
    assert c.get("/v1/agents", headers=auth()).status_code == 200


def test_wrong_tokens_never_lock_out_the_owners_own_mac(gated):
    """The owner's deck has no deck.toml, so it runs `public`. A loopback caller
    with no X-Forwarded-For is the Mac itself (`bin/deck`, hooks, the board),
    not Caddy. Banning it would strand every desk's hooks for 30 minutes."""
    c = _peer(LOCAL)
    for _ in range(30):
        assert c.get("/v1/agents", headers=auth(WRONG)).status_code == 401
    assert c.get("/v1/agents", headers=auth()).status_code == 200
    assert gated.limiter.banned("127.0.0.1") == 0


def test_the_proxy_on_loopback_is_not_exempt_its_forwarded_client_is_banned(gated):
    """Caddy is loopback too. What tells it from the Mac is X-Forwarded-For."""
    c = _peer(LOCAL)
    fwd = {"X-Forwarded-For": "198.51.100.7"}
    for _ in range(10):
        assert c.get("/v1/agents", headers={**auth(WRONG), **fwd}).status_code == 401
    assert c.get("/v1/agents", headers={**auth(), **fwd}).status_code == 429
    # ...and the Mac itself, same socket, is still welcome
    assert c.get("/v1/agents", headers=auth()).status_code == 200


def test_a_local_caller_can_use_a_device_token_too(gated):
    token, _ = gated.devices.add("laptop on the box")
    assert _peer(LOCAL).get("/v1/agents", headers=auth(token)).status_code == 200


def test_unset_master_token_is_still_503_not_open(gated, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    r = _peer(REMOTE).get("/v1/agents", headers=auth(WRONG))
    assert r.status_code == 503 and r.json()["reason"] == "auth_not_configured"
