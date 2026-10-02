"""The deck's phone-reply socket is the OWNER'S, and nothing else may take it.

MEASURED 2026-09-30 on the owner's Mac: `/tmp/cc-socks/agentdeck.sock` was held
by another agent's pytest run, not by the owner's deck. Any test that ran the
app's startup bound the real path, and `Listener.start()` unlinked whatever was
there first -- including a LIVE socket. So every suite run on that machine stole
his WhatsApp reply path, silently, until his deck was restarted.

Three controls, three detectors:
  1. the path comes from `DECK_PHONE_SOCKET` when set, and the suite always sets
     it to a tmp dir (conftest autouse);
  2. a listener never unlinks a socket that a live process is accepting on;
  3. the suite refuses any AF_UNIX bind under a real `cc-socks` directory.
"""
from __future__ import annotations

import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from server import app as app_mod
from server import phone


@pytest.fixture
def short_dir():
    d = Path(tempfile.mkdtemp(dir="/tmp", prefix="dps"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_the_suite_points_the_deck_socket_at_a_tmp_path():
    where = app_mod._deck_socket()
    assert where is not None
    assert "cc-socks" not in Path(where).parts, where
    assert where == os.environ.get("DECK_PHONE_SOCKET"), where


def test_deck_phone_socket_names_the_path(monkeypatch, short_dir):
    target = short_dir / "mine.sock"
    monkeypatch.setenv("DECK_PHONE_SOCKET", str(target))
    assert app_mod._deck_socket() == str(target)


def test_a_live_socket_is_never_taken_over(short_dir):
    path = short_dir / "agentdeck.sock"
    heard = []
    first = phone.Listener(path, on_text=heard.append)
    assert first.start() is True
    try:
        second = phone.Listener(path, on_text=lambda _: None)
        assert second.start() is False, "a second deck unlinked a live socket"
        # The first listener still owns the path and still accepts.
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.connect(str(path))
        probe.close()
    finally:
        first.stop()


def test_a_second_listener_stopping_does_not_remove_the_live_owners_socket(short_dir):
    path = short_dir / "agentdeck.sock"
    first = phone.Listener(path, on_text=lambda _: None)
    assert first.start() is True
    try:
        second = phone.Listener(path, on_text=lambda _: None)
        second.start()
        second.stop()
        assert path.exists(), "the refused listener's stop() unlinked the owner's socket"
    finally:
        first.stop()


def test_the_suite_refuses_a_bind_under_a_real_cc_socks_dir():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError, match="cc-socks"):
            s.bind("/tmp/cc-socks/pytest-must-not-bind.sock")
    finally:
        s.close()
        stray = Path("/tmp/cc-socks/pytest-must-not-bind.sock")
        if stray.is_socket():  # only when the guard is missing: leave no litter
            stray.unlink()
