"""Sign in once; every desk's browser is signed in.

The owner, verbatim: *"why does each agent need me to log in on his computer
if it's the same server?"* Each desk has its own container and its own
Chromium profile (`server/sandbox.py`), so a sign-in on one desk's screen used
to sign nothing else in.

**How.** When a browser sign-in handoff is answered `done`, the desk the owner
just signed in on has its cookies read over CDP (`Network.getAllCookies`),
merged into one vault on the deck's disk, and written into every other
running desk's browser (`Network.setCookies`). A desk whose browser starts
later is seeded from the vault on launch (`desk_computer.ensure`).

**Why CDP and not a shared profile or a file copy.** Measured on the box:

* Chromium will not open one profile from two containers at once -- the
  second exits 21, "The profile appears to be in use by another Chromium
  process ... on another computer". A shared profile means one browser.
* Copying the `Cookies` SQLite file works only with Chromium stopped, and it
  loses **session** cookies: they are never written to disk (a session cookie
  set on httpbin.org was gone after `Browser.close`; a Max-Age one survived
  as a `v10` row). Many sign-ins are session cookies.
* `Network.setCookies` into a live second container took 0.14 s and httpbin's
  `/cookies` echoed the transferred cookie straight back.

**What is not shared.** localStorage / IndexedDB (a site that keeps its token
there needs a sign-in per desk), and anything bound to the device: a site
that pins a session to a device key may still re-challenge.

**Security.** Every desk gets every login -- the owner's full-access ruling.
The vault is `0700` / `0600`, owned by the deck user, under the bus dir (no
deploy backup copies it; nothing ships it off the box), and no cookie value
is ever logged or returned: reports carry counts and desk names only.
`[desks] shared_logins = false` in deck.toml keeps each desk isolated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import atomic, deckconfig, sandbox
from .paths import BUS_DIR

_log = logging.getLogger(__name__)

VAULT_PATH = BUS_DIR / "browser" / "login-vault" / "cookies.json"
#: The localStorage half of the vault, beside the cookie file. A site that
#: keeps its session in localStorage (vapi: the WorkOS tokens) is not signed in
#: by cookies alone, so those keys travel here. See `AUTH_LS`.
LS_PATH = BUS_DIR / "browser" / "login-vault" / "local-storage.json"
#: Handoff ids already fanned out, beside the vault, so a retried answer or a
#: restarted daemon cannot share one sign-in twice.
SHARED_NAME = "shared-handoffs.json"
#: Per desk, a fingerprint of each auth cookie as last seen or pushed there, so
#: the sync can tell a value the site just SET on a desk from a stale copy.
SEEN_NAME = "desk-seen.json"
MAX_COOKIES = 5000
MAX_SHARED_IDS = 500

#: Cookie names that mark a sign-in. The live sync treats a change to one of
#: these on a desk as "the owner just logged in here" and fans it out; a change
#: to an analytics cookie (`_ga`) is ignored so the poll stays quiet. A
#: superset is safe -- an extra name only means one more cookie is watched.
AUTH_NAMES = frozenset({
    "sessionid", "ds_user_id", "sessionid_sign",         # instagram / meta
    "li_at", "liap",                                      # linkedin
    "auth_token", "ct0",                                  # x / twitter
    "next-auth.session-token", "__Secure-next-auth.session-token",
    "connect.sid", "session", "_session", "sid",
    "workos_rt", "access_token", "refresh_token", "jwt", "appSession",
    "remember_token", "remember_web", "laravel_session",
    "_rails_session", "JSESSIONID", "PHPSESSID", "token",
})

#: Cookies bound to the browser or device that do NOT grant a session when
#: copied to another Chromium. MEASURED on the box 2026-10-01: a desk holding
#: the vault's Google cookies still lands on the signed-out page -- Google's
#: DBSC ties the session to the browser instance. They rotate constantly, so
#: the live sync never reads or pushes them: copying both fails to help and
#: would churn every desk every poll. (The handoff path may still store them.)
DEVICE_BOUND = frozenset({
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC", "SIDCC", "__Secure-ENID",
})

#: Per-origin localStorage keys that carry a session. Only these keys travel,
#: never a whole origin's storage. MEASURED on the box 2026-10-01: a signed-in
#: dashboard.vapi.ai has its session only in these localStorage keys; its
#: cookies alone leave another desk on /login.
AUTH_LS: dict[str, frozenset[str]] = {
    "https://dashboard.vapi.ai": frozenset({
        "USER_TOKEN", "ORG_TOKEN", "WORKOS_ACCESS_TOKEN", "SELECTED_ORG",
        "WORKOS_USER_EMAIL", "WORKOS_AUTH_METHOD", "AUTH_PROVIDER"}),
}
MAX_LS_VALUE = 32 * 1024

#: The switch every desk's Chromium is launched with (`browser.chrome_argv`),
#: the same one the Mac fresh sign-in uses (#164). With it, a Google session
#: made on a desk is an ordinary cookie set, so the live sync carries it.
DBSC_OFF_FLAG = "--disable-features=DeviceBoundSessionCredentials"

#: Google's sign-in cookies. Synced only while desks run with DBSC off; under
#: DBSC they are bound to one browser and copying them signs nothing in.
GOOGLE_AUTH_NAMES = frozenset({
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "LSID", "__Host-1PLSID", "__Host-3PLSID",
    # Not SIDCC / __Secure-1PSIDCC / __Secure-3PSIDCC: MEASURED on the box,
    # Google reissues them on every request (new value, one-year expiry), so
    # syncing them fanned out to every desk every tick. Google re-derives them
    # from SID; they are not a login.
})


#: Sites that revoke a session used from several browsers at once, so each desk
#: signs in on its own and nothing here ever copies their cookies to another
#: desk. MEASURED 2026-10-01 (UTC): his everyday-Chrome TikTok session, shared
#: to every desk at 17:36:14, was dead on the desks AND on his Mac by 18:19:27
#: (Mac back at tiktok.com/login); his Instagram one, shared at 23:39:25, was
#: "logged out" on a desk by 00:27; his live Stripe session sat on every desk.
#: Per-cookie merging also stitched one TikTok jar from three sessions. Added to
#: the owner's `[desks] login_sync_exclude`, never replaced by it.
PER_DESK_SITES = ("tiktok.com", "instagram.com", "stripe.com")

#: Cookie values a site writes when it signs a browser OUT. Never a login, so
#: never merged, pushed or accepted -- one desk's sign-out must not travel.
LOGGED_OUT_VALUES = frozenset({"", "deleted", "null", "undefined", "none",
                               "0", "false", "logout", "loggedout", "logged_out"})

#: Paths that sign a browser out. Every desk shares one session per site, so a
#: sign-out on one revokes it for all (MEASURED: tiktok.com/logout 17:32:50,
#: accounts.google.com/SignOutOptions 17:32:46 and 19:47:07 -- the second one
#: signed every desk out of Google). `navigate` refuses them.
SIGN_OUT_SEGMENTS = frozenset({"logout", "log_out", "log-out", "signout",
                               "sign_out", "sign-out", "signoutoptions"})


def signs_everyone_out(url: str) -> bool:
    """True for a URL whose visit signs this browser out of its site."""
    try:
        path = urlsplit(str(url or "")).path.lower()
    except ValueError:
        return False
    return any(seg in SIGN_OUT_SEGMENTS for seg in path.split("/"))


def _logged_out(row: dict) -> bool:
    return str(row.get("value") or "").strip().lower() in LOGGED_OUT_VALUES


def _on(row: dict, domains: tuple[str, ...]) -> bool:
    host = str(row.get("domain") or "").lstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def desk_dbsc_off() -> bool:
    """True when the desk launch line carries `DBSC_OFF_FLAG` -- read from the
    real argv, so the vault and the launch can never disagree."""
    try:
        return DBSC_OFF_FLAG in sandbox.browser_argv("dbsc-probe")
    except Exception:  # noqa: BLE001 - unknown means keep Google excluded
        return False


def browser_dbsc_off(desk: str) -> bool:
    """True when `desk`'s RUNNING Chromium was started with `DBSC_OFF_FLAG`.

    The launch line changes on deploy, but a browser started before that keeps
    binding Google sessions until it next restarts. Read from the live process,
    one `ps` in the container; unknown means bound (the safe answer).
    """
    try:
        proc = sandbox._run(sandbox.exec_argv(desk, ["ps", "-eo", "args"]))
    except Exception:  # noqa: BLE001
        return False
    if getattr(proc, "returncode", 1) != 0:
        return False
    out = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) \
        else str(proc.stdout or "")
    return any("--user-data-dir=" in line and DBSC_OFF_FLAG in line
               for line in out.splitlines())


def auth_names() -> frozenset[str]:
    """The cookie names whose change means a sign-in, Google's included once
    desks run with DBSC off."""
    return AUTH_NAMES | GOOGLE_AUTH_NAMES if desk_dbsc_off() else AUTH_NAMES


def device_bound() -> frozenset[str]:
    """Names the live sync must never touch: none once DBSC is off."""
    return frozenset() if desk_dbsc_off() else DEVICE_BOUND

#: The writable fields of CDP `Network.CookieParam`. `size` and `session` are
#: read-only and make Chromium reject the whole `setCookies` call.
SETTABLE = ("name", "value", "domain", "path", "secure", "httpOnly",
            "sameSite", "expires", "priority", "sameParty", "sourceScheme",
            "sourcePort", "partitionKey")

_lock = threading.Lock()


def enabled() -> bool:
    """`[desks] shared_logins`, default on. A broken config is not a reason to
    stop sharing the owner asked for, and not a reason to crash a handoff."""
    try:
        return bool(deckconfig.load().desks.shared_logins)
    except Exception:  # noqa: BLE001 - config trouble is reported by the doctor
        return True


def exclude_domains() -> tuple[str, ...]:
    """The "this desk only" sites: the measured `PER_DESK_SITES` plus the
    owner's own, lower-cased and dot-stripped. A broken config still keeps the
    built-in ones out rather than crashing the sync."""
    try:
        raw = deckconfig.load().desks.login_sync_exclude
    except Exception:  # noqa: BLE001 - config trouble is the doctor's to report
        raw = ()
    own = tuple(str(d).strip().lstrip(".").lower() for d in raw if str(d).strip())
    return tuple(dict.fromkeys(PER_DESK_SITES + own))


def _excluded(row: dict, domains: tuple[str, ...]) -> bool:
    """True for a cookie the live sync must leave alone: a device-bound name,
    or one on a "this desk only" site (the host or any subdomain of it)."""
    if row.get("name") in device_bound():
        return True
    return _on(row, domains)


# ── CDP ────────────────────────────────────────────────────────────────────


def _settable(row: dict) -> dict | None:
    if not isinstance(row, dict) or not row.get("name") or not row.get("domain"):
        return None
    out = {k: row[k] for k in SETTABLE if k in row}
    if row.get("session") or not isinstance(out.get("expires"), (int, float)) \
            or out.get("expires", -1) <= 0:
        out.pop("expires", None)  # a session cookie stays a session cookie
    return out


def _live(row: dict, now: float) -> bool:
    exp = row.get("expires")
    return not isinstance(exp, (int, float)) or exp > now


def export_cookies(driver) -> list[dict]:
    """Every cookie in `driver`'s browser, as settable rows. Values included --
    never log the result.

    Each row is stamped `_seen` with now: that is the observation time the
    last-writer-wins `merge` compares, so a cookie just read off a live browser
    always beats an older copy sitting in the vault. `_seen` is not a CDP field
    (`_settable` drops it), so it never reaches `Network.setCookies`.
    """
    rows = (driver.send("Network.getAllCookies") or {}).get("cookies")
    out = [_settable(r) for r in rows] if isinstance(rows, list) else []
    now = time.time()
    return [{**r, "_seen": now} for r in out if r]


def import_cookies(driver, cookies: list[dict]) -> int:
    """Write `cookies` into `driver`'s browser in one call. Returns the count."""
    now = time.time()
    rows = [r for r in (_settable(c) for c in cookies) if r and _live(r, now)]
    if rows:
        driver.send("Network.setCookies", {"cookies": rows})
    return len(rows)


