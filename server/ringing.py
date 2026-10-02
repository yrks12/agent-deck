"""A desk calls HIM: the settings that allow it, and the ring itself.

Owner, 2026-10-01: "settings to let Atlas call us by himself". Until now the
owner could call a desk (`server/calls.py`) but no desk could call him.

`mcp__deck__call_owner(reason, urgency)` places a RING here. The deck pushes
it once (`push_policy.PushPolicy.ring`, so every channel -- the Mac app, the
iPhone and ntfy -- reads it from `GET /v1/owner/alerts`), and the apps show an
incoming-call screen for `RING_SECONDS`. Then exactly one of:

* **answered** -- the app opens the ordinary live call (`server/calls.py`),
  with the desk briefed and the reason as its first spoken line;
* **declined** or **missed** -- the reason is posted to his thread as a line
  from the desk. A ring never dead-ends.

The settings decide whether a ring may happen at all. The public default is
OFF: a deck rings nobody until he turns it on.

Two processes write these files: the deck (answer, decline, the sweep, his
settings) and a desk's `server.deck_mcp` (the ring). Every read-modify-write
holds an `flock` on a sibling lock file, as `server/decisions.py` does.
"""

from __future__ import annotations

import fcntl
import json
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import atomic, office
from .push_policy import quiet_window

WHO = ("chief", "any")
WHEN = ("off", "urgent", "anytime")
DEFAULTS = {"who": "chief", "when": "off", "quiet_hours": "22:00-08:00",
            "tz": "America/New_York", "max_per_day": 3, "call_me_now": False}
#: How long the apps ring before it counts as no answer.
RING_SECONDS = 30.0
#: "Call me now" turns itself off after this, or after one answered call.
CALL_ME_NOW_SECONDS = 3600.0
MAX_PER_DAY = 20
REASON_MAX = 200
#: Settled rings kept on disk (for the daily count and the dedupe).
KEEP = 200


class RingError(Exception):
    def __init__(self, reason: str, detail: str = "", status: int = 400) -> None:
        super().__init__(detail or reason)
        self.reason, self.detail, self.status = reason, detail or reason, status


def _bus() -> Path:
    return Path(office.BUS_DIR)


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


# ── settings ────────────────────────────────────────────────────────────────


def _settings_path() -> Path:
    return _bus() / "call_owner.json"


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - a bad zone is the default zone
        return ZoneInfo(DEFAULTS["tz"])


def settings(at: float | None = None) -> dict:
    """What he chose, over the defaults. `call_me_now` is live: it reads False
    once its hour is up."""
    at = time.time() if at is None else at
    raw = _read(_settings_path())
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in raw and key != "call_me_now":
            out[key] = raw[key]
    until = raw.get("call_me_now_until")
    out["call_me_now"] = isinstance(until, (int, float)) and until > at
    out["ring_seconds"] = int(RING_SECONDS)
    return out


def update(changes: dict, at: float | None = None) -> tuple[dict, bool]:
    """Apply his edits. Returns (settings, call_me_now_just_turned_on)."""
    at = time.time() if at is None else at
    if not isinstance(changes, dict) or not changes:
        raise RingError("empty", "send at least one setting")
    unknown = sorted(set(changes) - set(DEFAULTS))
    if unknown:
        raise RingError("unknown_setting", f"no setting {unknown[0]!r}")
    if "who" in changes and changes["who"] not in WHO:
        raise RingError("bad_who", f"who is one of {list(WHO)}")
    if "when" in changes and changes["when"] not in WHEN:
        raise RingError("bad_when", f"when is one of {list(WHEN)}")
    if "quiet_hours" in changes:
        spec = changes["quiet_hours"]
        if not isinstance(spec, str) or (spec.strip() and quiet_window(spec) is None):
            raise RingError("bad_quiet_hours", 'quiet_hours is "HH:MM-HH:MM" or ""')
    if "tz" in changes:
        try:
            ZoneInfo(str(changes["tz"]))
        except Exception:  # noqa: BLE001
            raise RingError("bad_tz", "tz is an IANA zone, e.g. America/New_York")
    if "max_per_day" in changes:
        cap = changes["max_per_day"]
        if not isinstance(cap, int) or isinstance(cap, bool) or not 0 <= cap <= MAX_PER_DAY:
            raise RingError("bad_max_per_day", f"max_per_day is 0..{MAX_PER_DAY}")
    if "call_me_now" in changes and not isinstance(changes["call_me_now"], bool):
        raise RingError("bad_call_me_now", "call_me_now is true or false")
    path = _settings_path()
    with _locked(path):
        raw = _read(path)
        was_on = settings(at)["call_me_now"]
        for key, value in changes.items():
            if key == "call_me_now":
                raw["call_me_now_until"] = at + CALL_ME_NOW_SECONDS if value else 0
            else:
                raw[key] = value
        atomic.write_text(path, json.dumps(raw))
    now = settings(at)
    return now, now["call_me_now"] and not was_on


