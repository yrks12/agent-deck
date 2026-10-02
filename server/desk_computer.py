"""A desk drives its own computer: navigate, read, look, click, type.

MEASURED on the box, 2026-09-28: `deck-desk-atlas` had been up 21 days and no
desk had ever touched it -- zero uses of xdotool, `docker exec` or chromium in
any transcript. The desk's Claude runs on the host, the DevTools port is in
the container's private netns, and nothing told the desk the machine existed.

**How it gets in -- through the same door as the owner's panel.** Every
command here is `sandbox.exec_argv`: one named container, uid 1000, display
:99. DevTools stays bound to 127.0.0.1 *inside* the container and is never
published; `relay_argv` is a `docker exec --interactive` running a constant
bash line that dials that loopback port, so the bytes cross the boundary on
docker's own unix socket, exactly as a frame grab does.

**Why a hand-written WebSocket client, when docs/the-browser.md preferred the
`websockets` package.** That plan put the dependency in an in-container venv;
the image has no Python, and rebuilding it means recreating atlas's container.
The peer here is our own Chromium on loopback, the channel carries only CDP
text frames, and `frame`/`read_message` are ~40 lines with a size cap. The
deck stays stdlib-only and the running containers stay untouched.

**On first use, not for every desk.** `ensure` starts the container and the
browser the first time a desk calls a tool. MEASURED: atlas's container holds
673 MiB on a 7.9 GiB box already at load 10 on 4 cores, and zero desks had
used one in 21 days -- eight idle Chromiums would be 5 GiB spent on nothing.
A cold start is ~0.3 s for the container plus a second or two for Chromium.

**It stops at a login, a code, a captcha or a payment.** `_guard` runs
`browser_takeover.needs_human` on the page before any click, keystroke or
typed text, and refuses; the owner takes over on the same screen and the
sign-in is then the desk's. `docs/the-browser.md` names that detector as the
guard upstream of the driver -- this is where it now sits.

Stdlib only.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import select
import string
import subprocess
import time
from urllib.parse import urlsplit

from . import (browser, browser_reaper, browser_takeover, handoff, login_vault,
               sandbox, vault)
from .browser_cdp import CDPDriver

CDP_HOST = browser.CDP_BIND
CDP_PORT = browser.CDP_PORT_BASE

#: The whole relay, and a constant: nothing a desk sends reaches this string.
#: The reader is killed when the host closes stdin, so no connection outlives
#: the call that opened it.
RELAY = (f"exec 3<>/dev/tcp/{CDP_HOST}/{CDP_PORT} || exit 7; "
         "cat <&3 & r=$!; cat >&3; kill $r 2>/dev/null")
RELAY_SECONDS = 30
WAIT = 20.0
MAX_MESSAGE = 8 * 1024 * 1024
TEXT_MAX = 20_000

_TARGET_PATH = re.compile(r"\A/devtools/page/[A-Za-z0-9-]{1,64}\Z")

#: A fixed expression. There is no door for arbitrary JavaScript: a driver
#: that runs whatever it is handed in a signed-in browser can exfiltrate it.
_TEXT_JS = ("(() => ({url: location.href, title: document.title, "
            "text: document.body ? document.body.innerText : ''}))()")


def relay_argv(desk: str, seconds: int = RELAY_SECONDS) -> list[str]:
    """A byte pipe to 127.0.0.1:9222 inside `desk`'s container. PURE.

    `seconds` caps its life; only the mobile-mode holder asks for long."""
    argv = sandbox.exec_argv(desk, ["timeout", str(int(seconds)),
                                    "bash", "-c", RELAY])
    argv.insert(2, "--interactive")
    sandbox._sweep(argv)
    return argv


def launch_argv(desk: str) -> list[str]:
    """`sandbox.browser_argv`, detached, so no host process holds it. PURE."""
    argv = sandbox.browser_argv(desk)
    argv.insert(2, "--detach")
    return argv


def target_path(ws_url: str) -> str:
    """The path of a page target on OUR loopback port, or ValueError.

    It came out of Chromium's own target list, but it is spliced into a
    request line, so it is checked: host, port, shape, and no CR/LF.
    """
    parts = urlsplit(str(ws_url))
    if (parts.scheme != "ws" or parts.hostname != CDP_HOST
            or parts.port != CDP_PORT or not _TARGET_PATH.match(parts.path)):
        raise ValueError(f"not a page on {CDP_HOST}:{CDP_PORT}: {ws_url!r}")
    return parts.path


class _Pipe:
    """One `docker exec` relay: bytes in, bytes out, on a deadline."""

    def __init__(self, desk: str, seconds: int = RELAY_SECONDS) -> None:
        self.proc = subprocess.Popen(relay_argv(desk, seconds),
                                     stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.buf = b""

    def write(self, data: bytes) -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def _fill(self, deadline: float) -> None:
        fd = self.proc.stdout.fileno()
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            raise sandbox.SandboxError("computer_not_responding",
                                       "the browser did not answer")
        chunk = os.read(fd, 65536)
        if not chunk:
            raise sandbox.SandboxError("cdp_unreachable",
                                       "the browser closed the connection")
        self.buf += chunk

    def read(self, n: int, timeout: float = WAIT) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self.buf) < n:
            self._fill(deadline)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_until(self, marker: bytes, timeout: float = WAIT) -> bytes:
        deadline = time.monotonic() + timeout
        while marker not in self.buf:
            if len(self.buf) > 65536:
                raise sandbox.SandboxError("cdp_unreachable", "no header end")
            self._fill(deadline)
        head, _, self.buf = self.buf.partition(marker)
        return head

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


def frame(payload: bytes, opcode: int = 1, *, mask: bytes | None = None) -> bytes:
    """One client WebSocket frame: FIN, masked, as RFC 6455 requires. PURE."""
    key = mask or os.urandom(4)
    n = len(payload)
    if n < 126:
        head = bytes([0x80 | opcode, 0x80 | n])
    elif n < 65536:
        head = bytes([0x80 | opcode, 0x80 | 126]) + n.to_bytes(2, "big")
    else:
        head = bytes([0x80 | opcode, 0x80 | 127]) + n.to_bytes(8, "big")
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def read_message(pipe, timeout: float = WAIT) -> str:
    """One whole text message; answers pings, skips pongs, caps the size.

    `timeout` bounds the wait for a message to START; once one has begun,
    its remaining bytes get the usual `WAIT`."""
    parts: list[bytes] = []
    first = True
    while True:
        b0, b1 = pipe.read(2, timeout) if first else pipe.read(2)
        first = False
        n = b1 & 0x7F
        if n == 126:
            n = int.from_bytes(pipe.read(2), "big")
        elif n == 127:
            n = int.from_bytes(pipe.read(8), "big")
        key = pipe.read(4) if b1 & 0x80 else b""
        if n + sum(map(len, parts)) > MAX_MESSAGE:
            raise sandbox.SandboxError("cdp_unreachable", "message over cap")
        data = pipe.read(n) if n else b""
        if key:
            data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
        opcode = b0 & 0x0F
        if opcode == 8:
            raise sandbox.SandboxError("cdp_unreachable", "browser closed")
        if opcode == 9:
            pipe.write(frame(data, 10))
            continue
        if opcode == 10:
            continue
        parts.append(data)
        if b0 & 0x80:
            return b"".join(parts).decode("utf-8", "replace")


class _Socket:
    """What `CDPDriver.send` expects of `websockets`: send, recv, with."""

    def __init__(self, pipe, path: str) -> None:
        self.pipe = pipe
        key = base64.b64encode(os.urandom(16)).decode()
        pipe.write((f"GET {path} HTTP/1.1\r\nHost: {CDP_HOST}:{CDP_PORT}\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        if not pipe.read_until(b"\r\n\r\n").startswith(b"HTTP/1.1 101"):
            pipe.close()
            raise sandbox.SandboxError("cdp_unreachable", "no upgrade")

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.pipe.write(frame(b"", 8))
        except OSError:
            pass
        self.pipe.close()

    def send(self, text: str) -> None:
        self.pipe.write(frame(text.encode()))

    def recv(self, timeout: float = WAIT) -> str:
        return read_message(self.pipe, timeout)


class ContainerCDP(CDPDriver):
    """`CDPDriver`, with its two network calls routed through the relay."""

    #: How long one command connection may live (the holder raises it).
    relay_seconds = RELAY_SECONDS

    def __init__(self, desk: str) -> None:
        super().__init__(browser.BrowserSession(
            desk=browser._check_desk(desk), display=sandbox.DISPLAY,
            cdp_port=CDP_PORT, profile_dir=sandbox.DESK_PROFILE, pid=None,
            started_at=0.0))

    @property
    def base(self) -> str:
        return f"{sandbox.container_name(self.sess.desk)}:{CDP_PORT}"

    def _get(self, path: str) -> object:
        pipe = _Pipe(self.sess.desk)
        try:
            pipe.write((f"GET {path} HTTP/1.1\r\nHost: {CDP_HOST}:{CDP_PORT}"
                        "\r\nConnection: close\r\n\r\n").encode())
            head = pipe.read_until(b"\r\n\r\n")
            size = re.search(rb"(?im)^content-length:\s*(\d+)", head)
            if not head.startswith(b"HTTP/1.1 200") or not size:
                raise browser.BrowserError("cdp_unreachable", self.base)
            return json.loads(pipe.read(int(size.group(1))))
        except (sandbox.SandboxError, ValueError, OSError) as exc:
            raise browser.BrowserError("cdp_unreachable", str(exc)) from exc
        finally:
            pipe.close()

    def _connect(self):
        if not self._ws_url:
            self.wait_for_page(timeout=WAIT)
        return _Socket(_Pipe(self.sess.desk, self.relay_seconds),
                       target_path(self._ws_url))


# ── what the desk's tools call ──────────────────────────────────────────────


def ensure(desk: str) -> ContainerCDP:
    """The desk's computer and its browser, started on first use.

    Marks the desk as used (`browser_reaper` stops idle browsers). A
    container comes up only through `browser_reaper.admit`, which holds the
    live-browser cap across every process and refuses `browsers_full` at it.
    """
    browser_reaper.touch(desk)
    if not sandbox.is_up(desk):
        browser_reaper.admit(desk, sandbox.start)
    driver = ContainerCDP(desk)
    launched = driver.page_target() is None
    if launched:
        sandbox._run(launch_argv(desk))
    driver.wait_for_page(timeout=WAIT)
    if launched:  # a fresh browser gets the owner's logins (login_vault.py)
        login_vault.seed(desk, driver)
    return driver


def _needs_human(desk: str) -> str | None:
    return browser_takeover.needs_human(ensure(desk).page_state())


def _raise_card(sess: browser.BrowserSession, kind: str, url: str, *,
                passkey: bool = False) -> str:
    """File (once) the owner's "Needs your attention" card for this stop.

    The existing handoff store and the existing `/v1/handoffs` answer path --
    no second system. One card per desk, kind and site while it is waiting:
    a desk that reads the page again, or clicks, must not stack a card per
    call. Returns the card id, or "" if it could not be filed -- the tool must
    still refuse and explain, so a full disk never turns into a crash.
    """
    path = handoff.DEFAULT_PATH
    try:
        origin = browser_takeover.origin_of(url)
        for h in handoff.waiting(path):
            if h.agent == sess.desk and h.kind == kind and \
                    _origin_or_none(h.where) == origin:
                return h.id
        return browser_takeover.raise_browser_handoff(
            path, sess, kind=kind, url=url, passkey=passkey).id
    except (ValueError, OSError):
        return ""


def _origin_or_none(url: str) -> str | None:
    try:
        return browser_takeover.origin_of(url)
    except ValueError:
        return None


def card_note(kind: str, card: str) -> str:
    """What the desk is told once the stop is on the owner's screen. PURE."""
    if card:
        return (f"this page is a {kind} step, so it needs one tap from the "
                f"owner. An approve card ({card}) is now on his app: Allow / "
                f"Allow always for this site / No. Do other work that does "
                f"not need this browser, or end your turn. When he taps "
                f"Allow you will get a message and your clicks here go "
                f"through -- then do the step yourself. If he takes over the "
                f"screen instead, his sign-in stays in this browser for you.")
    return (f"this page is a {kind} step and it is the owner's. Stop here: "
            f"tell whoever you report to which site it is and that it is "
            f"waiting on your screen, so the owner can take it over in the "
            f"app. His sign-in stays in this browser for you afterwards.")