# ── the vault ──────────────────────────────────────────────────────────────


def _key(row: dict) -> tuple:
    return (row.get("name"), row.get("domain"), row.get("path", "/"),
            json.dumps(row.get("partitionKey"), sort_keys=True))


def _seen_of(row: dict) -> float:
    """When this row was last observed. Unstamped rows read as 0, so a freshly
    read cookie (stamped now by `export_cookies`/`share_in`) beats them."""
    seen = row.get("_seen")
    return seen if isinstance(seen, (int, float)) else 0.0


def _fresh(row: dict) -> tuple[float, float, float]:
    """How new a cookie is, for last-writer-wins.

    First `_set`: when a site actually SET this value -- stamped by the live
    sync only when a desk's value changed since it last looked, and by a sign-in
    that was just made. A rotated token can carry an EARLIER expiry than the one
    it replaces (a fixed-lifetime session, a refresh), so expiry alone let a
    stale copy beat the fresh one and be pushed back over it. Expiry, then the
    observation time, only break ties among rows never seen being set.
    """
    exp = row.get("expires")
    set_at = row.get("_set")
    return (set_at if isinstance(set_at, (int, float)) else 0.0,
            exp if isinstance(exp, (int, float)) else 0.0, _seen_of(row))


def merge(old: list[dict], new: list[dict]) -> list[dict]:
    """Last writer wins per (name, domain, path, partition); expired rows go.

    A `new` row replaces the matching `old` one only when it is at least as
    fresh (`_fresh`), so the live sync can never roll a desk's newer session
    back to an older copy it happens to read. Ties fall to `new`.
    """
    now = time.time()
    by_key = {_key(r): r for r in old}
    for r in new:
        if _logged_out(r):
            continue  # a sign-out is never a login to keep or share
        k = _key(r)
        cur = by_key.get(k)
        if cur is None or _fresh(r) >= _fresh(cur):
            by_key[k] = r
    rows = [r for r in by_key.values() if _live(r, now)]
    return rows[-MAX_COOKIES:]


