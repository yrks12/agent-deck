"""Real-time notifications for free: each owner alert, posted to HIS ntfy.

WHY THIS EXISTS. The iPhone app is signed with a free Apple team, and free
provisioning cannot use APNs -- so the app itself cannot be woken by a push.
The ntfy iOS app can. A self-hosted ntfy server configured with
``upstream-base-url: "https://ntfy.sh"`` forwards a *poll request* upstream
for every message -- the message id and a hashed topic, no title, no text --
and ntfy.sh's APNs wakes the ntfy app, which then fetches the message itself
from HIS server over his tunnel. So the text of what a desk said never leaves
the box except to his own phone.

WHAT IS SENT. Exactly what `GET /v1/owner/alerts` hands the app, read from the
same `Surface.owner_alerts` -- one classification, so ntfy can never buzz for
a thing the app stays quiet about. Each post carries a ``click`` link
(``agentdeck://open?thread=...``) that opens that desk's thread in the Agent
Deck app.

OFF UNLESS CONFIGURED: ``DECK_NTFY_URL`` is the full topic URL
(``http://your-deck:8090/deck-<random>``), ``DECK_NTFY_TOKEN`` an optional
access token. Unset, `Pusher.tick` does nothing and writes nothing.

ONCE EACH. The cursor lives on disk; a first run starts at the head and sends
nothing (switching it on must not replay a month); a failed post leaves the
cursor where it was, so the same alert is retried next tick and nothing after
it jumps the queue.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import atomic

ENV_URL = "DECK_NTFY_URL"
ENV_TOKEN = "DECK_NTFY_TOKEN"

#: The app's URL scheme (ios/project.yml CFBundleURLTypes).
CLICK_SCHEME = "agentdeck"

#: Posts per tick. A backlog after an outage drains over a few seconds rather
#: than as one burst of forty buzzes.
PER_TICK = 5

TIMEOUT = 8.0

#: ntfy priorities: 4 is "high" (breaks through a focus summary), 3 default.
_PRIORITY = {"needs_you": 4, "for_you": 3}


def target(env: dict | None = None) -> tuple[str, str, str] | None:
    """``(server root, topic, token)`` or None when not usable. PURE."""
    source = os.environ if env is None else env
    raw = str(source.get(ENV_URL) or "").strip()
    if not raw:
        return None
    parts = urllib.parse.urlsplit(raw)
    topic = parts.path.strip("/")
    if parts.scheme not in ("http", "https") or not parts.netloc or not topic \
            or "/" in topic:
        return None
    root = f"{parts.scheme}://{parts.netloc}/"
    return root, topic, str(source.get(ENV_TOKEN) or "").strip()


def click_link(alert: dict) -> str:
    """The deep link the ntfy notification opens: the app, on that thread."""
    query = urllib.parse.urlencode({
        "thread": alert.get("thread_id") or "",
        "card": alert.get("card_id") or "",
        "alert": alert.get("id") or "",
    })
    return f"{CLICK_SCHEME}://open?{query}"


def call_link(alert: dict) -> str:
    """A ring's link: the app, straight into the incoming call."""
    query = urllib.parse.urlencode({"ring": alert.get("ring_id") or "",
                                    "thread": alert.get("thread_id") or ""})
    return f"{CLICK_SCHEME}://call?{query}"


def payload(alert: dict, topic: str) -> dict:
    """One alert as ntfy's JSON publish body. PURE."""
    if alert.get("source") == "ring":
        # A desk calling him: the loudest ntfy has, and the tap answers.
        return {"topic": topic, "title": str(alert.get("title") or "A call"),
                "message": str(alert.get("body") or ""), "priority": 5,
                "tags": ["telephone_receiver"], "click": call_link(alert)}
    urgent = alert.get("urgent") is True
    return {
        "topic": topic,
        "title": str(alert.get("title") or alert.get("agent") or "Shaliach"),
        "message": str(alert.get("body") or ""),
        "priority": 5 if urgent else _PRIORITY.get(alert.get("kind"), 3),
        "tags": ["bell"] if alert.get("kind") == "needs_you" else ["speech_balloon"],
        "click": click_link(alert),
    }


def _urlopen(request: urllib.request.Request) -> None:
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        response.read()


class Pusher:
    """Posts new owner alerts to ntfy, once each, cursor on disk."""

    def __init__(self, state_path: Path | str,
                 fetch: Callable[[str | None], dict],
                 opener: Callable[[urllib.request.Request], None] = _urlopen
                 ) -> None:
        self.state_path = Path(state_path)
        self._fetch = fetch
        self._open = opener

    def _cursor(self) -> str | None:
        try:
            return json.loads(self.state_path.read_text()).get("since") or None
        except (OSError, ValueError, AttributeError):
            return None

    def _save(self, since: str) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(self.state_path, json.dumps({"since": since}))

    def tick(self, env: dict | None = None) -> list[str]:
        """Post what is new. Returns the alert ids posted. Never raises."""
        where = target(env)
        if where is None:
            return []
        root, topic, token = where
        since = self._cursor()
        if since is None:
            try:
                self._save(self._fetch(None)["next_since"])
            except Exception as exc:
                print(f"[agent-deck] ntfy: could not start: {exc!r}")
            return []

        posted: list[str] = []
        try:
            alerts = self._fetch(since).get("alerts") or []
        except Exception as exc:
            print(f"[agent-deck] ntfy: could not read alerts: {exc!r}")
            return []
        for alert in alerts[:PER_TICK]:
            headers = {"content-type": "application/json"}
            if token:
                headers["authorization"] = f"Bearer {token}"
            request = urllib.request.Request(
                root, method="POST", headers=headers,
                data=json.dumps(payload(alert, topic)).encode("utf-8"))
            try:
                self._open(request)
            except Exception as exc:
                # Loud, and the cursor stays: retried next tick, in order.
                print(f"[agent-deck] ntfy: {alert.get('id')} not posted: {exc!r}")
                break
            posted.append(str(alert.get("id")))
            self._save(str(alert["cursor"]))
        return posted
