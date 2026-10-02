"""The one implementation of `BrowserDriver` that talks to a real Chrome.

**Nothing in the default suite imports this at a level that needs a
dependency, and nothing here runs in it.** The deck is stdlib-only and stays
that way: the target list, the version banner and the screenshot write all go
over plain HTTP with `urllib`, but *commanding* a page needs a WebSocket, and
the standard library has no WebSocket client. There is no way around that --
CDP's command channel is `ws://` and only `ws://`.

So the dependency is quarantined:

* it is imported **lazily**, inside the method that first needs it, so
  `import server.browser_cdp` works on a Mac with nothing installed;
* it is never added to the deck's environment. It belongs in an isolated venv
  on the box that runs the browser (docs/the-browser.md names the path);
* a missing package raises `BrowserError("driver_unavailable")` with the
  install line in it, rather than an `ImportError` from four frames down.

Every method that opens a socket is exercised only under `-m live`
(tests/test_browser_live.py), on a box with Xvfb and Chrome.

**`page_state` is the whole point of this class.** It returns the structural
snapshot `browser_takeover.needs_human` reads -- input types, autocomplete
tokens, visibility, and the frame tree's origins. It deliberately does not
return page text: `needs_human` must not be able to read it even by accident,
and a driver that hands back the full DOM of a signed-in page is a driver that
puts that page into every log the deck writes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from .browser import CDP_BIND, BrowserError, BrowserSession

# What a live driver needs, and the only third-party import in the project.
DRIVER_PACKAGE = "websockets"
DRIVER_INSTALL = "pip install 'websockets>=12,<16'"

HTTP_TIMEOUT = 5.0
WS_TIMEOUT = 20.0

# Collected in the page, returned as data. Reads only structural attributes --
# never `innerText`, never `value` (a value is whatever the human just typed,
# which on a login form is the password).
_PAGE_STATE_JS = r"""
(() => {
  const vis = (el) => {
    const s = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden'
        && Number(s.opacity) !== 0 && r.width > 0 && r.height > 0;
  };
  const inputs = [...document.querySelectorAll('input, [data-sitekey]')]
    .map((el) => ({
      type: (el.getAttribute('type') || '').toLowerCase(),
      autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
      name: el.getAttribute('name') || '',
      sitekey: el.getAttribute('data-sitekey') || '',
      size: (el.getAttribute('data-size') || '').toLowerCase(),
      disabled: !!el.disabled,
      hidden: !vis(el),
    }));
  // Geometry, not just the src: Stripe.js puts hidden 1px frames on every
  // page of a site that has a billing section, and only a frame a person can
  // see is a payment field. MEASURED on dashboard.vapi.ai, 2026-09-30.
  const frames = [...document.querySelectorAll('iframe')]
    .map((el) => {
      const r = el.getBoundingClientRect();
      return { url: el.src || '', hidden: !vis(el),
               width: Math.round(r.width), height: Math.round(r.height) };
    })
    .filter((f) => f.url);
  return { url: location.href, inputs, frames };
})()
"""


#: Events kept while waiting for a reply; a page that floods `Page.*` events
#: must not grow memory without bound.
MAX_EVENTS = 500


def _quiet(exc: Exception) -> bool:
    """A read that timed out with nothing to say -- not a broken socket."""
    return isinstance(exc, TimeoutError) or \
        getattr(exc, "reason", "") == "computer_not_responding"


class CDPSession:
    """Several CDP commands on ONE connection, with the events kept.

    `run_commands` sends a fixed list; this is for when a step needs the last
    one's answer (an objectId) or an event (`Page.fileChooserOpened`). State
    such as file-chooser interception or device emulation lives exactly as
    long as this connection -- MEASURED on Chromium 151: the overrides are
    gone the moment the socket closes.
    """

    def __init__(self, socket) -> None:
        self.socket = socket
        self.events: list[dict] = []
        self._n = 0

    def __enter__(self) -> "CDPSession":
        return self

    def __exit__(self, *exc) -> None:
        self.socket.__exit__(*exc)

    def _keep(self, msg: dict) -> None:
        self.events.append(msg)
        del self.events[:-MAX_EVENTS]

    def call(self, method: str, params: dict | None = None) -> dict:
        self._n += 1
        n = self._n
        self.socket.send(json.dumps({"id": n, "method": method,
                                     "params": params or {}}))
        while True:
            try:
                reply = json.loads(self.socket.recv(timeout=WS_TIMEOUT))
            except ValueError:
                continue
            if "id" not in reply and "method" in reply:
                self._keep(reply)
                continue
            if reply.get("id") != n:
                continue
            if "error" in reply:
                raise BrowserError("cdp_error",
                                   json.dumps(reply["error"])[:400])
            return reply.get("result") or {}

    def wait_event(self, method: str, timeout: float) -> dict | None:
        """The params of the next `method` event, or None after `timeout`."""
        import time

        deadline = time.monotonic() + float(timeout)
        while True:
            for i, msg in enumerate(self.events):
                if msg.get("method") == method:
                    return self.events.pop(i).get("params") or {}
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                raw = self.socket.recv(timeout=left)
            except Exception as exc:  # noqa: BLE001 - only a quiet read is ok
                if _quiet(exc):
                    if time.monotonic() >= deadline:
                        return None
                    continue
                raise
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "method" in msg and "id" not in msg:
                self._keep(msg)


class CDPDriver:
    """Drives one `BrowserSession` over the DevTools Protocol.

    Connects to `127.0.0.1:<cdp_port>` and nowhere else -- the bind address is
    taken from `browser.CDP_BIND`, not from a parameter, because a driver that
    can be pointed at another host is a driver that can be pointed at another
    person's browser.
    """

    def __init__(self, sess: BrowserSession) -> None:
        self.sess = sess
        self._ws_url: str | None = None

    # ── stdlib-only: the HTTP half of CDP ──────────────────────────────────

    @property
    def base(self) -> str:
        return f"http://{CDP_BIND}:{int(self.sess.cdp_port)}"

    def _get(self, path: str) -> object:
        url = f"{self.base}{path}"
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BrowserError("cdp_unreachable",
                               f"{url}: {exc}") from exc

    def targets(self) -> list[dict]:
        """Every open target. `[]` while Chrome is still coming up."""
        try:
            rows = self._get("/json/list")
        except BrowserError:
            return []
        return [row for row in rows if isinstance(row, dict)] \
            if isinstance(rows, list) else []

    def page_target(self) -> dict | None:
        for row in self.targets():
            if row.get("type") == "page" and row.get("webSocketDebuggerUrl"):
                return row
        return None

    def wait_for_page(self, timeout: float = 15.0) -> dict:
        """Block until Chrome publishes a page target.

        Chrome takes a second or two to come up on a cold profile, and the
        difference between "not up yet" and "failed to start" is only visible
        as time passing -- so it is a timeout, with a reason that says which.
        """
        import time

        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            target = self.page_target()
            if target:
                self._ws_url = str(target["webSocketDebuggerUrl"])
                return target
            time.sleep(0.25)
        raise BrowserError(
            "cdp_no_page",
            f"no page target on {self.base} after {timeout}s — Chrome may "
            f"have failed to start on {self.sess.display}",
        )

    # ── the WebSocket half: the quarantined dependency ─────────────────────

    def _connect(self):
        """Open the command channel. Imports the dependency here, not above."""
        try:
            from websockets.sync.client import connect  # noqa: PLC0415
        except ImportError as exc:
            raise BrowserError(
                "driver_unavailable",
                f"driving Chrome needs the {DRIVER_PACKAGE} package, which "
                f"the stdlib has no equivalent of. Install it in the browser "
                f"box's own venv ({DRIVER_INSTALL}) — never in the deck's. "
                f"See docs/the-browser.md.",
            ) from exc

        if not self._ws_url:
            self.wait_for_page()
        return connect(self._ws_url, open_timeout=WS_TIMEOUT,
                       max_size=8 * 1024 * 1024)

    def send(self, method: str, params: dict | None = None) -> dict:
        """One CDP command, one reply.

        A connection per command rather than a long-lived one: this driver is
        called a handful of times per page, the human may be using the same
        browser between calls, and a socket held open across a take-over is a
        socket that goes stale exactly when it matters.
        """
        message = json.dumps({"id": 1, "method": method,
                              "params": params or {}})
        with self._connect() as socket:
            socket.send(message)
            while True:
                raw = socket.recv(timeout=WS_TIMEOUT)
                try:
                    reply = json.loads(raw)
                except ValueError:
                    continue
                if reply.get("id") != 1:
                    continue  # an event, not our answer
                if "error" in reply:
                    raise BrowserError("cdp_error",
                                       json.dumps(reply["error"])[:400])
                return reply.get("result") or {}

    def run_commands(self, commands: list) -> list[dict]:
        """Several CDP commands on ONE connection, in order.

        State such as a virtual authenticator lives only as long as the
        connection that created it, so it cannot be sent one command at a time.
        """
        results: list[dict] = []
        with self._connect() as socket:
            for n, (method, params) in enumerate(commands, start=1):
                socket.send(json.dumps({"id": n, "method": method,
                                        "params": params or {}}))
                while True:
                    try:
                        reply = json.loads(socket.recv(timeout=WS_TIMEOUT))
                    except ValueError:
                        continue
                    if reply.get("id") != n:
                        continue
                    if "error" in reply:
                        raise BrowserError("cdp_error",
                                           json.dumps(reply["error"])[:400])
                    results.append(reply.get("result") or {})
                    break
        return results

    def session(self) -> "CDPSession":
        """One connection for a conversation whose steps depend on each other
        and on the events between them (a file chooser opening, say). Use as
        a context manager; everything set on it ends when it closes."""
        return CDPSession(self._connect())

    def _evaluate(self, expression: str) -> object:
        result = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if result.get("exceptionDetails"):
            raise BrowserError("cdp_page_error",
                               str(result["exceptionDetails"])[:400])
        return (result.get("result") or {}).get("value")

    # ── BrowserDriver ──────────────────────────────────────────────────────

    def goto(self, url: str) -> None:
        self.send("Page.navigate", {"url": str(url)})

    def page_state(self) -> dict:
        """The structural snapshot `browser_takeover.needs_human` reads.

        No page text, by construction. `needs_human` must decide on structure,
        and the surest way to keep it honest is to never give it the words.
        """
        value = self._evaluate(_PAGE_STATE_JS)
        if not isinstance(value, dict):
            return {"url": "", "inputs": [], "frames": []}
        value.setdefault("inputs", [])
        value.setdefault("frames", [])
        return value

    def click(self, selector: str) -> None:
        found = self._evaluate(
            f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
            f" if (!el) return false; el.click(); return true; }})()"
        )
        if found is not True:
            raise BrowserError("no_such_element", selector)

    def type_text(self, selector: str, text: str) -> None:
        """Set a field's value and fire the events a framework listens for.

        There is no guard here against typing a password, and there must not
        be a use for one: `needs_human` stops the agent *before* a page with a
        password field is ever driven. If this method is ever reached on such
        a page, the bug is upstream in the detection, not here.
        """
        found = self._evaluate(
            f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
            f" if (!el) return false; el.focus();"
            f" el.value = {json.dumps(str(text))};"
            f" el.dispatchEvent(new Event('input', {{bubbles: true}}));"
            f" el.dispatchEvent(new Event('change', {{bubbles: true}}));"
            f" return true; }})()"
        )
        if found is not True:
            raise BrowserError("no_such_element", selector)

    def screenshot(self, dest: Path) -> str:
        """A PNG of the current page. Returns the path, or "" if it failed.

        Evidence for the handoff card. The *caption* around it is what carries
        a leak risk and goes through `browser.safe`; the image itself is the
        page as the human is about to see it anyway.
        """
        import base64

        dest = Path(dest)
        try:
            result = self.send("Page.captureScreenshot", {"format": "png"})
            data = result.get("data")
            if not data:
                return ""
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(data))
        except (BrowserError, OSError, ValueError):
            return ""
        return str(dest)

    def cookies(self) -> list[dict]:
        """Every cookie in the profile. Used by the live persistence test.

        Values included -- this is the profile's own state, read on the box,
        and nothing here logs it. Do not put the result in a message without
        `browser.safe`.
        """
        result = self.send("Network.getCookies", {})
        rows = result.get("cookies")
        return [row for row in rows if isinstance(row, dict)] \
            if isinstance(rows, list) else []

    def set_cookie(self, *, name: str, value: str, domain: str,
                   path: str = "/") -> None:
        self.send("Network.setCookie", {"name": name, "value": value,
                                        "domain": domain, "path": path})

    def close(self) -> None:
        """Ask the browser to shut down. `browser.stop` is the hard version.

        Best effort: a browser that has already gone is not an error to a
        function whose job is making sure it is gone.
        """
        try:
            self.send("Browser.close")
        except BrowserError:
            pass