def _quiet(conf: dict, at: float) -> bool:
    zone = _zone(str(conf["tz"]))
    inside = quiet_window(str(conf["quiet_hours"]),
                          clock=lambda t: datetime.fromtimestamp(t, zone).timetuple())
    return bool(inside and inside(at))


def _day(conf: dict, at: float) -> str:
    return datetime.fromtimestamp(at, _zone(str(conf["tz"]))).date().isoformat()


def _key(reason: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", reason.lower()).split())


def decide(conf: dict, *, desk: str, chief: str | None, urgent: bool,
           at: float, rang_today: int, reason_key: str,
           keys_today: set[str]) -> tuple[str, str] | None:
    """None when the ring may go out, else (reason, what to do instead). PURE."""
    if conf["who"] == "chief" and desk != chief:
        return ("not_allowed", "only the chief of staff may call him; say it "
                "to your boss instead")
    now = conf["call_me_now"]
    if conf["when"] == "off" and not now:
        return ("calls_off", "he has calls from desks turned off")
    if conf["when"] == "urgent" and not urgent and not now:
        return ("urgent_only", "he takes only urgent calls")
    if not urgent and not now and _quiet(conf, at):
        return ("quiet_hours", "it is his quiet hours; only urgent calls ring")
    if f"{desk}:{reason_key}" in keys_today:
        return ("already_rang", "you already called him about this today")
    if rang_today >= int(conf["max_per_day"]):
        return ("daily_cap", f"he takes at most {conf['max_per_day']} calls a day")
    return None


# ── the ring ────────────────────────────────────────────────────────────────


def _rings_path() -> Path:
    return _bus() / "rings.json"


def _load_rings() -> dict[str, dict]:
    rings = _read(_rings_path()).get("rings")
    return {k: v for k, v in rings.items() if isinstance(v, dict)} \
        if isinstance(rings, dict) else {}


def _save_rings(rings: dict[str, dict]) -> None:
    done = sorted((r for r in rings.values() if r.get("state") != "ringing"),
                  key=lambda r: r.get("created_at") or 0.0)
    for old in done[:max(0, len(done) - KEEP)]:
        rings.pop(old["id"], None)
    atomic.write_text(_rings_path(), json.dumps({"version": 1, "rings": rings}))


def public(ring: dict) -> dict:
    """The wire shape: what an incoming-call screen draws."""
    return {"id": ring["id"], "agent": ring["desk"],
            "thread_id": f"direct:{ring['desk']}", "reason": ring["reason"],
            "urgent": bool(ring.get("urgent")), "state": ring["state"],
            "created_at": ring["created_at"], "expires_at": ring["expires_at"]}


def fallback(ring: dict, why: str) -> None:
    """Not answered: the reason goes to his thread, and the desk is told."""
    office.send(office.OWNER_INBOX, f"I tried to call you: {ring['reason']}",
                sender=ring["desk"], extra={"said": True, "ring_id": ring["id"]})
    office.send(ring["desk"], f"[Agent Deck] He did not pick up ({why}). Your "
                "reason is in his thread now; carry on there.", sender="deck")


def place(desk: str, reason, urgency="normal", *, chief: str | None,
          at: float | None = None) -> dict:
    """A desk's ring. A refusal that is not a repeat posts the reason to his
    thread instead, so the desk's point still reaches him."""
    at = time.time() if at is None else at
    text = " ".join(str(reason or "").split())
    if not text:
        raise RingError("empty_reason", "say in one line why you are calling")
    if len(text) > REASON_MAX:
        raise RingError("too_long", f"the reason is one line of at most "
                        f"{REASON_MAX} characters")
    if urgency not in ("normal", "urgent"):
        raise RingError("bad_urgency", 'urgency is "normal" or "urgent"')
    conf = settings(at)
    day = _day(conf, at)
    path = _rings_path()
    with _locked(path):
        rings = _load_rings()
        today = [r for r in rings.values() if r.get("day") == day]
        refused = decide(conf, desk=desk, chief=chief, urgent=urgency == "urgent",
                         at=at, rang_today=len(today), reason_key=_key(text),
                         keys_today={f"{r['desk']}:{r.get('key')}" for r in today})
        if refused is None:
            ring = {"id": "ring_" + uuid.uuid4().hex[:12], "desk": desk,
                    "reason": text, "urgent": urgency == "urgent",
                    "state": "ringing", "created_at": at,
                    "expires_at": at + RING_SECONDS, "day": day, "key": _key(text)}
            rings[ring["id"]] = ring
            _save_rings(rings)
    if refused is not None:
        why, detail = refused
        posted = why not in ("already_rang", "not_allowed")
        if posted:
            office.send(office.OWNER_INBOX, text, sender=desk, extra={"said": True})
        return {"ok": False, "reason": why, "posted_to_thread": posted,
                "detail": detail + ("; your reason was posted to his thread "
                                    "instead" if posted else "")}
    return {"ok": True, "ring_id": ring["id"], "state": "ringing",
            "rings_for_seconds": int(RING_SECONDS),
            "detail": "ringing his Mac and iPhone; if he picks up you get the "
                      "call, otherwise your reason is posted to his thread"}


def live(at: float | None = None) -> list[dict]:
    """Rings still ringing, oldest first, in the wire shape."""
    at = time.time() if at is None else at
    return [public(r) for r in sorted(_load_rings().values(),
                                      key=lambda r: r["created_at"])
            if r.get("state") == "ringing" and r["expires_at"] > at]


def _settle(ring_id: str, state: str, at: float) -> dict:
    path = _rings_path()
    if not path.exists():   # no ring ever placed: nothing to lock, or create
        raise RingError("unknown_ring", f"no ring {ring_id!r}", 404)
    with _locked(path):
        rings = _load_rings()
        ring = rings.get(ring_id)
        if ring is None:
            raise RingError("unknown_ring", f"no ring {ring_id!r}", 404)
        if ring["state"] != "ringing" or ring["expires_at"] <= at:
            raise RingError("not_ringing", "this call is no longer ringing", 409)
        ring.update(state=state, settled_at=at)
        _save_rings(rings)
    return ring


def answer(ring_id: str, at: float | None = None) -> dict:
    at = time.time() if at is None else at
    ring = _settle(ring_id, "answered", at)
    if settings(at)["call_me_now"]:
        update({"call_me_now": False}, at=at)
    return ring


def decline(ring_id: str, at: float | None = None) -> dict:
    ring = _settle(ring_id, "declined", time.time() if at is None else at)
    fallback(ring, "declined")
    return ring


def sweep(at: float | None = None) -> list[dict]:
    """Rings nobody answered in time become missed, with the fallback."""
    at = time.time() if at is None else at
    path = _rings_path()
    if not path.exists():
        return []
    with _locked(path):
        rings = _load_rings()
        missed = [r for r in rings.values()
                  if r.get("state") == "ringing" and r["expires_at"] <= at]
        for ring in missed:
            ring.update(state="missed", settled_at=at)
        if missed:
            _save_rings(rings)
    for ring in missed:
        fallback(ring, "no answer")
    return missed
