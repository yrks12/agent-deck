"""Cadence readers must perform their first poll immediately after boot."""

from types import SimpleNamespace

from server.sources import sessions, usage


def test_claude_usage_does_not_treat_recent_boot_as_a_fresh_cache(monkeypatch):
    monkeypatch.setattr(usage.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(usage, "_load_disk", lambda: None)
    monkeypatch.setattr(usage, "_read_token", lambda: None)

    assert usage.UsageReader().poll()["reason"] == "no keychain access"


def test_session_scanner_calls_claude_on_its_first_post_boot_scan(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(sessions.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(
        sessions.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs))
        or SimpleNamespace(returncode=0, stdout="[]"),
    )

    sessions.SessionScanner().scan()

    assert len(calls) == 1