def _private_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)


def load(path: Path | None = None) -> list[dict]:
    path = Path(path or VAULT_PATH)
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def save(cookies: list[dict], path: Path | None = None) -> None:
    """Write the vault. A per-desk site is never kept: nothing may use it, and
    his Mac's Stripe / Instagram copies sat here unused (purged 2026-10-01).
    Each row carries `_src` -- where the login came from: `mac-chrome` (his
    everyday Chrome), `mac-fresh` (a fresh passkey sign-in), `mac` (an app
    that predates the field), `handoff` or `desk` -- so the vault is auditable.
    """
    path = Path(path or VAULT_PATH)
    domains = exclude_domains()
    cookies = [r for r in cookies if not _on(r, domains)]
    _private_dir(path)
    atomic.write_text(path, json.dumps(cookies), mode=0o600)


def _fp(value) -> str:
    """A one-way fingerprint of a cookie value: equality only, never the value."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]


def _load_seen(path: Path) -> dict:
    try:
        data = json.loads((path.parent / SEEN_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_seen(seen: dict, path: Path) -> None:
    ledger = path.parent / SEEN_NAME
    _private_dir(ledger)
    atomic.write_text(ledger, json.dumps(seen), mode=0o600)


def _claim(handoff_id: str, path: Path) -> bool:
    """True the first time `handoff_id` is seen; recorded before sharing."""
    ledger = path.parent / SHARED_NAME
    try:
        seen = json.loads(ledger.read_text(encoding="utf-8"))
        seen = seen if isinstance(seen, list) else []
    except (OSError, ValueError):
        seen = []
    if handoff_id in seen:
        return False
    _private_dir(ledger)
    atomic.write_text(ledger, json.dumps((seen + [handoff_id])[-MAX_SHARED_IDS:]),
                      mode=0o600)
    return True


# ── localStorage: the half cookies cannot carry ─────────────────────────────


def ls_origin(url: str) -> str | None:
    """The `scheme://host[:port]` of `url` if we sync localStorage for it."""
    for origin in AUTH_LS:
        if str(url or "").startswith(origin):
            return origin
    return None


def export_local_storage(driver, origin: str) -> dict:
    """The auth localStorage keys for `origin` on the page the driver is on.

    Reads only the named keys, never the whole store, and only as a value map;
    the caller must already have the driver's page on `origin` (a frame there is
    required). Returns {} on anything unexpected -- never a crash in the loop.
    """
    names = AUTH_LS.get(origin)
    if not names:
        return {}
    want = json.dumps(sorted(names))
    expr = ("(() => { const o = {}; for (const k of " + want + ") {"
            " const v = window.localStorage.getItem(k);"
            " if (v !== null) o[k] = v; } return JSON.stringify(o); })()")
    try:
        res = driver.send("Runtime.evaluate",
                          {"expression": expr, "returnByValue": True})
        raw = (res.get("result") or {}).get("value")
        out = json.loads(raw) if isinstance(raw, str) else {}
    except Exception:  # noqa: BLE001 - localStorage is best effort
        return {}
    return {k: v for k, v in out.items()
            if isinstance(v, str) and len(v) <= MAX_LS_VALUE} \
        if isinstance(out, dict) else {}


def import_local_storage(driver, items: dict) -> int:
    """Write `items` into the page's localStorage. The page must be on the
    matching origin. Returns how many keys were written."""
    clean = {k: v for k, v in (items or {}).items()
             if isinstance(k, str) and isinstance(v, str) and len(v) <= MAX_LS_VALUE}
    if not clean:
        return 0
    expr = ("(() => { const d = " + json.dumps(clean) + ";"
            " for (const k in d) window.localStorage.setItem(k, d[k]);"
            " return Object.keys(d).length; })()")
    driver.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    return len(clean)


def load_ls(path: Path | None = None) -> dict:
    """The localStorage vault: {origin: {key: {"value", "_seen"}}}."""
    path = Path(path or LS_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_ls(store: dict, path: Path | None = None) -> None:
    path = Path(path or LS_PATH)
    _private_dir(path)
    atomic.write_text(path, json.dumps(store), mode=0o600)


def merge_ls(store: dict, origin: str, items: dict, *, now: float | None = None) -> bool:
    """Last writer wins per key into `store[origin]`. True if anything changed."""
    now = time.time() if now is None else now
    bucket = dict(store.get(origin) or {})
    changed = False
    for key, value in items.items():
        cur = bucket.get(key)
        if not isinstance(cur, dict) or cur.get("value") != value:
            bucket[key] = {"value": value, "_seen": now}
            changed = True
    if changed:
        store[origin] = bucket
    return changed


def _ls_values(store: dict, origin: str) -> dict:
    """The plain {key: value} map for `origin`, for injection."""
    bucket = store.get(origin) or {}
    return {k: v.get("value") for k, v in bucket.items()
            if isinstance(v, dict) and isinstance(v.get("value"), str)}


# ── the desks ──────────────────────────────────────────────────────────────


def running_desks() -> list[str]:
    """Desks whose computer is up, asked of Docker (the name is the registry)."""
    try:
        proc = sandbox._run(["docker", "ps", "--format", "{{.Names}}",
                             "--filter", f"name=^{sandbox.NAME_PREFIX}"])
    except sandbox.SandboxError:
        return []
    names = proc.stdout.decode("utf-8", "replace").split() if proc.returncode == 0 else []
    return sorted(n[len(sandbox.NAME_PREFIX):] for n in names
                  if n.startswith(sandbox.NAME_PREFIX))


def driver_for(desk: str):
    from .desk_computer import ContainerCDP  # noqa: PLC0415 - it imports us
    return ContainerCDP(desk)


def _why(exc: Exception) -> str:
    """A slug, never the message: CDP errors echo the params, and the params of
    `setCookies` are the cookies."""
    reason = getattr(exc, "reason", None)
    return reason if isinstance(reason, str) and reason.isidentifier() else type(exc).__name__


def seed(desk: str, driver) -> int:
    """A desk's browser just started: give it every login in the vault."""
    if not enabled():
        return 0
    domains = exclude_domains()
    cookies = [r for r in load() if not _on(r, domains) and not _logged_out(r)]
    if not cookies:
        return 0
    try:
        count = import_cookies(driver, cookies)
    except Exception as exc:  # noqa: BLE001 - a desk must start without it
        _log.warning("login vault: could not seed %s (%s)", desk, _why(exc))
        return 0
    _log.info("login vault: seeded %s with %d cookies", desk, count)
    return count


