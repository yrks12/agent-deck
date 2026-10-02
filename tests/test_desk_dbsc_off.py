"""Google logins travel between desks: every desk's Chromium runs with DBSC off.

MEASURED on the box 2026-10-01: a desk seeded with another desk's Google
cookies still landed signed out -- Chromium's Device Bound Session Credentials
bind a Google session to the browser that made it. The Mac fresh sign-in
(#164) fixed the same thing by launching Chrome with
`--disable-features=DeviceBoundSessionCredentials`; desks now do the same, and
once they do, the live login sync carries Google's auth cookies too.
"""

from __future__ import annotations

import time

from server import browser, desk_computer, login_vault, sandbox

FLAG = "--disable-features=DeviceBoundSessionCredentials"


def test_every_desk_browser_launches_with_dbsc_off():
    argv = desk_computer.launch_argv("atlas")
    assert FLAG in argv
    # Chromium honours only the last --disable-features; there must be one.
    assert sum(a.startswith("--disable-features=") for a in argv) == 1


def test_the_flag_is_in_the_shared_chrome_command_line():
    argv = browser.chrome_argv(display=":99", cdp_port=9222,
                               profile_dir="/home/agent/chrome-profile",
                               url="about:blank")
    assert FLAG in argv


def test_the_vault_sees_dbsc_off_from_the_real_launch_line():
    assert login_vault.desk_dbsc_off() is True


def _google(name, value="g-session"):
    return {"name": name, "value": value, "domain": ".google.com", "path": "/",
            "expires": time.time() + 3600, "httpOnly": True, "secure": True}


def test_google_auth_cookies_are_synced_once_dbsc_is_off(monkeypatch):
    monkeypatch.setattr(login_vault, "desk_dbsc_off", lambda: True)
    for name in ("SID", "__Secure-1PSID", "__Secure-1PSIDTS", "LSID"):
        assert name in login_vault.auth_names(), name
        assert not login_vault._excluded(_google(name), ()), name


def test_google_stays_excluded_while_desks_still_bind_sessions(monkeypatch):
    monkeypatch.setattr(login_vault, "desk_dbsc_off", lambda: False)
    assert "SID" not in login_vault.auth_names()
    assert login_vault._excluded(_google("__Secure-1PSIDTS"), ())


def test_dbsc_off_is_false_when_the_launch_line_lacks_it(monkeypatch):
    monkeypatch.setattr(sandbox, "browser_argv", lambda desk, **k: ["chromium"])
    assert login_vault.desk_dbsc_off() is False


# ── the changeover: browsers started before the flag still bind sessions ────


class _Jar:
    def __init__(self, cookies):
        self.jar = [dict(c) for c in cookies]
        self.sets: list[list[dict]] = []

    def page_target(self):
        return {"id": "p"}

    def page_state(self):
        return {"url": ""}

    def send(self, method, params=None):
        if method == "Network.getAllCookies":
            return {"cookies": [dict(c) for c in self.jar]}
        if method == "Network.setCookies":
            self.sets.append(params["cookies"])
            return {}
        raise AssertionError(method)


def test_an_old_dbsc_browser_neither_gives_nor_gets_google(tmp_path, monkeypatch):
    """atlas still runs a browser started before the flag: its rotating, bound
    Google cookies must not leak into the owner's fresh session, and the fresh
    session must not be pushed into a browser that would bind it."""
    monkeypatch.setattr(login_vault, "VAULT_PATH", tmp_path / "v" / "cookies.json")
    monkeypatch.setattr(login_vault, "LS_PATH", tmp_path / "v" / "ls.json")
    monkeypatch.setattr(login_vault, "enabled", lambda: True)
    monkeypatch.setattr(login_vault, "exclude_domains", lambda: ())
    monkeypatch.setattr(login_vault, "desk_dbsc_off", lambda: True)
    old = _Jar([_google("__Secure-1PSIDTS", "bound-rotating"),
                {**_google("sessionid", "ig"), "domain": ".instagram.com"}])
    fresh = _Jar([_google("SID", "fresh-sid")])
    other = _Jar([])
    drivers = {"atlas": old, "fresh": fresh, "other": other}
    monkeypatch.setattr(login_vault, "running_desks", lambda: sorted(drivers))
    monkeypatch.setattr(login_vault, "driver_for", lambda d: drivers[d])
    monkeypatch.setattr(login_vault, "browser_dbsc_off", lambda d: d != "atlas")

    login_vault.sync_running_desks()

    vault = {c["name"]: c["value"] for c in login_vault.load()}
    assert "__Secure-1PSIDTS" not in vault          # atlas's bound cookie stays put
    assert vault["SID"] == "fresh-sid" and vault["sessionid"] == "ig"
    got_other = {c["name"] for batch in other.sets for c in batch}
    assert {"SID", "sessionid"} <= got_other          # a DBSC-off desk gets Google
    got_atlas = {c["name"] for batch in old.sets for c in batch}
    assert "sessionid" in got_atlas and "SID" not in got_atlas  # atlas: non-Google only


