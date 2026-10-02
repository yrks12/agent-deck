"""K2 pairing codes and the two K3 stores behind them.

A pairing code (`ADK1.<base64url JSON>`) carries the server's address, how to
trust its certificate, and a **one-time, 15-minute, 128-bit secret**. The Mac
redeems the secret at `POST /v1/pair` for a long-lived per-device token. Three
secrets, three lifetimes -- none of them the master token.

What is on disk, and why it is safe to leak:

* `<state_dir>/pairing.json` -- `{"codes": [{"sha256", "exp", "used"}]}`. Only
  the hash of each secret. A used or expired entry stays for a day so a late
  redemption hears "used"/"expired" rather than "unknown".
* `<state_dir>/devices.json` -- `{"devices": [{"id", "name", "sha256",
  "created", "last_seen"}]}`. Only the hash of each device token.

Both are `0600` in a `0700` dir, written through `server/atomic.py`, and every
load-modify-write holds an `fcntl` lock on a sibling `.lock` file. The lock is
not decoration: `deckctl pair` / `deckctl revoke` run in their own process
while the daemon redeems codes and bumps `last_seen`. Without it, a daemon
writing back its stale copy would un-burn a used code or un-revoke a stolen
laptop.

Hashes are compared with `hmac.compare_digest` against every entry, without
stopping at the first match. No function here logs, and no error carries the
secret or token it was handed.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlsplit

from server import atomic

__all__ = ["PairingError", "PairingCode", "parse", "encode", "PairingStore",
           "DeviceStore", "Device", "mint_code", "sha256_hex", "CODE_TTL"]

CODE_TTL = 900          # seconds a pairing code lives
MAX_LIVE_CODES = 5      # minting a 6th evicts the oldest
KEEP_DEAD_FOR = 86_400  # used/expired entries answer "used"/"expired" this long
LAST_SEEN_EVERY = 60    # devices.json is rewritten for last_seen at most this often

_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_PREFIX = re.compile(r"^ADK(\d+)\.(.*)$", re.IGNORECASE | re.DOTALL)
_NEWER = "This code is from a newer Shaliach — update the app."


class PairingError(ValueError):
    """`reason` is the wire slug (`code_*` for parsing, `pair_*` for redeem)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def sha256_hex(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    if not _B64URL.match(text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ── K2: the code ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PairingCode:
    url: str
    code: str
    exp: int
    tls: str
    name: str
    pin: str = ""
    v: int = 1


def encode(pc: PairingCode) -> str:
    body = {"v": pc.v, "url": pc.url, "code": pc.code, "exp": pc.exp,
            "tls": pc.tls, "name": pc.name}
    if pc.pin:
        body["pin"] = pc.pin
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "ADK1." + _b64encode(raw.encode("utf-8"))


def _bad(what: str) -> PairingError:
    return PairingError("code_malformed", f"This is not a valid pairing code ({what}).")


def _valid_pin(pin: object) -> bool:
    if not isinstance(pin, str) or not pin.startswith("sha256/"):
        return False
    try:
        return len(base64.b64decode(pin[7:], validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


def parse(text: str) -> PairingCode:
    """Terminal-wrapped, upper- or lower-case prefix, extra keys ignored."""
    compact = "".join(str(text).split())
    match = _PREFIX.match(compact)
    if not match:
        raise _bad("it should start with ADK1.")
    if match.group(1) != "1":
        raise PairingError("code_version", _NEWER)
    try:
        body = json.loads(_b64decode(match.group(2)))
    except (ValueError, UnicodeDecodeError):
        raise _bad("the text after ADK1. is damaged") from None
    if not isinstance(body, dict):
        raise _bad("the text after ADK1. is damaged")

    version = body.get("v")
    if isinstance(version, int) and not isinstance(version, bool) and version > 1:
        raise PairingError("code_version", _NEWER)
    required = ["v", "url", "code", "exp", "tls", "name"]
    if body.get("tls") == "pin":
        required.append("pin")
    missing = [k for k in required if k not in body]
    if missing:
        raise PairingError("code_incomplete",
                           f"This pairing code is missing {', '.join(missing)}; "
                           "copy the whole code again.")

    if version != 1 or isinstance(version, bool):
        raise _bad("unknown version")
    url = body["url"]
    parts = urlsplit(url) if isinstance(url, str) else None
    if (parts is None or parts.scheme != "https" or not parts.hostname or parts.path
            or parts.query or parts.fragment or "@" in parts.netloc):
        raise _bad("the server address must be https:// with no path")
    code = body["code"]
    if not isinstance(code, str) or len(code) != 22 or not _B64URL.match(code):
        raise _bad("the secret is the wrong length")
    exp = body["exp"]
    if isinstance(exp, bool) or not isinstance(exp, int):
        raise _bad("the expiry is not a time")
    tls = body["tls"]
    if tls not in ("ca", "pin"):
        raise _bad("unknown certificate mode")
    pin = body.get("pin", "")
    if tls == "pin" and not _valid_pin(pin):
        raise _bad("the certificate pin is not sha256/<base64>")
    if tls == "ca" and "pin" in body:
        raise _bad("a pin is only allowed with tls=pin")
    name = body["name"]
    if not isinstance(name, str) or len(name) > 60:
        raise _bad("the deck name is too long")
    return PairingCode(url=url, code=code, exp=exp, tls=tls, name=name, pin=pin)


# ── shared file plumbing ─────────────────────────────────────────────────────


class _JsonFile:
    def __init__(self, state_dir: str | os.PathLike, filename: str, key: str,
                 clock: Callable[[], float]) -> None:
        self.dir = Path(state_dir)
        self.path = self.dir / filename
        self._lock_path = self.dir / f".{filename}.lock"
        self._key = key
        self._clock = clock

    def _now(self) -> int:
        return int(self._clock())

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the flock

    def _load(self, *, strict: bool) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            rows = data[self._key]
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ValueError
            return rows
        except FileNotFoundError:
            return []
        except (OSError, ValueError, KeyError, TypeError):
            if strict:
                raise ValueError(f"{self.path} is damaged; move it aside and pair again") from None
            return []

    def _save(self, rows: list[dict]) -> None:
        text = json.dumps({self._key: rows}, indent=1, sort_keys=True) + "\n"
        atomic.write_text(self.path, text, mode=0o600)


def _find(rows: list[dict], digest: str) -> dict | None:
    found = None
    for row in rows:  # no early exit: every entry costs the same
        if hmac.compare_digest(str(row.get("sha256", "")), digest):
            found = row
    return found


# ── the pairing-code store ───────────────────────────────────────────────────


class PairingStore(_JsonFile):
    def __init__(self, state_dir: str | os.PathLike, *,
                 clock: Callable[[], float] = time.time) -> None:
        super().__init__(state_dir, "pairing.json", "codes", clock)

    def mint(self) -> tuple[str, int]:
        """A fresh secret and its expiry. Only the hash is written."""
        secret = secrets.token_urlsafe(16)
        now = self._now()
        exp = now + CODE_TTL
        with self._locked():
            rows = [r for r in self._load(strict=False)
                    if int(r.get("exp", 0)) + KEEP_DEAD_FOR > now]
            live = sorted((r for r in rows if not r.get("used") and int(r["exp"]) > now),
                          key=lambda r: int(r["exp"]))
            for old in live[:max(0, len(live) - MAX_LIVE_CODES + 1)]:
                rows.remove(old)
            rows.append({"sha256": sha256_hex(secret), "exp": exp, "used": False})
            self._save(rows)
        return secret, exp

    def redeem(self, secret: str) -> None:
        """Burn `secret` or raise `pair_unknown` / `pair_used` / `pair_expired`."""
        digest = sha256_hex(secret)
        with self._locked():
            rows = self._load(strict=False)
            row = _find(rows, digest)
            if row is None:
                raise PairingError("pair_unknown", "That pairing code is not one this "
                                   "server made. Run deckctl pair for a new one.")
            if row.get("used"):
                raise PairingError("pair_used", "That pairing code was already used. "
                                   "Run deckctl pair for a new one.")
            if self._now() >= int(row.get("exp", 0)):
                raise PairingError("pair_expired", "That pairing code has expired. "
                                   "Run deckctl pair for a new one.")
            row["used"] = True
            self._save(rows)


def mint_code(store: PairingStore, *, url: str, name: str, tls: str = "ca",
              pin: str = "") -> tuple[str, int]:
    """What `deckctl pair` prints: the full ADK1 text and its expiry."""
    secret, exp = store.mint()
    return encode(PairingCode(url=url, code=secret, exp=exp, tls=tls, name=name,
                              pin=pin if tls == "pin" else "")), exp


# ── the device-token store ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    created: int
    last_seen: int


def _device(row: dict) -> Device:
    return Device(id=str(row["id"]), name=str(row.get("name", "")),
                  created=int(row.get("created", 0)), last_seen=int(row.get("last_seen", 0)))


def clean_device_name(name: object) -> str:
    """1-60 printable characters, trimmed; ValueError otherwise."""
    if not isinstance(name, str):
        raise ValueError("device name must be text")
    name = name.strip()
    if not 1 <= len(name) <= 60 or any(unicodedata.category(c) == "Cc" for c in name):
        raise ValueError("device name must be 1-60 printable characters")
    return name


class DeviceStore(_JsonFile):
    PREFIX = "adt_"

    def __init__(self, state_dir: str | os.PathLike, *,
                 clock: Callable[[], float] = time.time) -> None:
        super().__init__(state_dir, "devices.json", "devices", clock)

    def add(self, name: object) -> tuple[str, Device]:
        """A new device token (returned once, never stored) and its record."""
        name = clean_device_name(name)
        token = self.PREFIX + secrets.token_urlsafe(32)
        now = self._now()
        with self._locked():
            rows = self._load(strict=True)
            taken = {r.get("id") for r in rows}
            device_id = "d_" + secrets.token_hex(4)
            while device_id in taken:
                device_id = "d_" + secrets.token_hex(4)
            row = {"id": device_id, "name": name, "sha256": sha256_hex(token),
                   "created": now, "last_seen": now}
            rows.append(row)
            self._save(rows)
        return token, _device(row)

    def verify(self, token: str) -> Device | None:
        """The device this token belongs to, or None. Never raises on bad input."""
        if not isinstance(token, str) or not token.startswith(self.PREFIX) or len(token) != 47:
            return None
        digest = sha256_hex(token)
        row = _find(self._load(strict=False), digest)
        if row is None:
            return None
        now = self._now()
        if now - int(row.get("last_seen", 0)) < LAST_SEEN_EVERY:
            return _device(row)
        with self._locked():
            rows = self._load(strict=False)
            row = _find(rows, digest)  # re-read: a revoke may have landed meanwhile
            if row is None:
                return None
            row["last_seen"] = now
            self._save(rows)
        return _device(row)

    def list(self) -> list[Device]:
        return [_device(r) for r in self._load(strict=False) if "id" in r]

    def revoke(self, device_id: str) -> bool:
        with self._locked():
            rows = self._load(strict=True)
            kept = [r for r in rows if r.get("id") != device_id]
            if len(kept) == len(rows):
                return False
            self._save(kept)
        return True
