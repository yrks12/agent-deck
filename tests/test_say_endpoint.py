"""Talking to the manager from the browser.

Every refusal must come back as a 409 with a stable slug the page can show. A
500 here would read as "the deck is broken" when the real answer is "your
manager went away".
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import manager


@pytest.fixture
def client(short_tmp, monkeypatch):
    monkeypatch.setattr(manager, "MANAGER_FILE", short_tmp / "manager.json")
    return TestClient(app_mod.app)


def test_say_refuses_when_nobody_is_crowned(client):
    res = client.post("/api/manager/say", json={"text": "merge"})
    assert res.status_code == 409
    assert res.json()["reason"] == "no_manager"


def test_say_refuses_empty_text(client, fake_socket, monkeypatch):
    sock_dir, pid, _ = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    manager.crown("sid-a", pid, "mgr")
    res = client.post("/api/manager/say", json={"text": "   "})
    assert res.status_code == 409
    assert res.json()["reason"] == "empty"


def test_say_refuses_oversized_text(client, fake_socket, monkeypatch):
    sock_dir, pid, _ = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    manager.crown("sid-a", pid, "mgr")
    res = client.post("/api/manager/say", json={"text": "x" * 2001})
    assert res.status_code == 409
    assert res.json()["reason"] == "too_long"


def test_say_injects_into_the_crowned_pid(client, fake_socket, monkeypatch):
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    manager.crown("sid-a", pid, "mgr")

    res = client.post("/api/manager/say", json={"text": "merge"})
    assert res.status_code == 200
    assert res.json()["ok"] is True

    for _ in range(60):
        if received:
            break
        time.sleep(0.05)
    assert received, "nothing arrived on the manager's socket"
    assert json.loads(received[0])["message"]["content"] == "merge"


def test_say_reports_a_dead_socket_rather_than_500(client, short_tmp, monkeypatch):
    empty = short_tmp / "cc-socks"
    empty.mkdir()
    monkeypatch.setattr(manager, "sock_dir", lambda: empty)
    manager.crown("sid-a", 999999, "mgr")
    res = client.post("/api/manager/say", json={"text": "merge"})
    assert res.status_code == 409
    assert res.json()["reason"] == "no_socket"


def test_say_never_writes_to_a_session_that_is_not_the_manager(client, fake_socket, monkeypatch):
    """The blast radius: one pid, the crowned one, and no other."""
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    manager.crown("sid-a", pid, "mgr")

    # A caller asking for a different pid must not be honoured -- the endpoint
    # takes no pid at all, it reads the crown.
    res = client.post("/api/manager/say", json={"text": "merge", "pid": 424243})
    assert res.status_code == 200
    for _ in range(60):
        if received:
            break
        time.sleep(0.05)
    assert res.json()["pid"] == pid


def test_say_arms_the_speaker_for_the_manager_only(client, fake_socket, monkeypatch):
    sock_dir, pid, _ = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    manager.crown("sid-a", pid, "mgr")

    armed = []
    monkeypatch.setattr(
        app_mod.collector.speaker, "arm",
        lambda session_id, **kw: armed.append((session_id, kw)),
    )
    client.post("/api/manager/say", json={"text": "merge"})
    assert armed and armed[0][0] == "sid-a"
    assert armed[0][1].get("muted") is False


def test_say_respects_the_mute_toggle(client, fake_socket, monkeypatch):
    sock_dir, pid, _ = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    manager.crown("sid-a", pid, "mgr")

    armed = []
    monkeypatch.setattr(
        app_mod.collector.speaker, "arm",
        lambda session_id, **kw: armed.append(kw),
    )
    client.post("/api/manager/say", json={"text": "merge", "mute": True})
    assert armed and armed[0].get("muted") is True