def _push(cookies: list[dict], *, skip: str | None = None,
          bound: set[str] | frozenset[str] = frozenset()) -> tuple[int, list[str]]:
    """Write `cookies` into every running desk's browser but `skip`'s.
    A desk in `bound` (its browser still binds Google sessions) gets no Google
    sign-in cookie. Returns (desks written, desks that failed)."""
    done, failed = 0, []
    domains = exclude_domains()
    cookies = [r for r in cookies if not _on(r, domains) and not _logged_out(r)]
    for desk in running_desks():
        if desk == skip:
            continue
        rows = cookies if desk not in bound else \
            [r for r in cookies if r.get("name") not in GOOGLE_AUTH_NAMES]
        try:
            driver = driver_for(desk)
            if driver.page_target() is None:
                continue  # no browser yet: it is seeded when it starts
            import_cookies(driver, rows)
            done += 1
        except Exception as exc:  # noqa: BLE001 - one dead desk is not all of them
            _log.warning("login vault: could not share into %s (%s)", desk, _why(exc))
            failed.append(desk)
    return done, failed


def fan_out(source: str, *, handoff_id: str, path: Path | None = None) -> dict:
    """Share `source`'s sign-in with the vault and every other running desk."""
    if not enabled():
        return {"shared": False, "reason": "isolated"}
    path = Path(path or VAULT_PATH)
    with _lock:
        if not _claim(handoff_id, path):
            return {"shared": False, "reason": "already_shared"}
        try:
            now = time.time()
            cookies = [{**r, "_set": now, "_src": "handoff"}
                       for r in export_cookies(driver_for(source))]
        except Exception as exc:  # noqa: BLE001
            _log.warning("login vault: could not read %s (%s)", source, _why(exc))
            return {"shared": False, "reason": "source_unreachable"}
        save(merge(load(path), cookies), path)

    done, failed = _push(cookies, skip=source)
    _log.info("login vault: shared %d cookies from %s to %d desks (%d failed)",
              len(cookies), source, done, len(failed))
    return {"shared": True, "source": source, "cookies": len(cookies),
            "desks": done, "failed": failed}


