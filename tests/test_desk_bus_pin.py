"""A desk on a second Claude account sends a file, and the owner SEES it.

Owner screenshot, 2026-10-01: atlas -- moved to the `work` account that
evening -- sent five phone screenshots with `send_file`, and each one reached
the Mac app as a text bubble ending in

    Attached image: ~/.claude-accounts/work/agent-bus/attachments/...

MEASURED on the box: the bytes were in the deck's store (the account dir's
`agent-bus` is a symlink to the primary one) and `/v1/attachments/...`
answered 200. But the desk's MCP runs with `CLAUDE_CONFIG_DIR` set to the
account, and `server.paths` derived `BUS_DIR` from it, ignoring the
`DECK_BUS_DIR` pin every desk is spawned with (`accounts.env_for`). The path
the desk wrote named the account's alias of the store, the server's `url_for`
matched its own spelling only, and `attachments()` handed the app a path
instead of an image.

Two halves, both pinned here:

* every Python process a desk spawns (deck, computer, mac MCPs) resolves the
  bus from `DECK_BUS_DIR` before `CLAUDE_CONFIG_DIR`, as the JS hooks already
  do (`tests/test_hooks_bus_pin.py`) -- and with no symlink at all the file
  still lands where the server serves it;
* a message already written under an alias of the store still draws, because
  `url_for` compares where the path really is, not how it is spelled.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from server import api as api_mod
from server import office, uploads
from tests.test_owner_attachments import AUTH, Rig

REPO = Path(__file__).resolve().parent.parent
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

_SEND = """
import json, sys
from server import deck_mcp, uploads
uploads.make_preview = lambda src, **kw: None
print(json.dumps(deck_mcp.send_file("atlas", sys.argv[1], "Home page, top")))
"""


def _desk_env(primary: Path, account: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("DECK_BUS_DIR", "CLAUDE_CONFIG_DIR")}
    env["CLAUDE_CONFIG_DIR"] = str(account)   # what accounts.env_for sets...
    env["DECK_BUS_DIR"] = str(primary)        # ...and the pin beside it
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


@pytest.mark.parametrize("shared_by_symlink", [True, False],
                         ids=["box-layout", "no-symlink"])
def test_a_desk_on_another_account_sends_a_file_the_server_serves(
        tmp_path, monkeypatch, shared_by_symlink):
    (tmp_path / "bus").mkdir()
    rig = Rig(tmp_path / "bus", monkeypatch)
    primary = rig.dir
    work = tmp_path / "repo"
    work.mkdir()
    (primary / "roster.json").write_text(json.dumps({"version": 1, "agents": [
        {"name": "atlas", "cwd": str(work), "engine": "claude",
         "mission": "run", "reports_to": None}]}))
    account = tmp_path / "accounts" / "work"
    account.mkdir(parents=True)
    if shared_by_symlink:
        (account / "agent-bus").symlink_to(primary)
    src = work / "home-top.png"
    src.write_bytes(PNG)

    done = subprocess.run([sys.executable, "-c", _SEND, str(src)], cwd=REPO,
                          env=_desk_env(primary, account),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    sent = json.loads(done.stdout.strip().splitlines()[-1])

    (record,) = [json.loads(line) for line in
                 (primary / "messages.jsonl").read_text().splitlines() if line]
    [entry] = api_mod.attachments(record["text"])
    assert entry["kind"] == "image"
    assert entry.get("url") == sent["url"], entry
    got = rig.client.get(entry["url"], headers=AUTH)
    assert got.status_code == 200 and got.content == PNG


def test_a_message_written_under_an_alias_of_the_store_still_draws(
        tmp_path, monkeypatch):
    """The five screenshots already sent: their text names the account's
    symlinked spelling of the store. They draw without rewriting the log."""
    primary = tmp_path / "primary"
    store = primary / "attachments" / "att_525a85954e7244c3"
    store.mkdir(parents=True)
    (store / "home-top.png").write_bytes(PNG)
    alias = tmp_path / "accounts" / "work" / "agent-bus"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(primary)
    monkeypatch.setattr(office, "BUS_DIR", primary)

    text = ("Home page, top (phone)\n\nAttached image: "
            f"{alias}/attachments/att_525a85954e7244c3/home-top.png")
    [entry] = api_mod.attachments(text)
    assert entry["url"] == "/v1/attachments/att_525a85954e7244c3/home-top.png"
    assert entry["media"] == "image" and entry["bytes"] == len(PNG)


def test_a_path_that_only_looks_like_the_store_is_not_held(tmp_path,
                                                           monkeypatch):
    """Resolving must not widen what counts as held: a look-alike folder
    elsewhere is still a plain path."""
    primary = tmp_path / "primary"
    (primary / "attachments").mkdir(parents=True)
    monkeypatch.setattr(office, "BUS_DIR", primary)
    other = tmp_path / "elsewhere" / "attachments" / "att_525a85954e7244c3"
    other.mkdir(parents=True)
    (other / "x.png").write_bytes(PNG)
    assert uploads.url_for(str(other / "x.png")) is None
    assert uploads.url_for(str(primary / "attachments" / ".." / ".." /
                               "elsewhere" / "attachments" /
                               "att_525a85954e7244c3" / "x.png")) is None


def test_an_alias_the_store_does_not_hold_is_never_looked_up(tmp_path,
                                                             monkeypatch):
    """`attachments()` runs on every path in every bubble. One under
    `/attachments/att_...` the store does not hold must cost no lookup of the
    path itself: on a Mac `/home/...` is an automount, and resolving a box
    path there stalls the board's refresh (MEASURED: the stall let a refresh
    thread outlive its test in tests/test_approve_records.py)."""
    primary = tmp_path / "primary"
    (primary / "attachments").mkdir(parents=True)
    monkeypatch.setattr(office, "BUS_DIR", primary)

    def no_lookup(*a, **kw):
        raise AssertionError("resolved a path the store does not hold")

    monkeypatch.setattr(os.path, "realpath", no_lookup)
    monkeypatch.setattr(os.path, "samefile", no_lookup)
    box = ("/home/someone/.claude-accounts/work/agent-bus/attachments/"
           "att_6972121de8ef4471/site-overview.jpg")
    assert uploads.url_for(box) is None