def _allowed(desk: str, kind: str, url: str) -> str | None:
    """"always" / "once" if the owner already said yes to this step here.

    Read from the grants file beside the handoff queue on EVERY check, so the
    owner's Allow -- written by the API process -- unlocks the very next click.
    """
    try:
        origin = browser_takeover.origin_of(url)
    except ValueError:
        return None
    return browser_takeover.granted(
        browser_takeover.grants_path(handoff.DEFAULT_PATH),
        desk=desk, origin=origin, kind=kind)


def _guard(desk: str) -> None:
    """Hold an action on a step the owner has not approved yet -- one card."""
    kind = _needs_human(desk)
    if kind:
        url = ""
        try:
            url = str(ensure(desk).page_state().get("url") or "")
        except (sandbox.SandboxError, browser.BrowserError, OSError):
            pass
        if url and _allowed(desk, kind, url):
            return
        card = _raise_card(ContainerCDP(desk).sess, kind, url) if url else ""
        raise PermissionError(card_note(kind, card))


def _steer_off_passkey(driver, url: str) -> bool:
    """On a passkey page, dodge it; True if it is STILL a passkey page.

    Passkeys are on the owner's own devices, never in this container, so a
    passkey-first sign-in dead-ends. Install an empty virtual authenticator and
    click "Try another way" -- never typing a credential. Best effort.
    """
    try:
        driver.run_commands(browser_takeover.passkey_steer_commands())
        time.sleep(1.0)
        now = str(driver.page_state().get("url") or url)
    except (sandbox.SandboxError, browser.BrowserError, OSError):
        return True
    return browser_takeover.is_passkey_challenge(now)


