"""Tests for the OpenCode Go plan-usage reader."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from server.sources import opencode_usage


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        pass


def _write_key(path: Path, key: str) -> None:
    path.write_text(json.dumps({"opencode": {"type": "api", "key": key}}))


@pytest.fixture
def temp_auth(tmp_path: Path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    monkeypatch.setattr(opencode_usage, "OPENCODE_AUTH_JSON", auth_file)
    return auth_file


@pytest.fixture
def temp_cache(tmp_path: Path, monkeypatch):
    cache_file = tmp_path / "opencode-go-usage-cache.json"
    monkeypatch.setattr(opencode_usage, "CACHE_FILE", cache_file)
    return cache_file


@pytest.fixture
def reader_factory(temp_auth, temp_cache):
    def _factory():
        return opencode_usage.OpencodeGoUsageReader()

    return _factory


VERIFIED_PAYLOAD = {
    "usage": {
        "rolling": {
            "status": "ok",
            "percent": 12,
            "resetsAt": "2026-08-13T21:34:34.008Z",
        },
        "weekly": {
            "status": "ok",
            "percent": 34,
            "resetsAt": "2026-08-13T21:35:34.008Z",
        },
        "monthly": {
            "status": "ok",
            "percent": 56,
            "resetsAt": "2026-08-13T21:36:34.008Z",
        },
    }
}


EXPECTED_WINDOWS = [
    {
        "key": "opencode_rolling",
        "label": "go 5-hour",
        "percent": 12.0,
        "severity": "normal",
        "resets_at": "2026-08-13T21:34:34.008Z",
    },
    {
        "key": "opencode_weekly",
        "label": "go weekly",
        "percent": 34.0,
        "severity": "normal",
        "resets_at": "2026-08-13T21:35:34.008Z",
    },
    {
        "key": "opencode_monthly",
        "label": "go monthly",
        "percent": 56.0,
        "severity": "normal",
        "resets_at": "2026-08-13T21:36:34.008Z",
    },
]


def test_parses_verified_shape(reader_factory, temp_auth, monkeypatch):
    _write_key(temp_auth, "sk-test")

    calls = []

    def fake_urlopen(req: urllib.request.Request, **_kwargs):
        calls.append(req)
        assert req.get_header("Authorization") == "Bearer sk-test"
        assert req.get_header("User-agent") == opencode_usage.USER_AGENT
        assert req.get_header("Accept") == "application/json"
        return FakeResponse(200, json.dumps(VERIFIED_PAYLOAD))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    reader = reader_factory()
    result = reader.poll()

    assert result["available"] is True
    assert result["stale"] is False
    assert result["windows"] == EXPECTED_WINDOWS
    assert "fetched_at" in result
    assert len(calls) == 1


def test_first_poll_runs_even_when_the_machine_just_booted(
    reader_factory, temp_auth, monkeypatch
):
    _write_key(temp_auth, "sk-test")
    monkeypatch.setattr(opencode_usage.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(200, json.dumps(VERIFIED_PAYLOAD)),
    )

    assert reader_factory().poll()["windows"] == EXPECTED_WINDOWS


def test_degrades_when_auth_missing(reader_factory, temp_auth):
    assert not temp_auth.exists()

    reader = reader_factory()
    result = reader.poll()

    assert result["available"] is False
    assert result["stale"] is False
    assert result["reason"] == "no opencode key"
    assert result["windows"] == []


def test_refresh_cadence_returns_cache(reader_factory, temp_auth, monkeypatch):
    _write_key(temp_auth, "sk-test")
    call_count = 0

    def fake_urlopen(req: urllib.request.Request, **_kwargs):
        nonlocal call_count
        call_count += 1
        return FakeResponse(200, json.dumps(VERIFIED_PAYLOAD))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    reader = reader_factory()
    first = reader.poll()
    second = reader.poll()

    assert first == second
    assert first["available"] is True
    assert first["stale"] is False
    assert call_count == 1


def test_caches_reading_and_degrades_on_error(
    reader_factory, temp_auth, temp_cache, monkeypatch
):
    _write_key(temp_auth, "sk-test")

    def success_urlopen(req: urllib.request.Request, **_kwargs):
        return FakeResponse(200, json.dumps(VERIFIED_PAYLOAD))

    monkeypatch.setattr(urllib.request, "urlopen", success_urlopen)

    reader = reader_factory()
    first = reader.poll()
    assert first["available"] is True
    assert first["stale"] is False
    assert first["windows"] == EXPECTED_WINDOWS
    assert temp_cache.exists()

    def fail_urlopen(req: urllib.request.Request, **_kwargs):
        raise urllib.error.HTTPError(req.full_url, 500, "internal error", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    reader._fetched_at = float("-inf")
    second = reader.poll()

    assert second["available"] is True
    assert second["stale"] is True
    assert second["reason"] == "http 500"
    assert second["windows"] == EXPECTED_WINDOWS


def test_caches_persists_across_restart(reader_factory, temp_auth, temp_cache, monkeypatch):
    _write_key(temp_auth, "sk-test")

    def success_urlopen(req: urllib.request.Request, **_kwargs):
        return FakeResponse(200, json.dumps(VERIFIED_PAYLOAD))

    monkeypatch.setattr(urllib.request, "urlopen", success_urlopen)

    reader = reader_factory()
    first = reader.poll()
    assert first["windows"] == EXPECTED_WINDOWS

    # Simulate a restart: a brand-new reader with no working network should
    # still surface the cached windows as stale.
    def fail_urlopen(req: urllib.request.Request, **_kwargs):
        raise urllib.error.HTTPError(req.full_url, 503, "unavailable", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    new_reader = reader_factory()
    result = new_reader.poll()

    assert result["available"] is True
    assert result["stale"] is True
    assert result["windows"] == EXPECTED_WINDOWS


def test_401_refreshes_key_when_key_changes(reader_factory, temp_auth, monkeypatch):
    _write_key(temp_auth, "sk-old")

    calls = []

    def fake_urlopen(req: urllib.request.Request, **_kwargs):
        calls.append(req.get_header("Authorization"))
        if req.get_header("Authorization") == "Bearer sk-old":
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)
        if req.get_header("Authorization") == "Bearer sk-new":
            return FakeResponse(200, json.dumps(VERIFIED_PAYLOAD))
        raise AssertionError(f"unexpected authorization: {req.get_header('Authorization')}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    reader = reader_factory()
    first = reader.poll()
    assert first["available"] is False
    assert first["reason"] == "token expired"

    _write_key(temp_auth, "sk-new")
    reader._fetched_at = float("-inf")
    second = reader.poll()
    assert second["available"] is True
    assert second["windows"] == EXPECTED_WINDOWS
    assert calls == ["Bearer sk-old", "Bearer sk-new"]


def test_rate_limit_backoff(reader_factory, temp_auth, monkeypatch):
    _write_key(temp_auth, "sk-test")

    call_count = 0

    def fake_urlopen(req: urllib.request.Request, **_kwargs):
        nonlocal call_count
        call_count += 1
        raise urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    reader = reader_factory()
    first = reader.poll()
    assert first["available"] is False
    assert first["reason"] == "rate limited"
    assert reader._backoff > 0

    # A second poll within the backoff window should not hit the network.
    second = reader.poll()
    assert second == first
    assert call_count == 1
