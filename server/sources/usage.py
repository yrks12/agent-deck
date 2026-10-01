"""Real Anthropic plan usage — the numbers behind `/usage`.

`GET https://api.anthropic.com/api/oauth/usage` returns the 5-hour session
window and the 7-day weekly window as utilisation percentages with reset
timestamps. Auth is the OAuth access token Claude Code already stores: first
`$CLAUDE_CONFIG_DIR/.credentials.json`, then `~/.claude/.credentials.json`
(Linux boxes), then the macOS login keychain item "Claude Code-credentials".

The token is held in memory only, never logged, and never leaves this process.
We NEVER refresh it: the CLI owns refresh and rewrites the file/keychain item
when a desk runs, so we re-read on every poll and pick the new one up for free.
An `expiresAt` in the past is reported as `token_expired` (last numbers kept,
stale) without sending the dead token anywhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from ..paths import BUS_DIR, CLAUDE_HOME
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CACHE_FILE = BUS_DIR / "usage-cache.json"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CONFIG_DIR = CLAUDE_HOME
HOME_DIR = Path.home()

# Plan usage moves slowly and the endpoint rate-limits: 45s earned a 429.
REFRESH_SECONDS = 300.0
TIMEOUT_SECONDS = 8.0
MAX_BACKOFF_SECONDS = 3600.0

# Windows the deck shows, in display order.
WINDOWS = [
    ("session", "session", "5-hour session"),
    ("weekly_all", "weekly", "weekly"),
]


def _load_disk() -> dict | None:
    """Last good reading, so a restart or a rate-limit window shows numbers."""
    try:
        data = json.loads(CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("windows"):
        return None
    data["stale"] = True
    data["reason"] = "cached"
    data["reason_code"] = "cached"
    return data


def _save_disk(payload: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def _keychain_payload() -> dict | None:
    """macOS only; the Linux box has no `security` binary."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _file_payload(directory: Path) -> dict | None:
    try:
        payload = json.loads((directory / ".credentials.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_login() -> dict | None:
    """The OAuth block from the first source that has a token."""
    for source in (
        lambda: _file_payload(CONFIG_DIR),
        lambda: _file_payload(HOME_DIR / ".claude"),
        _keychain_payload,
    ):
        payload = source()
        if not payload:
            continue
        oauth = payload.get("claudeAiOauth") or payload
        token = oauth.get("accessToken")
        if isinstance(token, str) and token:
            return oauth
    return None


def _read_token() -> str | None:
    login = _read_login()
    return login["accessToken"] if login else None


_PLAN_NAMES = {"pro": "Pro", "team": "Team", "enterprise": "Enterprise"}


def plan_info(subscription, tier) -> dict | None:
    """Closed map from the login's subscriptionType + rateLimitTier."""
    if not isinstance(subscription, str) or not subscription:
        return None
    tier = tier if isinstance(tier, str) and tier else None
    if subscription == "max":
        name = "Max"
        if tier and tier.endswith("_max_5x"):
            name = "Max 5\u00d7"
        elif tier and tier.endswith("_max_20x"):
            name = "Max 20\u00d7"
    else:
        name = _PLAN_NAMES.get(subscription) or subscription.replace("_", " ").title()
    return {"name": name, "subscription": subscription, "tier": tier}


# Legacy reason text (the old board prints it) and the C4 slug for each state.
_REASONS = {
    "starting": "starting",
    "no_login": "no keychain access",
    "token_expired": "token expired",
    "rate_limited": "rate limited",
    "unreachable": "unreachable",
    "auth_failed": "auth failed",
}


class UsageReader:
    """Polls the plan-usage endpoint on a slow cadence and caches the result."""

    def __init__(self) -> None:
        self._fetched_at = float("-inf")
        self._backoff = 0.0
        self._sub: dict | None = None
        self._cache: dict = _load_disk() or {
            "available": False,
            "reason": _REASONS["starting"],
            "reason_code": "starting",
            "windows": [],
            "limits": [],
        }

    def _degrade(self, code: str, reason: str | None = None) -> dict:
        """Never throw away good numbers because one request failed.

        The first version replaced the cache on any error, so a single 429 blanked
        the meters even though the last reading was minutes old and still true.
        """
        reason = reason or _REASONS.get(code, code)
        if self._cache.get("windows"):
            self._cache = {**self._cache, "stale": True, "reason": reason,
                           "reason_code": code}
        else:
            self._cache = {"available": False, "reason": reason, "reason_code": code,
                           "windows": [], "limits": []}
        self._cache["subscription"] = self._sub or self._cache.get("subscription")
        self._cache["next_refresh_at"] = time.time() + max(REFRESH_SECONDS, self._backoff)
        return self._cache

    def _request(self, token: str) -> dict | None:
        req = urllib.request.Request(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def poll(self) -> dict:
        now = time.monotonic()
        if now - self._fetched_at < max(REFRESH_SECONDS, self._backoff):
            return self._cache
        self._fetched_at = now

        # Re-read every poll: the CLI rewrites the file when it refreshes, and
        # that is the only refresh there is. We never call the refresh endpoint.
        token = _read_token()
        if token is None:
            return self._degrade("no_login")
        login = _read_login() or {}
        self._sub = plan_info(login.get("subscriptionType"), login.get("rateLimitTier"))
        expires = login.get("expiresAt")
        if isinstance(expires, (int, float)) and expires / 1000.0 <= time.time():
            return self._degrade("token_expired")

        try:
            payload = self._request(token)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                # Respect Retry-After when offered, else double the wait.
                retry_after = 0.0
                try:
                    retry_after = float(exc.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    pass
                self._backoff = min(
                    MAX_BACKOFF_SECONDS,
                    retry_after or max(REFRESH_SECONDS * 2, self._backoff * 2),
                )
                return self._degrade("rate_limited")
            if exc.code in (401, 403):
                return self._degrade("token_expired")
            return self._degrade("unreachable", f"http {exc.code}")
        except Exception:
            return self._degrade("unreachable")

        self._backoff = 0.0
        self._cache = _shape(payload)
        self._cache["subscription"] = self._sub
        self._cache["fetched_at"] = time.time()
        self._cache["next_refresh_at"] = self._cache["fetched_at"] + REFRESH_SECONDS
        _save_disk(self._cache)
        return self._cache


# C4 windows: one per limits[] entry of these kinds.
_KIND_LABELS = {"session": "5-hour session", "weekly_all": "Weekly"}


def _scope_name(scope) -> str | None:
    if isinstance(scope, str):
        return scope or None
    if isinstance(scope, dict):
        for part in ("model", "surface"):
            inner = scope.get(part)
            if isinstance(inner, dict):
                inner = inner.get("display_name") or inner.get("id")
            if isinstance(inner, str) and inner:
                return inner
    return None


def _percent(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _limit_windows(payload: dict) -> list[dict]:
    out = []
    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind not in ("session", "weekly_all", "weekly_scoped"):
            continue
        scope = _scope_name(entry.get("scope")) if kind == "weekly_scoped" else None
        if kind == "weekly_scoped":
            key = f"weekly_scoped:{scope}" if scope else "weekly_scoped"
            label = f"Weekly \u00b7 {scope}" if scope else "Weekly \u00b7 scoped"
        else:
            key, label = kind, _KIND_LABELS[kind]
        out.append({
            "key": key,
            "label": label,
            "percent": _percent(entry.get("percent")),
            "severity": entry.get("severity") or "normal",
            "resets_at": entry.get("resets_at"),
            "scope": scope,
        })
    if out:
        return out
    # Older shapes only carry the flat five_hour / seven_day objects.
    for flat_key, key in (("five_hour", "session"), ("seven_day", "weekly_all")):
        flat = payload.get(flat_key)
        if isinstance(flat, dict):
            out.append({
                "key": key, "label": _KIND_LABELS[key],
                "percent": _percent(flat.get("utilization")),
                "severity": "normal", "resets_at": flat.get("resets_at"),
                "scope": None,
            })
    return out


def _breakdown(payload: dict) -> dict | None:
    raw = payload.get("seven_day_breakdown")
    if not isinstance(raw, dict) or not isinstance(raw.get("rows"), list):
        return None
    rows = [
        {"key": r.get("key"), "label": r.get("display_name") or r.get("key"),
         "percent": r.get("percent")}
        for r in raw["rows"] if isinstance(r, dict) and r.get("key")
    ]
    return {"as_of": raw.get("as_of"), "rows": rows}


def _shape(payload: dict) -> dict:
    limits = _limit_windows(payload)

    # The old board and collector._merge_plan_usage read `windows` with the keys
    # "session" / "weekly"; keep exactly that, richer data rides in `limits`.
    legacy = []
    for kind, key, label in WINDOWS:
        w = next((x for x in limits if x["key"] == kind), None)
        if w is not None:
            legacy.append({"key": key, "label": label, "percent": w["percent"],
                           "severity": w["severity"], "resets_at": w["resets_at"]})

    extra = payload.get("extra_usage") or {}
    return {
        "available": True,
        "stale": False,
        "reason": None,
        "reason_code": None,
        "windows": legacy,
        "limits": limits,
        "breakdown": _breakdown(payload),
        "extra_usage_enabled": bool(extra.get("is_enabled")),
        "extra_usage": {"enabled": bool(extra.get("is_enabled")),
                        "spend_limit_reached": bool(extra.get("spend_limit_reached"))},
    }