def look(driver: ContainerCDP) -> dict:
    page = driver._evaluate(_TEXT_JS) or {}
    text = str(page.get("text") or "")
    state = driver.page_state()
    kind = browser_takeover.needs_human(state)
    url = str(page.get("url") or "")
    passkey = False
    if not kind and browser_takeover.is_passkey_challenge(url):
        if _steer_off_passkey(driver, url):
            kind, passkey = "login", True
        else:  # it moved on: report what is there now
            state = driver.page_state()
            kind = browser_takeover.needs_human(state)
            url = str(state.get("url") or url)
    allowed = _allowed(driver.sess.desk, kind, url) \
        if kind and url and not passkey else None
    if allowed:
        kind = None
    card = _raise_card(driver.sess, kind, url, passkey=passkey) \
        if kind and url else ""
    return {"url": url, "title": str(page.get("title") or ""),
            "text": text[:TEXT_MAX], "truncated": len(text) > TEXT_MAX,
            "needs_human": kind, "card": card, "allowed": allowed}


#: What a desk is told instead of opening a sign-out page.
SIGN_OUT_REFUSED = (
    "Refused: this page signs you out, and the sign-in is shared by every desk "
    "-- one sign-out signs every desk out (and can sign the owner out too). "
    "Never sign out to switch account: use the site's account switcher, or ask "
    "the owner.")