def sync_running_desks(path: Path | None = None,
                       ls_path: Path | None = None) -> dict:
    """Catch a sign-in made by hand on a desk's own screen and fan it out.

    This is the path #94 did not have: it shared only a sign-in the owner
    answered on a handoff card, or uploaded from his Mac -- never one he typed
    straight into a desk's browser. Called on a timer (~30 s):

    1. Read every running desk's cookies. A change to an auth cookie (`AUTH_NAMES`)
       against the vault means a fresh sign-in on that desk.
    2. Merge the changes in (last writer wins) and push the merged auth cookies
       to every running desk, so they all converge within one tick.
    3. The same for the localStorage sessions (`AUTH_LS`) of any desk sitting on
       such an origin, injected into every other desk that is on it too.

    Device-bound and "this desk only" cookies are never touched (`_excluded`).
    Returns counts and desk names only -- never a cookie value.
    """
    if not enabled():
        return {"synced": False, "reason": "isolated"}
    path = Path(path or VAULT_PATH)
    ls_path = Path(ls_path or LS_PATH)
    domains = exclude_domains()
    names = auth_names()
    google = bool(names & GOOGLE_AUTH_NAMES)
    bound: set[str] = set()  # desks whose running browser still binds Google
    off: set[str] = set()    # desks checked and running with DBSC off

    jars: dict[str, list[dict]] = {}
    origins: dict[str, str] = {}
    for desk in running_desks():
        try:
            driver = driver_for(desk)
            if driver.page_target() is None:
                continue  # no browser yet: it is seeded when it starts
            rows = export_cookies(driver)
            if google and any(r.get("name") in GOOGLE_AUTH_NAMES for r in rows):
                if browser_dbsc_off(desk):
                    off.add(desk)
                else:
                    bound.add(desk)
                    rows = [r for r in rows if r.get("name") not in GOOGLE_AUTH_NAMES]
            jars[desk] = rows
            url = str(driver.page_state().get("url") or "")
            origin = ls_origin(url)
            if origin:
                origins[desk] = origin
        except Exception as exc:  # noqa: BLE001 - one dead desk is not all of them
            _log.warning("login vault: could not read %s (%s)", desk, _why(exc))
    if not jars:
        return {"synced": False, "reason": "no_running_desks"}

    with _lock:
        vault = load(path)
        before = {_key(r): r.get("value") for r in vault}
        seen = _load_seen(path)
        now = time.time()
        candidates: list[dict] = []
        stale = False
        for desk, rows in jars.items():
            mine = seen.get(desk) if isinstance(seen.get(desk), dict) else None
            for r in rows:
                if r.get("name") not in names or _excluded(r, domains) \
                        or _logged_out(r):
                    continue
                k = _key(r)
                if before.get(k) == r.get("value"):
                    continue
                last = mine.get(json.dumps(k)) if mine is not None else None
                if last is None:
                    candidates.append({**r, "_src": "desk"})  # first sight: expiry decides
                elif last != _fp(r.get("value")):
                    candidates.append({**r, "_set": now, "_src": "desk"})  # just set
                else:
                    stale = True  # unchanged here, the vault moved on: pull it up
        merged = merge(vault, candidates) if candidates else vault
        after = {_key(r): r.get("value") for r in merged}
        cookies_changed = sum(1 for k, v in after.items() if before.get(k) != v)
        if candidates:
            save(merged, path)  # at least a re-stamp; the content may be the same
        push = [r for r in merged
                if r.get("name") in names and not _excluded(r, domains)]
        _save_seen(_seen_now(jars, names, domains), path)
        ls_changed = _sync_ls(origins, ls_path, domains)

    if not candidates and not stale and not ls_changed:
        return {"synced": False, "reason": "no_change"}

    done, failed = (0, [])
    if candidates or stale:
        # Push the winners to every desk: a fresh sign-in reaches the others, and
        # a desk still on an older session is pulled UP to the vault's newer one.
        if any(r.get("name") in GOOGLE_AUTH_NAMES for r in push):
            # A browser with no Google cookie yet was not checked while reading;
            # it must not receive a Google session it would bind.
            bound |= {d for d in jars
                      if d not in bound and d not in off and not browser_dbsc_off(d)}
        done, failed = _push(push, bound=bound)
    _log.info("login vault: live sync settled %d cookie(s) and %d origin(s) "
              "across %d desk(s) (%d failed)",
              cookies_changed, ls_changed, done, len(failed))
    return {"synced": True, "cookies": cookies_changed, "origins": ls_changed,
            "desks": done, "failed": failed}


