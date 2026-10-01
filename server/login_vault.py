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

import json
import logging
import os
import threading
import time
from pathlib import Path

from . import atomic, deckconfig, sandbox
from .paths import BUS_DIR

_log = logging.getLogger(__name__)

VAULT_PATH = BUS_DIR / "browser" / "login-vault" / "cookies.json"
#: Handoff ids already fanned out, beside the vault, so a retried answer or a
#: restarted daemon cannot share one sign-in twice.
SHARED_NAME = "shared-handoffs.json"
MAX_COOKIES = 5000
MAX_SHARED_IDS = 500

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
    never log the result."""
    rows = (driver.send("Network.getAllCookies") or {}).get("cookies")
    out = [_settable(r) for r in rows] if isinstance(rows, list) else []
    return [r for r in out if r]


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


def merge(old: list[dict], new: list[dict]) -> list[dict]:
    """`new` wins on the same (name, domain, path, partition); expired rows go."""
    now = time.time()
    by_key = {_key(r): r for r in old}
    by_key.update({_key(r): r for r in new})
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
    path = Path(path or VAULT_PATH)
    _private_dir(path)
    atomic.write_text(path, json.dumps(cookies), mode=0o600)


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
    cookies = load()
    if not cookies:
        return 0
    try:
        count = import_cookies(driver, cookies)
    except Exception as exc:  # noqa: BLE001 - a desk must start without it
        _log.warning("login vault: could not seed %s (%s)", desk, _why(exc))
        return 0
    _log.info("login vault: seeded %s with %d cookies", desk, count)
    return count


def _push(cookies: list[dict], *, skip: str | None = None) -> tuple[int, list[str]]:
    """Write `cookies` into every running desk's browser but `skip`'s.
    Returns (desks written, desks that failed)."""
    done, failed = 0, []
    for desk in running_desks():
        if desk == skip:
            continue
        try:
            driver = driver_for(desk)
            if driver.page_target() is None:
                continue  # no browser yet: it is seeded when it starts
            import_cookies(driver, cookies)
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
            cookies = export_cookies(driver_for(source))
        except Exception as exc:  # noqa: BLE001
            _log.warning("login vault: could not read %s (%s)", source, _why(exc))
            return {"shared": False, "reason": "source_unreachable"}
        save(merge(load(path), cookies), path)

    done, failed = _push(cookies, skip=source)
    _log.info("login vault: shared %d cookies from %s to %d desks (%d failed)",
              len(cookies), source, done, len(failed))
    return {"shared": True, "source": source, "cookies": len(cookies),
            "desks": done, "failed": failed}


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
    rows = [r for r in (_settable(c) for c in cookies) if r and _live(r, now)]
    if not rows:
        raise ShareRefused(400, "no_cookies", "no usable cookie in the upload")
    path = Path(path or VAULT_PATH)
    with _lock:
        save(merge(load(path), rows), path)
    done, failed = _push(rows)
    _log.info("login vault: %s sign-in shared %d cookies to %d desks (%d failed)",
              source, len(rows), done, len(failed))
    return {"shared": True, "source": source, "cookies": len(rows),
            "desks": done, "failed": failed}
