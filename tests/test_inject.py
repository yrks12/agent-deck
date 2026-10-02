import json
import time

import pytest

from server import manager


def test_frame_is_exactly_what_the_cli_documents():
    assert manager.frame("hello") == (
        b'{"type":"user","message":{"role":"user","content":"hello"}}\n'
    )


def test_frame_is_one_line_even_for_multiline_text():
    out = manager.frame("line one\nline two")
    assert out.count(b"\n") == 1
    assert json.loads(out)["message"]["content"] == "line one\nline two"


def test_inject_writes_the_frame_to_the_socket(fake_socket):
    sock_dir, pid, received = fake_socket
    result = manager.inject(pid, "merge", sock_dir=sock_dir)
    assert result["ok"] is True
    for _ in range(50):
        if received:
            break
        time.sleep(0.05)
    assert received, "nothing arrived on the socket"
    assert json.loads(received[0])["message"]["content"] == "merge"


def test_inject_refuses_a_dead_socket(tmp_path):
    sock_dir = tmp_path / "cc-socks"
    sock_dir.mkdir()
    with pytest.raises(manager.InjectError) as err:
        manager.inject(999999, "merge", sock_dir=sock_dir)
    assert err.value.reason == "no_socket"


def test_inject_refuses_empty_and_oversized_text(fake_socket):
    sock_dir, pid, _ = fake_socket
    with pytest.raises(manager.InjectError) as empty:
        manager.inject(pid, "   ", sock_dir=sock_dir)
    assert empty.value.reason == "empty"
    with pytest.raises(manager.InjectError) as big:
        manager.inject(pid, "x" * 2001, sock_dir=sock_dir)
    assert big.value.reason == "too_long"


def test_socket_path_never_escapes_the_socket_dir(tmp_path):
    sock_dir = tmp_path / "cc-socks"
    sock_dir.mkdir()
    (tmp_path / "elsewhere.sock").write_text("")
    with pytest.raises(manager.InjectError) as err:
        manager.inject("../elsewhere", "merge", sock_dir=sock_dir)
    assert err.value.reason == "bad_pid"


def _wire_bytes(received) -> bytes:
    """Everything the fake socket saw, once it has seen anything."""
    for _ in range(50):
        if received:
            break
        time.sleep(0.05)
    assert received, "nothing arrived on the socket"
    return b"".join(received)


def test_auth_line_precedes_the_user_frame(fake_socket):
    """Order is the contract: the CLI honours an auth frame only as the FIRST
    line of the connection. Asserts the good signal -- both frames present,
    auth first -- not merely that nothing errored.
    """
    sock_dir, pid, received = fake_socket
    manager.inject(pid, "hello", sock_dir=sock_dir, token="t0k")
    lines = _wire_bytes(received).splitlines()
    assert json.loads(lines[0]) == {"type": "auth", "token": "t0k"}
    assert json.loads(lines[1])["type"] == "user"


def test_without_a_token_the_bytes_are_exactly_what_they_are_today(fake_socket):
    """No token discoverable -> behaviour must not change at all."""
    sock_dir, pid, received = fake_socket
    manager.inject(pid, "hello", sock_dir=sock_dir)
    assert _wire_bytes(received) == manager.frame("hello")