def _seen_now(jars: dict, names, domains) -> dict:
    """Each read desk's auth values as read this tick (fingerprints). A value
    a push then lands equals the vault next tick, so it is never mistaken for a
    fresh set; only desks read this tick are kept."""
    return {desk: {json.dumps(_key(r)): _fp(r.get("value")) for r in rows
                   if r.get("name") in names and not _excluded(r, domains)}
            for desk, rows in jars.items()}


def _sync_ls(origins: dict[str, str], ls_path: Path,
             domains: tuple[str, ...]) -> int:
    """Capture and fan out localStorage sessions. Returns origins changed.

    Held under the same lock as the cookie merge. A desk on an opt-out site is
    not read; an auth-LS origin's host matches the opt-out the same way a cookie
    domain does.
    """
    if not origins:
        return 0
    store = load_ls(ls_path)
    now = time.time()
    touched: set[str] = set()
    for desk, origin in origins.items():
        host = origin.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        if any(host == d or host.endswith("." + d) for d in domains):
            continue
        try:
            items = export_local_storage(driver_for(desk), origin)
        except Exception as exc:  # noqa: BLE001
            _log.warning("login vault: could not read %s storage (%s)", desk, _why(exc))
            continue
        if items and merge_ls(store, origin, items, now=now):
            touched.add(origin)
    if touched:
        save_ls(store, ls_path)
    # Inject the merged session into every desk sitting on one of these origins.
    for desk, origin in origins.items():
        values = _ls_values(store, origin)
        if not values:
            continue
        try:
            import_local_storage(driver_for(desk), values)
        except Exception as exc:  # noqa: BLE001
            _log.warning("login vault: could not write %s storage (%s)", desk, _why(exc))
    return len(touched)


