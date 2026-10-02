"""K3 rate limiting and ban, in-process, keyed by client IP.

Rules (docs/plans/2026-09-30-easy-setup.md section 3.3):

* 10 failed bearer checks from one IP in 10 minutes -> that IP is banned for
  30 minutes; every `/v1` answer to it is 429 `rate_limited`.
* `/v1/pair` allows 5 attempts per IP per 10 minutes, whatever their outcome.
* Every auth failure writes one journal line,
  `deck-auth-fail ip=<ip> route=<path> reason=<reason>`, so a fail2ban jail can
  be added later without code changes. The presented token is never an
  argument here, so it cannot end up in that line.

Bans live in memory only; a restart clears them. That is acceptable because
the secrets are 128/256-bit -- this stops noise, not a feasible guess.

Who "the client" is: the TCP peer, except when the peer is loopback **and**
`network.mode = "public"`, where the only loopback caller that carries
`X-Forwarded-For` is Caddy, and its last hop is the real client.
`server/deckconfig.py` refuses public mode with a non-loopback bind, which is
what keeps that header unforgeable.
"""
from __future__ import annotations

import ipaddress
import logging
import math
import re
import threading
import time
from collections import deque
from typing import Callable

__all__ = ["RateLimiter", "client_ip", "log_failure", "LOGGER_NAME"]

LOGGER_NAME = "deck.auth"
_LOG = logging.getLogger(LOGGER_NAME)

AUTH_FAILS = 10
AUTH_WINDOW = 600
BAN_SECONDS = 1800
PAIR_ATTEMPTS = 5
PAIR_WINDOW = 600

_UNSAFE = re.compile(r"[^\x21-\x7e]")


def _ip(text: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address((text or "").strip()))
    except ValueError:
        return None


def client_ip(peer: str | None, forwarded_for: str | None, mode: str) -> str:
    if not peer:
        return "unknown"
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = False
    if loopback and mode == "public" and forwarded_for:
        hop = _ip(forwarded_for.split(",")[-1])
        if hop:
            return hop
    return peer


def log_failure(ip: str, route: str, reason: str) -> None:
    """One fail2ban-ready line. Anything that is not printable ASCII becomes `?`."""
    _LOG.warning("deck-auth-fail ip=%s route=%s reason=%s",
                 _UNSAFE.sub("?", ip), _UNSAFE.sub("?", route), _UNSAFE.sub("?", reason))


class RateLimiter:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 max_tracked: int = 10_000) -> None:
        self._clock = clock
        self._max = max_tracked
        self._lock = threading.Lock()
        self._fails: dict[str, deque[float]] = {}
        self._pairs: dict[str, deque[float]] = {}
        self._bans: dict[str, float] = {}

    def tracked(self) -> int:
        with self._lock:
            return len(self._fails) + len(self._pairs) + len(self._bans)

    def banned(self, ip: str) -> int:
        """Seconds left on this IP's ban (rounded up), or 0."""
        with self._lock:
            until = self._bans.get(ip)
            if until is None:
                return 0
            left = until - self._clock()
            if left <= 0:
                del self._bans[ip]
                return 0
            return math.ceil(left)

    def auth_failed(self, ip: str) -> None:
        now = self._clock()
        with self._lock:
            hits = self._window(self._fails, ip, now, AUTH_WINDOW)
            hits.append(now)
            if len(hits) >= AUTH_FAILS:
                del self._fails[ip]
                self._bans[ip] = now + BAN_SECONDS
                self._bound(self._bans)

    def pair_attempt(self, ip: str) -> int:
        """0 and the attempt is counted, or the seconds until one is allowed."""
        now = self._clock()
        with self._lock:
            hits = self._window(self._pairs, ip, now, PAIR_WINDOW)
            if len(hits) >= PAIR_ATTEMPTS:
                return max(1, math.ceil(hits[0] + PAIR_WINDOW - now))
            hits.append(now)
            return 0

    def _window(self, table: dict[str, deque[float]], ip: str, now: float,
                width: float) -> deque[float]:
        hits = table.pop(ip, None) or deque()
        while hits and hits[0] <= now - width:
            hits.popleft()
        table[ip] = hits  # re-insert: dict order is now least-recently-seen first
        self._bound(table)
        return hits

    def _bound(self, table: dict) -> None:
        # A spray of addresses must not grow memory without limit. Forgetting
        # the least recently seen IP only ever errs towards letting one through.
        while len(table) > self._max:
            del table[next(iter(table))]