#: Words on a link or button that sign the browser out.
_SIGN_OUT_TEXT = re.compile(r"^\s*(log\s*-?\s*out|sign\s*-?\s*out|log\s*off|sign\s*off)\b",
                            re.IGNORECASE)

#: The link/button under a screen click. The click is in screen pixels; the
#: page starts below the browser's own toolbar, so subtract that first.
_TARGET_JS = """(() => {{
  const top = window.screenY + (window.outerHeight - window.innerHeight);
  const left = window.screenX + (window.outerWidth - window.innerWidth) / 2;
  const e = document.elementFromPoint({x} - left, {y} - top);
  if (!e) return {{}};
  const a = e.closest('a,button,[role=button],[role=menuitem],[role=link],input');
  const t = a || e;
  return {{href: (a && a.href) || '',
           text: String(t.innerText || t.value || t.getAttribute('aria-label') || '').slice(0, 80)}};
}})()"""


def signs_out(href: str, text: str) -> bool:
    """A link or button that signs this browser out. PURE."""
    return login_vault.signs_everyone_out(href) or bool(_SIGN_OUT_TEXT.match(text or ""))


def _sign_out_card(desk: str, sess, url: str) -> str | None:
    """None if the owner already approved signing out here; else the card id
    ("" if it could not be filed) of the approval now on his app."""
    if url and _allowed(desk, "other", url):
        return None
    return _raise_card(sess, "other", url) if url else ""