def inject_on_navigate(desk: str, driver, url: str,
                       ls_path: Path | None = None) -> int:
    """A desk just navigated to `url`: if the vault holds a localStorage session
    for its origin, write it in so the page loads signed in. Cookies are already
    carried by `seed`/the live sync; this is the localStorage half. Best effort,
    returns keys written."""
    if not enabled():
        return 0
    origin = ls_origin(url)
    if not origin:
        return 0
    values = _ls_values(load_ls(ls_path), origin)
    if not values:
        return 0
    try:
        current = export_local_storage(driver, origin)
        diff = {k: v for k, v in values.items() if current.get(k) != v}
        if not diff:
            return 0  # the page already has this session: no write, no reload
        n = import_local_storage(driver, diff)
    except Exception as exc:  # noqa: BLE001 - a navigate must not fail on this
        _log.warning("login vault: could not seed %s storage (%s)", desk, _why(exc))
        return 0
    if n:
        _log.info("login vault: seeded %s with %d storage key(s) for an origin", desk, n)
    return n


def _is_browser(h) -> bool:
    return "surface=browser" in str(getattr(h, "evidence", "")).split()


def after_handoff(settled, outcome: str, *, background: bool = True):
    """Called once per answered handoff. Shares only a `done` browser one.

    In a thread by default: it is docker exec per desk, and the owner's tap
    must not wait on eight browsers.
    """
    if outcome != "done" or not _is_browser(settled):
        return {"shared": False, "reason": "not_a_browser_sign_in"}
    run = lambda: fan_out(settled.agent, handoff_id=settled.id)  # noqa: E731
    if not background:
        return run()
    threading.Thread(target=run, name="login-vault", daemon=True).start()
    return {"shared": "started"}


