"""Event wake-ups (Feature C): sources the deck polls itself, with no model turn.

Each runs in the deck process on deck.toml `[events] poll_seconds` and is off
until `[events]` turns it on:

    deploy_poll   `gh run list --status failure` for `deploy_repos`
    gmail_poll    users.history.list for replies on threads the mailbox sent
                  (DECK_GMAIL_CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN,
                  a gmail.readonly grant, in the root-only env file)
    stripe_poll   Stripe's /v1/events with DECK_STRIPE_API_KEY, for a deck
                  with no public HTTPS address for the webhook

The FIRST poll of a source records where it is and reports nothing, so turning
a source on never replays its history into a desk. That mark is persisted
(`pollers.json` beside the event log), so a restart does not replay it either.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import atomic, events

GMAIL_ENV = ("DECK_GMAIL_CLIENT_ID", "DECK_GMAIL_CLIENT_SECRET",
             "DECK_GMAIL_REFRESH_TOKEN")
SENT_THREADS_MAX = 5000


class State:
    """`pollers.json`: each source's cursor. Written whole, atomically."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "pollers.json"
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            raw = {}
        self.data: dict = raw if isinstance(raw, dict) else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(self.path, json.dumps(self.data))


def _gh(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=60,
                          check=True).stdout


def poll_deploys(hub: events.Hub, repos: list[str] | tuple[str, ...], *,
                 state: State, run: Callable[[list[str]], str] = _gh) -> list[dict]:
    """New failed GitHub Actions runs, one event per run id."""
    seeded = state.data.setdefault("deploy_seeded", [])
    out: list[dict] = []
    for repo in repos:
        try:
            rows = json.loads(run([
                "gh", "run", "list", "--repo", repo, "--status", "failure",
                "-L", "20", "--json",
                "databaseId,workflowName,headBranch,displayTitle,url"]))
        except Exception as exc:  # noqa: BLE001 - one repo, not the poll
            print(f"[agent-deck] deploy poll {repo}: {exc!r}", flush=True)
            continue
        if repo not in seeded:
            for row in rows:
                hub.store.claim(f"deploy:{row.get('databaseId')}")
            seeded.append(repo)
            continue
        for row in reversed(rows):
            ev = events.Event(
                source="deploy", account=repo, kind="failed",
                summary=(f"{row.get('workflowName') or 'A workflow'} failed on "
                         f"{row.get('headBranch') or '?'}: "
                         f"{row.get('displayTitle') or ''} {row.get('url') or ''}"
                         ).strip()[:events.SUMMARY_MAX],
                ref=str(row.get("databaseId")), ts=time.time())
            result = hub.ingest(ev)
            if result["state"] != "duplicate":
                out.append(result)
    state.save()
    return out


class Gmail:
    """The three Gmail API calls the poller makes, over a refresh token."""

    BASE = "https://gmail.googleapis.com/gmail/v1/users/me/"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        self._creds = (client_id, client_secret, refresh_token)
        self._token, self._expires = "", 0.0

    def _access(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        cid, secret, refresh = self._creds
        body = urllib.parse.urlencode({
            "client_id": cid, "client_secret": secret,
            "refresh_token": refresh, "grant_type": "refresh_token"}).encode()
        with urllib.request.urlopen("https://oauth2.googleapis.com/token", body,
                                    timeout=20) as resp:  # noqa: S310 - fixed host
            doc = json.load(resp)
        self._token = doc["access_token"]
        self._expires = time.time() + float(doc.get("expires_in") or 3000)
        return self._token

    def get(self, path: str, params: dict | None = None) -> dict:
        url = self.BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._access()}"})
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            return json.load(resp)


def gmail_from_env() -> Gmail | None:
    values = [(os.environ.get(name) or "").strip() for name in GMAIL_ENV]
    return Gmail(*values) if all(values) else None


def poll_gmail(hub: events.Hub, gmail, *, state: State) -> list[dict]:
    """Replies landing in INBOX on a thread the mailbox SENT on."""
    data = state.data
    if not data.get("gmail_history_id"):
        profile = gmail.get("profile")
        data["gmail_history_id"] = str(profile.get("historyId") or "")
        data["gmail_address"] = str(profile.get("emailAddress") or "")
        state.save()
        return []
    try:
        doc = gmail.get("history", {"startHistoryId": data["gmail_history_id"],
                                    "historyTypes": "messageAdded"})
    except urllib.error.HTTPError as err:
        if err.code == 404:   # the cursor aged out: start again from now
            data["gmail_history_id"] = ""
            state.save()
        raise
    sent = list(data.get("gmail_sent_threads") or [])
    replies = []
    for item in doc.get("history") or []:
        for added in item.get("messagesAdded") or []:
            msg = added.get("message") or {}
            labels, thread = msg.get("labelIds") or [], msg.get("threadId")
            if "SENT" in labels and thread not in sent:
                sent.append(thread)
            elif "INBOX" in labels and thread in sent:
                replies.append(msg)
    out = []
    for msg in replies:
        meta = gmail.get(f"messages/{msg['id']}", {
            "format": "metadata", "metadataHeaders": ["From", "Subject"]})
        heads = {h.get("name"): h.get("value") for h in
                 (meta.get("payload") or {}).get("headers") or []}
        ev = events.Event(
            source="gmail", account=data.get("gmail_address", ""), kind="reply",
            summary=(f"Reply from {heads.get('From') or 'someone'}: "
                     f"{heads.get('Subject') or '(no subject)'}")[:events.SUMMARY_MAX],
            ref=str(msg["id"]), ts=time.time())
        result = hub.ingest(ev)
        if result["state"] != "duplicate":
            out.append(result)
    data["gmail_sent_threads"] = sent[-SENT_THREADS_MAX:]
    data["gmail_history_id"] = str(doc.get("historyId") or data["gmail_history_id"])
    state.save()
    return out


def poll_stripe(hub: events.Hub, api_key: str, *, state: State,
                fetch=None) -> list[dict]:
    from . import stripe_hook
    since = state.data.get("stripe_since")
    if not since:
        state.data["stripe_since"] = int(time.time())
        state.save()
        return []
    kwargs = {"fetch": fetch} if fetch else {}
    found, newest = stripe_hook.poll_events(api_key, since=int(since),
                                            now=time.time(), **kwargs)
    out = [r for r in (hub.ingest(ev) for ev in found) if r["state"] != "duplicate"]
    state.data["stripe_since"] = newest
    state.save()
    return out
