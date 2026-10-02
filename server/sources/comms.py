"""Cross-session messages, reconstructed from transcripts we already tail.

Every message is on disk twice: as a SendMessage tool_use in the sender's
transcript, and as a <cross-session-message> block in the receiver's. The
receiver's copy carries the sender's socket -- and therefore its pid -- which is
the only hard identifier either side gives us. Names are not: two sessions can
share one, and a session outlives the name it was started with.
"""

from __future__ import annotations

import hashlib
import re
import time

from .. import office

KEY_PREFIX = 200

_OPEN_TAG = re.compile(r"<cross-session-message\b([^>]*)>", re.DOTALL)
_ATTR = re.compile(r'([a-z-]+)="([^"]*)"')
_CLOSE_TAG = "</cross-session-message>"
_SOCK_PID = re.compile(r"/(\d+)\.sock$")
_REF = re.compile(r"^(.*?)\s*\[([^\]]+)\]$")


def parse_inbound(text: str) -> dict | None:
    """Sender and body of a received cross-session message, or None."""
    if not text or "<cross-session-message" not in text:
        return None
    match = _OPEN_TAG.search(text)
    if match is None:
        return None
    attrs = dict(_ATTR.findall(match.group(1)))
    body = text[match.end():]
    close = body.find(_CLOSE_TAG)
    if close != -1:
        body = body[:close]
    return {
        "from_sock": attrs.get("from", ""),
        "from_name": attrs.get("from-name", ""),
        "from_mode": attrs.get("from-mode", ""),
        "body": body.strip(),
    }


def pid_from_sock(addr: str) -> int | None:
    """67127 out of 'uds:/tmp/cc-socks/67127.sock'."""
    if not addr:
        return None
    match = _SOCK_PID.search(addr)
    return int(match.group(1)) if match else None


def parse_target(label: str) -> tuple[str, str]:
    """'acme-web [156688]' -> ('acme-web', '156688')."""
    label = (label or "").strip()
    match = _REF.match(label)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return label, ""


def normalize(text: str) -> str:
    return " ".join((text or "").split())


def message_key(text: str) -> str:
    """Stable id for pairing the two copies of one message.

    Only the head is hashed: the receiver's copy can be reflowed or clipped, so
    comparing whole bodies would leave real pairs unmatched.
    """
    return hashlib.sha1(normalize(text)[:KEY_PREFIX].encode("utf-8")).hexdigest()[:12]


MAX_EDGES = 2000
PAIR_WINDOW_SECONDS = 120.0


def build_directory(cards: list[dict]) -> dict:
    """Lookup tables for turning a session id, pid or name into a session."""
    by_sid: dict[str, dict] = {}
    by_pid: dict[int, dict] = {}
    by_name: dict[str, dict] = {}
    for card in cards or []:
        session_id = card.get("session_id")
        if session_id:
            by_sid[str(session_id)] = card
        pid = card.get("pid")
        if isinstance(pid, int):
            by_pid[pid] = card
        name = card.get("name")
        if name:
            by_name[str(name)] = card
    return {"by_sid": by_sid, "by_pid": by_pid, "by_name": by_name}


def _endpoint(directory: dict, *, session_id=None, pid=None, name="") -> dict:
    """One end of an edge, resolved as far as the directory allows.

    An endpoint we cannot resolve keeps whatever label it came with. Dropping it
    would quietly shrink the graph, which is the one thing a view of "who said
    what to whom" must never do.
    """
    card = None
    if session_id:
        card = directory["by_sid"].get(str(session_id))
    if card is None and isinstance(pid, int):
        card = directory["by_pid"].get(pid)
    if card is None and name:
        card = directory["by_name"].get(name)
    if card is None:
        return {
            "session_id": None,
            "name": name or (str(pid) if pid else "?"),
            "pid": pid,
        }
    return {
        "session_id": card.get("session_id"),
        "name": card.get("name", ""),
        "pid": card.get("pid"),
    }


class _Pending:
    """One message, known from one side or both."""

    __slots__ = ("key", "ts", "text", "out_owner", "to_label",
                 "in_owner", "from_pid", "from_name")

    def __init__(self, key: str, ts: float, text: str) -> None:
        self.key = key
        self.ts = ts
        self.text = text
        self.out_owner = None
        self.to_label = ""
        self.in_owner = None
        self.from_pid = None
        self.from_name = ""

    @property
    def seen(self) -> str:
        if self.out_owner and self.in_owner:
            return "both"
        return "sender" if self.out_owner else "receiver"