# ── a sign-in done on the owner's Mac ──────────────────────────────────────


class ShareRefused(Exception):
    """`reason` is the slug the route answers with; `status` its HTTP code."""

    def __init__(self, status: int, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.reason, self.detail = status, reason, detail


def share_in(cookies, *, source: str = "mac", path: Path | None = None) -> dict:
    """Cookies signed in somewhere no desk can reach -- the owner's Mac, where
    his iCloud passkeys are -- go into the vault and every running desk.

    Every desk, the one whose card asked included: it is the one stuck.
    Raises `ShareRefused`; returns counts only, never a value.
    """
    if not enabled():
        raise ShareRefused(409, "isolated",
                           "[desks] shared_logins is off; desks keep their own logins")
    if not isinstance(cookies, list):
        raise ShareRefused(400, "no_cookies", "cookies must be a list of CDP cookie rows")
    if len(cookies) > MAX_COOKIES:
        raise ShareRefused(413, "too_many_cookies",
                           f"at most {MAX_COOKIES} cookies in one sign-in")
    now = time.time()
    rows = [{**r, "_seen": now, "_set": now, "_src": source}
            for r in (_settable(c) for c in cookies)
            if r and _live(r, now) and not _logged_out(r)]
    if not rows:
        raise ShareRefused(400, "no_cookies", "no usable cookie in the upload")
    domains = exclude_domains()
    if all(_on(r, domains) for r in rows):
        raise ShareRefused(
            409, "per_desk_site",
            "this site signs everyone out when one login is shared; sign each "
            "desk in on its own screen instead")
    path = Path(path or VAULT_PATH)
    with _lock:
        save(merge(load(path), rows), path)
    done, failed = _push(rows)
    _log.info("login vault: %s sign-in shared %d cookies to %d desks (%d failed)",
              source, len(rows), done, len(failed))
    return {"shared": True, "source": source, "cookies": len(rows),
            "desks": done, "failed": failed}
