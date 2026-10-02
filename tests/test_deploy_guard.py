"""The box notices when its code was changed outside `bin/deploy-box`.

MEASURED 2026-09-30: "another session overwrote the box with older code about a
minute after my deploy". Agents rsynced checkouts to /opt/agent-deck by hand,
and nothing on the box could tell. `bin/deploy-box` records what it deployed in
/opt/agent-deck/DEPLOYED (sha, time, who, and a fingerprint of the shipped
tree); `server/deploy_guard.py` compares the tree on disk with that record, and
the deck's startup and deckdoctor say so loudly when they differ.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from server import deploy_guard

ROOT = Path(__file__).resolve().parents[1]


def _tree(root: Path, text: str = "print('v1')\n") -> Path:
    (root / "server").mkdir(parents=True)
    (root / "server" / "app.py").write_text(text)
    (root / "hooks").mkdir()
    (root / "hooks" / "cc-bus.js").write_text("// hook\n")
    (root / "bin").mkdir()
    (root / "bin" / "deck").write_text("#!/bin/sh\n")
    return root


def _record(root: Path, sha: str = "a" * 40) -> None:
    deploy_guard.write_deployed(root, sha=sha, who="tester@mac", when="2026-09-30T23:00:00Z",
                                fingerprint=deploy_guard.fingerprint(root))


def test_fingerprint_ignores_bytecode_and_changes_with_any_shipped_file(tmp_path):
    root = _tree(tmp_path / "box")
    before = deploy_guard.fingerprint(root)
    (root / "server" / "__pycache__").mkdir()
    (root / "server" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\0")
    assert deploy_guard.fingerprint(root) == before
    (root / "hooks" / "cc-bus.js").write_text("// older hook\n")
    assert deploy_guard.fingerprint(root) != before


def test_fingerprint_sees_a_file_that_a_rollback_left_behind_or_removed(tmp_path):
    root = _tree(tmp_path / "box")
    before = deploy_guard.fingerprint(root)
    (root / "server" / "new_module.py").write_text("x = 1\n")
    assert deploy_guard.fingerprint(root) != before


def test_no_record_is_not_an_alarm(tmp_path):
    ok, detail = deploy_guard.check(_tree(tmp_path / "box"))
    assert ok is True and "DEPLOYED" in detail


def test_an_unchanged_tree_matches_its_record(tmp_path):
    root = _tree(tmp_path / "box")
    _record(root)
    ok, detail = deploy_guard.check(root)
    assert ok is True, detail


def test_a_hand_rsync_after_a_deploy_is_named_loudly(tmp_path):
    root = _tree(tmp_path / "box")
    _record(root, sha="b" * 40)
    (root / "server" / "app.py").write_text("print('older')\n")  # someone rsynced by hand
    ok, detail = deploy_guard.check(root)
    assert ok is False
    assert "bbbbbbbbbbbb" in detail and "tester@mac" in detail
    assert "bin/deploy-box" in detail and "rollback" in detail.lower()


def test_deployed_round_trips(tmp_path):
    root = _tree(tmp_path / "box")
    _record(root, sha="c" * 40)
    rec = deploy_guard.read_deployed(root)
    assert rec["sha"] == "c" * 40 and rec["who"] == "tester@mac"
    assert rec["fingerprint"] == deploy_guard.fingerprint(root)


def test_the_cli_prints_the_fingerprint_and_the_verdict(tmp_path):
    root = _tree(tmp_path / "box")
    out = subprocess.run([sys.executable, "-m", "server.deploy_guard", "fingerprint", str(root)],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == deploy_guard.fingerprint(root)
    _record(root)
    (root / "bin" / "deck").write_text("#!/bin/sh\n# changed\n")
    out = subprocess.run([sys.executable, "-m", "server.deploy_guard", "check", str(root)],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 1 and "bin/deploy-box" in out.stdout


def test_the_deck_says_it_at_startup():
    text = (ROOT / "server" / "app.py").read_text()
    assert "deploy_guard" in text and "DEPLOY GUARD" in text