def navigate(desk: str, url: str) -> dict:
    browser.origin_of(url)  # http(s) only -- ValueError before anything runs
    target = str(url).strip()
    if login_vault.enabled() and login_vault.signs_everyone_out(target):
        card = _sign_out_card(desk, ContainerCDP(desk).sess, target)
        if card is not None:
            return {"url": target, "title": "Not opened",
                    "text": SIGN_OUT_REFUSED + " " + card_note("sign-out", card),
                    "truncated": False, "needs_human": "other", "card": card,
                    "allowed": None}
    driver = ensure(desk)
    driver.goto(target)
    _await_ready(driver)
    # A site that keeps its session in localStorage (vapi) is not signed in by
    # the seeded cookies alone. If the vault holds that session for this origin
    # and the page does not, inject it and reload once so the app reads it.
    try:
        if login_vault.inject_on_navigate(desk, driver, target):
            driver.goto(target)
            _await_ready(driver)
    except Exception:  # noqa: BLE001 - a navigate must not fail on the session
        pass
    return look(driver)


def _await_ready(driver, *, timeout: float = WAIT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.3)
        if driver._evaluate("document.readyState") == "complete":
            return


def read_page(desk: str) -> dict:
    return look(ensure(desk))


def screenshot(desk: str) -> bytes:
    ensure(desk)
    return sandbox.frame(desk)


def act(desk: str, action: dict) -> None:
    """click / type / key -- the owner's own take-over path, after the guard."""
    _guard(desk)
    if action.get("action") == "click" and login_vault.enabled():
        _hold_sign_out_click(desk, action)
    sandbox.send_input(desk, action)


def _hold_sign_out_click(desk: str, action: dict) -> None:
    """A click on "Sign out" signs every desk out (they share the session): it
    waits for the owner's approval card instead. Best effort -- a page that
    cannot be read lets the click through rather than wedging the desk."""
    try:
        driver = ContainerCDP(desk)  # never START a browser just to look
        if driver.page_target() is None:
            return
        hit = driver._evaluate(_TARGET_JS.format(
            x=float(action.get("x") or 0), y=float(action.get("y") or 0))) or {}
        url = str(driver.page_state().get("url") or "")
    except Exception:  # noqa: BLE001 - unreadable page: let the click through
        return
    if not isinstance(hit, dict) or not signs_out(str(hit.get("href") or ""),
                                                   str(hit.get("text") or "")):
        return
    card = _sign_out_card(desk, driver.sess, url)
    if card is not None:
        raise PermissionError(SIGN_OUT_REFUSED + " " + card_note("sign-out", card))


# ── passwords: generated and typed by the deck, never seen by the model ────

_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!#%+-.=?@_"
PASSWORD_LENGTH = 20


def _new_password() -> str:
    """Strong, and accepted by picky sign-up forms: every class present."""
    while True:
        value = "".join(secrets.choice(_PASSWORD_ALPHABET)
                        for _ in range(PASSWORD_LENGTH))
        if (any(c.islower() for c in value) and any(c.isupper() for c in value)
                and any(c.isdigit() for c in value)
                and any(not c.isalnum() for c in value)):
            return value


def login_name(origin: str) -> str:
    """The vault name for a site's sign-in. PURE."""
    host = urlsplit(origin).hostname or ""
    return "LOGIN_" + re.sub(r"[^A-Z0-9]", "_", host.upper())


def type_password(desk: str, username: str = "") -> str:
    """Type this site's password into the focused field. Returns a note.

    Owner ruling 2026-09-30: with his Allow a desk may create accounts and
    sign in itself. The value never reaches the model: the deck reuses the
    site's saved password (the vault, granted to this desk) or generates a
    strong one and saves it there FIRST, then types it down a pipe. The site
    is the page the browser is on, never an argument -- a desk cannot aim one
    site's password at another.
    """
    _guard(desk)
    url = str(ensure(desk).page_state().get("url") or "")
    origin = browser_takeover.origin_of(url)
    name = login_name(origin)
    path = vault.DEFAULT_PATH
    value = vault.env_for(path, desk).get(name)
    made = False
    if value is None:
        if any(m.name == name for m in vault.meta(path)):
            raise PermissionError(
                f"a saved sign-in for {origin} exists ({name}) but is not "
                f"granted to you; ask the owner to grant it rather than "
                f"making a new password")
        value = _new_password()
        who = str(username or "").strip()[:120]
        vault.put(path, name=name, value=value,
                  description=(f"Sign-in for {origin}; username: "
                               f"{who or '(not given)'}; created by desk {desk}"),
                  grants=(desk,))
        made = True
    sandbox.type_secret(desk, value)
    return (f"typed the {'new' if made else 'saved'} password for {origin} "
            f"(kept in the deck vault as {name}; you never see it -- do not "
            f"try to read or repeat it)")
