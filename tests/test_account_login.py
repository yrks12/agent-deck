"""S7c: sign a Claude account in from the APP -- the CLI owns the login.

The owner signs in from his phone or Mac, not with deckctl on the box. The deck
starts the CLI's OWN `claude auth login` in a PTY with that account's
CLAUDE_CONFIG_DIR, hands back the authorize URL the CLI prints, and pipes the
code he pastes straight into the CLI. It proves the result with
`claude auth status --json` (loggedIn, in the right configDirectory) and only
then registers the account. Agent Deck never stores a token or the code, and
never logs the code.

MEASURED on the box (claude 2.1.286, probe dir): the CLI prints
"Opening browser to sign in…", "If the browser didn't open, visit: <OSC-8 link>"
and "Paste code here if prompted >".

A fake CLI stands in: it prints that screen, takes one line, and on the good
code writes `.credentials.json` where the real one would.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

from server import account_login, accounts

GOOD = "GOODCODE-7f3a#state-9"
URL = "https://claude.ai/oauth/authorize?code=true&client_id=x&state=abc"

FAKE = r'''#!{python}
import json, os, sys, time
home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.environ["HOME"], ".claude")
if sys.argv[1:3] == ["auth", "status"]:
    ok = os.path.exists(os.path.join(home, ".credentials.json"))
    print(json.dumps({{"loggedIn": ok, "configDirectory": home}}))
    sys.exit(0 if ok else 1)
if sys.argv[1:3] == ["auth", "login"]:
    if os.environ.get("FAKE_SILENT"):
        time.sleep(30)
    sys.stdout.write("Opening browser to sign in…\r\n")
    sys.stdout.write("If the browser didn't open, visit: \x1b]8;;{url}\x07{url}\x1b]8;;\x07\r\n")
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    line = sys.stdin.readline().strip()
    sys.stdout.write(line + "\r\n")  # a terminal echoes what was typed
    if line == "{good}":
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, ".credentials.json"), "w") as fh:
            fh.write(json.dumps({{"claudeAiOauth": {{"accessToken": "NEW-SECRET"}}}}))
        print("Login successful.")
        sys.exit(0)
    print("OAuth error: invalid code")
    sys.exit(1)
sys.exit(2)
'''


@pytest.fixture
def rig(tmp_path, monkeypatch, short_tmp):
    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE.format(python=sys.executable, url=URL, good=GOOD))
    fake.chmod(0o755)
    home = short_tmp / "home"
    (home / ".claude" / "agent-bus").mkdir(parents=True)
    reg = home / ".claude" / "agent-bus" / "accounts.json"
    monkeypatch.setattr(accounts, "REGISTRY", reg)
    now = {"t": 1000.0}
    mgr = account_login.LoginManager(
        claude_bin=str(fake), home=home, main_dir=home / ".claude",
        env={"PATH": os.environ["PATH"], "HOME": str(home),
             "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-main-must-not-leak"},
        clock=lambda: now["t"], url_wait=10.0, exit_wait=10.0)
    yield {"mgr": mgr, "home": home, "reg": reg, "now": now}
    mgr.shutdown()


def test_start_returns_the_url_the_cli_printed(rig):
    got = rig["mgr"].start("work", "Work")
    assert got["url"] == URL
    assert got["status"] == "waiting_code" and got["account"] == "work"
    work = rig["home"] / ".claude-accounts" / "work"
    assert work.is_dir() and (work / "projects").is_symlink()


def test_the_code_goes_to_the_cli_and_the_account_is_registered(rig):
    login = rig["mgr"].start("work", "Work")
    got = rig["mgr"].submit(login["login_id"], GOOD)
    assert got["status"] == "done", got
    rows = json.loads(rig["reg"].read_text())
    assert [(r["id"], r["label"], r["kind"]) for r in rows] == [("work", "Work", "subscription")]
    assert "NEW-SECRET" not in rig["reg"].read_text()
    assert rig["mgr"].get(login["login_id"])["status"] == "done"


def test_a_wrong_code_is_login_failed_and_registers_nothing(rig):
    """The app maps `login_failed` (its contract, PR #211): the CLI has exited,
    so the way on is a new sign-in, not another code."""
    login = rig["mgr"].start("work", "Work")
    with pytest.raises(account_login.LoginError) as err:
        rig["mgr"].submit(login["login_id"], "WRONG-CODE")
    assert err.value.reason == "login_failed"
    assert rig["mgr"].get(login["login_id"])["status"] == "failed"
    assert not rig["reg"].exists()


def test_signing_in_again_to_an_existing_account_is_accepted(rig):
    first = rig["mgr"].start("work", "Work")
    rig["mgr"].submit(first["login_id"], GOOD)
    again = rig["mgr"].start("work", "Work")
    assert again["status"] == "waiting_code"
    assert rig["mgr"].submit(again["login_id"], GOOD)["status"] == "done"
    rows = json.loads(rig["reg"].read_text())
    assert [r["id"] for r in rows] == ["work"]


def test_the_code_never_appears_in_state_or_logs(rig, caplog):
    caplog.set_level(logging.DEBUG)
    login = rig["mgr"].start("work", "Work")
    got = rig["mgr"].submit(login["login_id"], GOOD)
    seen = json.dumps(got) + json.dumps(rig["mgr"].get(login["login_id"])) + caplog.text
    seen += repr(rig["mgr"]._logins[login["login_id"]].__dict__)
    assert GOOD not in seen and "GOODCODE" not in seen


def test_main_relogs_in_its_own_dir_and_is_not_registered(rig):
    login = rig["mgr"].start("main", "")
    got = rig["mgr"].submit(login["login_id"], GOOD)
    assert got["status"] == "done"
    assert (rig["home"] / ".claude" / ".credentials.json").exists()
    assert not rig["reg"].exists()


def test_one_login_at_a_time_per_account(rig):
    rig["mgr"].start("work", "Work")
    with pytest.raises(account_login.LoginError) as err:
        rig["mgr"].start("work", "Work")
    assert (err.value.status, err.value.reason) == (409, "login_in_progress")
    rig["mgr"].start("other", "Other")  # another account is fine


def test_after_ten_minutes_it_expires_and_the_cli_is_killed(rig):
    login = rig["mgr"].start("work", "Work")
    proc = rig["mgr"]._logins[login["login_id"]].proc
    rig["now"]["t"] += account_login.EXPIRE_SECONDS + 1
    assert rig["mgr"].get(login["login_id"])["status"] == "expired"
    deadline = time.time() + 5
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert proc.poll() is not None, "the PTY's CLI is still running"
    with pytest.raises(account_login.LoginError) as err:
        rig["mgr"].submit(login["login_id"], GOOD)
    assert err.value.reason == "expired"
    rig["mgr"].start("work", "Work")  # the slot is free again


def test_the_cli_never_sees_mains_token_or_the_wrong_dir(rig, tmp_path):
    env = rig["mgr"]._env_for("work")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"] == str(rig["home"] / ".claude-accounts" / "work")
    assert "CLAUDE_CONFIG_DIR" not in rig["mgr"]._env_for("main")


@pytest.mark.parametrize("bad", ["../x", "Work", "", "a" * 40])
def test_a_bad_id_is_refused(rig, bad):
    with pytest.raises(account_login.LoginError) as err:
        rig["mgr"].start(bad, "x")
    assert (err.value.status, err.value.reason) == (400, "bad_account")


@pytest.mark.parametrize("bad", ["", "code\nsecond line", "a\x03b", "x" * 600])
def test_a_code_that_could_drive_the_terminal_is_refused(rig, bad):
    login = rig["mgr"].start("work", "Work")
    with pytest.raises(account_login.LoginError) as err:
        rig["mgr"].submit(login["login_id"], bad)
    assert (err.value.status, err.value.reason) == (400, "bad_code")
    assert rig["mgr"].get(login["login_id"])["status"] == "waiting_code"


def test_unknown_login(rig):
    with pytest.raises(account_login.LoginError) as err:
        rig["mgr"].get("nope")
    assert (err.value.status, err.value.reason) == (404, "unknown_login")


def test_a_cli_that_prints_no_url_fails_and_is_killed(rig, monkeypatch):
    rig["mgr"]._env["FAKE_SILENT"] = "1"
    rig["mgr"].url_wait = 0.5
    with pytest.raises(account_login.LoginError) as err:
        rig["mgr"].start("work", "Work")
    assert (err.value.status, err.value.reason) == (502, "login_failed")
    rig["mgr"]._env.pop("FAKE_SILENT")
    rig["mgr"].url_wait = 10.0
    rig["mgr"].start("work", "Work")  # nothing left holding the slot
