"""OpenCode Go subscription usage for the plan-usage header.

`GET https://opencode.ai/zen/go/v1/usage` returns rolling, weekly, and monthly
utilisation percentages with reset timestamps. Auth is the API key stored in
`~/.local/share/opencode/auth.json` under `opencode.key`.

The key is never logged, never cached to disk, and never leaves this module
except as a bearer token in the request.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from ..paths import BUS_DIR, OPENCODE_AUTH_JSON

USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
CACHE_FILE = BUS_DIR / "opencode-go-usage-cache.json"

# Plan usage moves slowly and we want to be a polite caller.
REFRESH_SECONDS = 300.0
TIMEOUT_SECONDS = 8.0
MAX_BACKOFF_SECONDS = 3600.0

WINDOWS = [
    ("rolling", "opencode_rolling", "go 5-hour"),
    ("weekly", "opencode_weekly", "go weekly"),
    ("monthly", "opencode_monthly", "go monthly"),
]

USER_AGENT = "AgentDeck/1.0 (plan-usage polling)"


def _load_disk() -> dict | None:
    """Last good reading, so a restart shows cached numbers."""
    try:
        data = json.loads(CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("windows"):
        return None
    data["stale"] = True
    data["reason"] = "cached"
    return data


def _save_disk(payload: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def _read_key() -> str | None:
    try:
        raw = OPENCODE_AUTH_JSON.read_text()
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    opencode = payload.get("opencode") if isinstance(payload, dict) else None
    if not isinstance(opencode, dict):
        return None
    key = opencode.get("key")
    return key if isinstance(key, str) and key else None


def _shape(payload: dict) -> dict:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        usage = {}

    windows = []
    for source_key, window_key, label in WINDOWS:
        entry = usage.get(source_key)
        if not isinstance(entry, dict):
            continue
        percent = entry.get("percent")
        windows.append(
            {
                "key": window_key,
                "label": label,
                "percent": float(percent) if isinstance(percent, (int, float)) else 0.0,
                "severity": "normal",
                "resets_at": entry.get("resetsAt"),
            }
        )

    return {
        "available": True,
        "stale": False,
        "windows": windows,
    }


class OpencodeGoUsageReader:
    """Polls the OpenCode Go usage endpoint on a slow cadence and caches it."""

    def __init__(self) -> None:
        self._key: str | None = None
        self._fetched_at = float("-inf")
        self._backoff = 0.0
        self._cache: dict = _load_disk() or {
            "available": False,
            "reason": "starting",
            "windows": [],
        }

    def _degrade(self, reason: str) -> dict:
        """Never throw away good numbers because one request failed."""
        if self._cache.get("windows"):
            self._cache = {**self._cache, "stale": True, "reason": reason}
        else:
            self._cache = {"available": False, "stale": False, "reason": reason, "windows": []}
        return self._cache

    def _request(self, key: str) -> dict | None:
        req = urllib.request.Request(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def poll(self) -> dict:
        now = time.monotonic()
        if now - self._fetched_at < max(REFRESH_SECONDS, self._backoff):
            return self._cache
        self._fetched_at = now

        if self._key is None:
            self._key = _read_key()
        if self._key is None:
            return self._degrade("no opencode key")

        try:
            payload = self._request(self._key)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = 0.0
                try:
                    retry_after = float(exc.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    pass
                self._backoff = min(
                    MAX_BACKOFF_SECONDS,
                    retry_after or max(REFRESH_SECONDS * 2, self._backoff * 2),
                )
                return self._degrade("rate limited")
            if exc.code in (401, 403):
                fresh = _read_key()
                if fresh and fresh != self._key:
                    self._key = fresh
                    try:
                        payload = self._request(fresh)
                    except Exception:
                        return self._degrade("auth failed")
                else:
                    self._key = None
                    return self._degrade("token expired")
            else:
                return self._degrade(f"http {exc.code}")
        except Exception:
            return self._degrade("unreachable")

        self._backoff = 0.0
        self._cache = _shape(payload)
        self._cache["fetched_at"] = time.time()
        _save_disk(self._cache)
        return self._cache
