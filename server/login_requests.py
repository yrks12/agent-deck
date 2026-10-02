"""The phone asks the Mac to sign a desk in.

Owner: *"on iPhone I don't have it at all."* His Chrome login and his passkey
live on his Mac. The phone cannot read either, so it files a request here; the
Mac app, already connected, claims it on a long-poll (`next`), does the sign-in
it already knows how to do, hands the cookies to `POST /v1/logins` (never to
this module) and reports a COUNT back.

Lifecycle: queued -> claimed -> done | failed, and either of the first two
becomes `expired` two minutes after it was filed. An unclaimed expiry is the
phone's "Your Mac is offline".

**No cookie ever passes through here.** A row is built from `FIELDS` and
nothing else, so a Mac (or a phone) that sends a cookie with its call has it
dropped before anything is written or returned.
"""

from __future__ import annotations

import fcntl
import json
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from server import atomic

TTL = 120.0
METHODS = ("chrome", "passkey")
OUTCOMES = ("signed_in", "opened")
MAX_ROWS = 100
FIELDS = ("id", "desk", "origin", "host", "method", "handoff", "status",
          "outcome", "node", "desks", "detail", "created_at", "claimed_at",
          "finished_at", "expires_at")
_TEXT = 300


class RequestRefused(Exception):
    def __init__(self, status: int, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.status, self.reason, self.detail = status, reason, detail or reason


def _now() -> float:
    return time.time()


def path_for(handoffs_path: Path) -> Path:
    """Beside the handoffs file, so a test's tmp deck is a whole deck."""
    return Path(handoffs_path).parent / "login-requests.json"


def front_door(origin) -> tuple[str, str]:
    """`https://host[:port]` and the host, or a refusal. Only https: no
    passkey is offered to plain http and no cookie should be either."""
    parts = urlsplit(str(origin or "").strip())
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or not host or " " in host:
        raise RequestRefused(400, "bad_origin",
                             f"{origin!r} is not an https site")
    port = f":{parts.port}" if parts.port else ""
    return f"https://{host}{port}", host


def _text(value, limit: int = _TEXT) -> str | None:
    if value is None:
        return None
    return str(value).strip()[:limit] or None


def _row(raw: dict) -> dict:
    return {k: raw.get(k) for k in FIELDS}


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _load(path: Path) -> list[dict]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = raw.get("requests") if isinstance(raw, dict) else None
    return [_row(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _save(path: Path, rows: list[dict]) -> None:
    atomic.write_text(path, json.dumps(
        {"version": 1, "requests": [_row(r) for r in rows[-MAX_ROWS:]]}),
        mode=0o600)


def _sweep(rows: list[dict], now: float) -> bool:
    changed = False
    for row in rows:
        if row["status"] in ("queued", "claimed") and now >= row["expires_at"]:
            row["status"], row["finished_at"] = "expired", row["expires_at"]
            changed = True
    return changed


def _find(rows: list[dict], rid: str) -> dict:
    wanted = str(rid or "").strip()
    for row in rows:
        if row["id"] == wanted:
            return row
    raise RequestRefused(404, "unknown_request", f"no login request {rid!r}")


def file_request(path: Path, payload: dict) -> dict:
    desk = _text(payload.get("desk"), 120)
    if not desk:
        raise RequestRefused(400, "missing_field", "desk is required")
    method = str(payload.get("method") or "").strip().lower()
    if method not in METHODS:
        raise RequestRefused(400, "bad_method",
                             f"{payload.get('method')!r} is not chrome or passkey")
    origin, host = front_door(payload.get("origin"))
    now = _now()
    with _locked(path):
        rows = _load(path)
        _sweep(rows, now)
        taken = {r["id"] for r in rows}
        rid = secrets.token_hex(6)
        while rid in taken:
            rid = secrets.token_hex(6)
        row = _row({"id": rid, "desk": desk, "origin": origin, "host": host,
                    "method": method, "handoff": _text(payload.get("handoff"), 80),
                    "status": "queued", "created_at": now,
                    "expires_at": now + TTL})
        rows.append(row)
        _save(path, rows)
    return row


def get(path: Path, rid: str) -> dict:
    if not Path(path).exists():  # nothing ever filed: write nothing either
        raise RequestRefused(404, "unknown_request", f"no login request {rid!r}")
    with _locked(path):
        rows = _load(path)
        if _sweep(rows, _now()):
            _save(path, rows)
        return _find(rows, rid)


def claim(path: Path, node) -> dict | None:
    """The oldest live request, now this Mac's. None when nothing waits."""
    name = _text(node, 80) or "a Mac"
    now = _now()
    if not Path(path).exists():  # the Mac polls all day: an idle poll writes nothing
        return None
    with _locked(path):
        rows = _load(path)
        changed = _sweep(rows, now)
        found = next((r for r in rows if r["status"] == "queued"), None)
        if found is not None:
            found.update(status="claimed", node=name, claimed_at=now)
            changed = True
        if changed:
            _save(path, rows)
        return found


def report(path: Path, rid: str, payload: dict) -> dict:
    status = str(payload.get("status") or "").strip().lower()
    if status not in ("done", "failed"):
        raise RequestRefused(400, "bad_status", f"{status!r} is not done or failed")
    outcome = str(payload.get("outcome") or "signed_in").strip().lower()
    if status == "done" and outcome not in OUTCOMES:
        raise RequestRefused(400, "bad_outcome", f"{outcome!r} is not an outcome")
    try:
        desks = max(0, int(payload.get("desks") or 0))
    except (TypeError, ValueError):
        desks = 0
    now = _now()
    if not Path(path).exists():
        raise RequestRefused(404, "unknown_request", f"no login request {rid!r}")
    with _locked(path):
        rows = _load(path)
        _sweep(rows, now)
        row = _find(rows, rid)
        if row["status"] != "claimed":
            _save(path, rows)
            raise RequestRefused(409, "not_claimed",
                                 f"{row['id']} is {row['status']}, not claimed")
        row.update(status=status, finished_at=now, desks=desks,
                   outcome=outcome if status == "done" else None,
                   detail=_text(payload.get("detail")))
        _save(path, rows)
        return row