def test_browser_dbsc_off_reads_the_running_browser_command_line(monkeypatch):
    class Proc:
        def __init__(self, out):
            self.returncode, self.stdout = 0, out
    monkeypatch.setattr(sandbox, "_run", lambda argv, **k: Proc(
        b"/usr/bin/chromium --user-data-dir=/home/agent/chrome-profile "
        + FLAG.encode() + b"\n"))
    assert login_vault.browser_dbsc_off("atlas") is True
    monkeypatch.setattr(sandbox, "_run", lambda argv, **k: Proc(
        b"/usr/bin/chromium --user-data-dir=/home/agent/chrome-profile\n"))
    assert login_vault.browser_dbsc_off("atlas") is False


def test_an_old_browser_with_no_google_yet_is_not_handed_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(login_vault, "VAULT_PATH", tmp_path / "v" / "cookies.json")
    monkeypatch.setattr(login_vault, "LS_PATH", tmp_path / "v" / "ls.json")
    monkeypatch.setattr(login_vault, "enabled", lambda: True)
    monkeypatch.setattr(login_vault, "exclude_domains", lambda: ())
    monkeypatch.setattr(login_vault, "desk_dbsc_off", lambda: True)
    fresh, old = _Jar([_google("SID", "fresh-sid")]), _Jar([])
    drivers = {"fresh": fresh, "old": old}
    monkeypatch.setattr(login_vault, "running_desks", lambda: sorted(drivers))
    monkeypatch.setattr(login_vault, "driver_for", lambda d: drivers[d])
    monkeypatch.setattr(login_vault, "browser_dbsc_off", lambda d: d == "fresh")
    login_vault.sync_running_desks()
    assert not any(c["name"] == "SID" for batch in old.sets for c in batch)


def test_googles_per_request_sidcc_does_not_fan_out_every_tick(tmp_path, monkeypatch):
    """MEASURED on the box: SIDCC and the __Secure-*PSIDCC pair change on every
    Google request (new value, new one-year expiry), so syncing them fanned out
    three cookies to every desk every 30 s. They are not a login: Google
    re-derives them from SID. A desk whose only change is SIDCC is quiet."""
    monkeypatch.setattr(login_vault, "VAULT_PATH", tmp_path / "v" / "cookies.json")
    monkeypatch.setattr(login_vault, "LS_PATH", tmp_path / "v" / "ls.json")
    monkeypatch.setattr(login_vault, "enabled", lambda: True)
    monkeypatch.setattr(login_vault, "exclude_domains", lambda: ())
    monkeypatch.setattr(login_vault, "desk_dbsc_off", lambda: True)
    monkeypatch.setattr(login_vault, "browser_dbsc_off", lambda d: True)
    a = _Jar([_google(n, "rotated-now") for n in
              ("SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC")])
    b = _Jar([])
    drivers = {"a": a, "b": b}
    monkeypatch.setattr(login_vault, "running_desks", lambda: sorted(drivers))
    monkeypatch.setattr(login_vault, "driver_for", lambda d: drivers[d])
    assert login_vault.sync_running_desks() == {"synced": False, "reason": "no_change"}
    assert b.sets == []