class CommsIndex:
    """Bounded, newest-wins store of paired cross-session messages.

    Two copies of one message are matched on the head of their text within a
    two-minute window. The cost of that heuristic is that two genuinely distinct
    messages with the same opening 200 characters, sent inside the same window,
    collapse into one edge -- rare enough to accept, and far cheaper than
    showing every message twice.
    """

    def __init__(self, max_edges: int = MAX_EDGES) -> None:
        self.max_edges = max_edges
        self._items: list[_Pending] = []

    def _match(self, key: str, ts: float) -> "_Pending | None":
        for item in reversed(self._items):
            if item.key == key and abs(item.ts - ts) <= PAIR_WINDOW_SECONDS:
                return item
        return None

    def add(self, raw: dict) -> None:
        text = str(raw.get("text") or "")
        if not text.strip():
            return
        ts = float(raw.get("ts") or 0.0)
        key = message_key(text)
        item = self._match(key, ts)
        if item is None:
            item = _Pending(key, ts, text)
            self._items.append(item)
            if len(self._items) > self.max_edges:
                del self._items[: len(self._items) - self.max_edges]

        if raw.get("dir") == "out":
            item.out_owner = raw.get("owner")
            item.to_label = str(raw.get("to_label") or "")
        else:
            item.in_owner = raw.get("owner")
            item.from_pid = pid_from_sock(str(raw.get("from_sock") or ""))
            item.from_name = str(raw.get("from_name") or "")
            # The receiver's stamp is the delivery moment; an edge should sort
            # by when it was said, so keep the earlier of the two.
            item.ts = min(item.ts, ts) if item.out_owner else ts

    def edges(self, directory: dict) -> list[dict]:
        out: list[dict] = []
        for item in self._items:
            target_name, _ref = parse_target(item.to_label)
            text, truncated = office.fit(item.text)
            out.append({
                "id": f"{item.key}-{int(item.ts)}",
                "ts": item.ts,
                "from": _endpoint(directory, session_id=item.out_owner,
                                  pid=item.from_pid, name=item.from_name),
                # By pid too, exactly as the sender already is. `SendMessage`
                # takes either a name or Claude Code's own
                # `uds:/tmp/cc-socks/<pid>.sock`, and the deck now briefs every
                # new hire with that second form -- a name stops being an
                # address the moment a desk can rename itself. Resolved only
                # here, on the way out: the address is the right thing to send
                # to and the wrong thing to file under, and left unresolved it
                # showed the owner a path where there is a desk, in a thread
                # whose id carried a `/` and so could never be opened.
                "to": _endpoint(directory, session_id=item.in_owner,
                                pid=pid_from_sock(item.to_label),
                                name=target_name),
                # The SECOND copy of the same borrowed number. This is one
                # agent's message to another as observed in a transcript;
                # nothing here is ever injected anywhere, so the socket's
                # 2000-byte ceiling had no business clipping it either. Same
                # ceiling as the office queue, and the same rule: if it is
                # ever cut, the text says so and the edge carries the count.
                "text": text,
                "truncated": truncated,
                "seen": item.seen,
                "status": "delivered" if item.in_owner else "sent",
            })
        out.sort(key=lambda edge: edge["ts"], reverse=True)
        return out


UNANSWERED_AFTER = 300.0


def groups(edges: list[dict], manager_session_id: str | None,
           now: float | None = None) -> dict:
    """Split the graph into the manager's team, their side-chatter, and the rest.

    Nothing is stored. The chart is whatever the traffic says right now, which
    is also what makes crowning a different session redraw it instantly.
    """
    now = time.time() if now is None else now
    if not manager_session_id:
        return {"reports": [], "peer_links": [], "elsewhere": _links(edges)}

    reports: dict[str, dict] = {}
    last_in: dict[str, float] = {}    # report -> last time it messaged the manager
    last_out: dict[str, float] = {}   # report -> last time the manager replied

    for edge in edges:
        frm, to = edge["from"], edge["to"]
        other = None
        if frm["session_id"] == manager_session_id and to["session_id"]:
            other = to
            last_out[to["session_id"]] = max(
                last_out.get(to["session_id"], 0.0), edge["ts"])
        elif to["session_id"] == manager_session_id and frm["session_id"]:
            other = frm
            last_in[frm["session_id"]] = max(
                last_in.get(frm["session_id"], 0.0), edge["ts"])
        if other is None:
            continue
        entry = reports.setdefault(other["session_id"], {
            "session_id": other["session_id"],
            "name": other["name"],
            "msg_count": 0,
            "last_ts": 0.0,
            "unanswered": False,
        })
        entry["msg_count"] += 1
        entry["last_ts"] = max(entry["last_ts"], edge["ts"])

    for session_id, entry in reports.items():
        inbound = last_in.get(session_id, 0.0)
        entry["unanswered"] = bool(
            inbound
            and last_out.get(session_id, 0.0) < inbound
            and now - inbound > UNANSWERED_AFTER
        )

    peers, elsewhere = [], []
    for link in _links(edges):
        if manager_session_id in (link["a"], link["b"]):
            continue  # already drawn as the report itself
        target = peers if (link["a"] in reports and link["b"] in reports) else elsewhere
        target.append(link)

    return {
        "reports": sorted(reports.values(), key=lambda r: r["last_ts"], reverse=True),
        "peer_links": peers,
        "elsewhere": elsewhere,
    }


def _links(edges: list[dict]) -> list[dict]:
    """Collapse edges into one undirected link per pair of known sessions."""
    out: dict[tuple, dict] = {}
    for edge in edges:
        a, b = edge["from"]["session_id"], edge["to"]["session_id"]
        if not a or not b:
            continue
        key = tuple(sorted((a, b)))
        link = out.setdefault(key, {
            "a": key[0], "b": key[1],
            "a_name": "", "b_name": "",
            "count": 0, "last_ts": 0.0,
        })
        link["count"] += 1
        link["last_ts"] = max(link["last_ts"], edge["ts"])
        for side in ("from", "to"):
            if edge[side]["session_id"] == key[0]:
                link["a_name"] = edge[side]["name"]
            elif edge[side]["session_id"] == key[1]:
                link["b_name"] = edge[side]["name"]
    return sorted(out.values(), key=lambda link: link["last_ts"], reverse=True)
