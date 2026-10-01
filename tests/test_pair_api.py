"""K3 HTTP surface: `/healthz`, `POST /v1/pair`, `GET /v1/version`, and the
bearer gate that accepts the master token OR a device token.

The router is built but not mounted on the daemon yet (slice B2 does that), so
these tests mount it on a bare app exactly the way B2 will: `pair_api.mount`.
"""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import deckconfig, pair_api, ratelimit
from server.pairing import DeviceStore, PairingStore

MASTER = "m" * 64
CADDY = ("127.0.0.1", 40000)  # every public request arrives from Caddy on loopback


class Clock:
    def __init__(self):
        self.now = 1_790_812_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def deck(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DECK_TOKEN", MASTER)
    clock = Clock()
    cfg = deckconfig.load(tmp_path / "absent.toml",
                          env={"DECK_STATE_DIR": str(tmp_path / "state")})
    pairing = PairingStore(cfg.deck.state_dir, clock=clock)
    devices = DeviceStore(cfg.deck.state_dir, clock=clock)
    limiter = ratelimit.RateLimiter(clock=clock)
    app = FastAPI()
    pair_api.mount(app, cfg, pairing=pairing, devices=devices, limiter=limiter,
                   version="0.9.0", min_app="0.9.0")

    class Deck:
        pass

    d = Deck()
    d.app, d.cfg, d.pairing, d.devices, d.limiter, d.clock = (
        app, cfg, pairing, devices, limiter, clock)
    d.client = _anonymous(app)
    return d


def _anonymous(app):
    """conftest gives every TestClient the suite's /api credential; /v1 auth is
    the thing under test here, so each request states its own."""
    client = TestClient(app, client=CADDY)
    client.headers.pop("authorization", None)
    return client


def _from(ip):
    return {"X-Forwarded-For": ip}


def _bearer(token, ip="203.0.113.9"):
    return {"Authorization": f"Bearer {token}", **_from(ip)}


def _refusal(resp, status, reason):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["ok"] is False and body["reason"] == reason
    assert isinstance(body["detail"], str) and body["detail"]


# ── /healthz ────────────────────────────────────────────────────────────────


def test_healthz_says_ok_and_nothing_else(deck):
    resp = deck.client.get("/healthz")
    assert resp.status_code == 200 and resp.json() == {"ok": True}


# ── /v1/pair ────────────────────────────────────────────────────────────────


def test_pair_exchanges_a_code_for_a_device_token(deck, caplog):
    secret, _ = deck.pairing.mint()
    with caplog.at_level(logging.DEBUG):
        resp = deck.client.post("/v1/pair", json={"code": secret, "device": "Dan's MacBook"},
                                headers=_from("203.0.113.9"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"token", "device_id", "deck"}
    assert body["token"].startswith("adt_") and len(body["token"]) == 47
    assert body["device_id"].startswith("d_") and len(body["device_id"]) == 10
    assert body["deck"] == {"name": "Agent Deck", "version": "0.9.0"}
    assert resp.headers["cache-control"] == "no-store"
    assert deck.devices.verify(body["token"]).name == "Dan's MacBook"
    assert body["token"] not in caplog.text and secret not in caplog.text
    # and the token opens /v1
    assert deck.client.get("/v1/version", headers=_bearer(body["token"])).status_code == 200


@pytest.mark.parametrize("payload", [
    None, "not json", [], {"device": "mac"}, {"code": "short", "device": "mac"},
    {"code": "A" * 21 + "+", "device": "mac"}, {"code": 7, "device": "mac"},
    {"code": "A" * 22}, {"code": "A" * 22, "device": ""},
    {"code": "A" * 22, "device": "x" * 61}, {"code": "A" * 22, "device": "a\nb"},
])
def test_a_malformed_request_is_pair_malformed(deck, payload):
    if isinstance(payload, str):
        resp = deck.client.post("/v1/pair", content=payload, headers=_from("203.0.113.9"))
    else:
        resp = deck.client.post("/v1/pair", json=payload, headers=_from("203.0.113.9"))
    _refusal(resp, 400, "pair_malformed")


def test_unknown_used_and_expired_codes(deck):
    ok, _ = deck.pairing.mint()
    stale, _ = deck.pairing.mint()
    post = lambda code, ip: deck.client.post(  # noqa: E731
        "/v1/pair", json={"code": code, "device": "mac"}, headers=_from(ip))
    _refusal(post("Z" * 22, "203.0.113.1"), 401, "pair_unknown")
    assert post(ok, "203.0.113.2").status_code == 200
    _refusal(post(ok, "203.0.113.3"), 409, "pair_used")
    deck.clock.now += 900
    _refusal(post(stale, "203.0.113.4"), 410, "pair_expired")


def test_pair_allows_five_attempts_per_ip(deck):
    for _ in range(5):
        deck.client.post("/v1/pair", json={"code": "Z" * 22, "device": "mac"},
                         headers=_from("203.0.113.9"))
    secret, _ = deck.pairing.mint()
    resp = deck.client.post("/v1/pair", json={"code": secret, "device": "mac"},
                            headers=_from("203.0.113.9"))
    _refusal(resp, 429, "rate_limited")
    assert int(resp.headers["retry-after"]) > 0
    # the code was not spent by the refused attempt, and another IP may use it
    resp = deck.client.post("/v1/pair", json={"code": secret, "device": "mac"},
                            headers=_from("198.51.100.4"))
    assert resp.status_code == 200


# ── the bearer gate ─────────────────────────────────────────────────────────


def test_version_takes_master_or_device_token(deck):
    token, _ = deck.devices.add("mac")
    for presented in (MASTER, token):
        resp = deck.client.get("/v1/version", headers=_bearer(presented))
        assert resp.status_code == 200
        assert resp.json() == {"version": "0.9.0", "min_app": "0.9.0", "api": 1}


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer "}, {"Authorization": "Basic " + MASTER},
    {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer adt_" + "A" * 43},
    {"Authorization": "Bearer " + MASTER + "x"}, {"Authorization": "Bearer ünïcode".encode("utf-8")},
])
def test_anything_else_is_401_as_today(deck, headers):
    resp = deck.client.get("/v1/version", headers=headers)
    _refusal(resp, 401, "unauthorized")
    assert resp.json()["detail"] == "bearer token missing or wrong"
    assert resp.headers["www-authenticate"] == "Bearer"


def test_no_master_token_is_503_as_today(deck, monkeypatch):
    monkeypatch.delenv("AGENT_DECK_TOKEN")
    _refusal(deck.client.get("/v1/version", headers=_bearer(MASTER)), 503,
             "auth_not_configured")


def test_a_revoked_device_is_refused(deck):
    token, device = deck.devices.add("mac")
    deck.devices.revoke(device.id)
    _refusal(deck.client.get("/v1/version", headers=_bearer(token)), 401, "unauthorized")


def test_ten_wrong_bearers_ban_the_ip_from_all_of_v1(deck, caplog):
    wrong = "adt_" + "W" * 43
    with caplog.at_level(logging.INFO, logger=ratelimit.LOGGER_NAME):
        for _ in range(10):
            _refusal(deck.client.get("/v1/version", headers=_bearer(wrong)), 401,
                     "unauthorized")
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["deck-auth-fail ip=203.0.113.9 route=/v1/version reason=bad_token"] * 10
    assert wrong not in caplog.text and MASTER not in caplog.text

    banned = deck.client.get("/v1/version", headers=_bearer(MASTER))
    _refusal(banned, 429, "rate_limited")
    assert int(banned.headers["retry-after"]) == 1800
    secret, _ = deck.pairing.mint()
    _refusal(deck.client.post("/v1/pair", json={"code": secret, "device": "mac"},
                              headers=_from("203.0.113.9")), 429, "rate_limited")
    # nobody else is punished, and /healthz is not /v1
    assert deck.client.get("/v1/version", headers=_bearer(MASTER, "198.51.100.4")).status_code == 200
    assert deck.client.get("/healthz", headers=_from("203.0.113.9")).status_code == 200


def test_a_missing_bearer_is_logged_but_does_not_count_towards_a_ban(deck, caplog):
    with caplog.at_level(logging.INFO, logger=ratelimit.LOGGER_NAME):
        for _ in range(12):
            deck.client.get("/v1/version", headers=_from("203.0.113.9"))
    assert "reason=missing_token" in caplog.text
    assert deck.client.get("/v1/version", headers=_bearer(MASTER)).status_code == 200


def test_pair_failures_are_logged_without_the_code(deck, caplog):
    with caplog.at_level(logging.INFO, logger=ratelimit.LOGGER_NAME):
        deck.client.post("/v1/pair", json={"code": "Q" * 22, "device": "mac"},
                         headers=_from("203.0.113.9"))
    assert caplog.records[-1].getMessage() == (
        "deck-auth-fail ip=203.0.113.9 route=/v1/pair reason=pair_unknown")
    assert "Q" * 22 not in caplog.text


def test_build_router_defaults_to_the_configured_state_dir(tmp_path, monkeypatch):
    """What B2 will call: config in, everything else derived from it."""
    monkeypatch.setenv("AGENT_DECK_TOKEN", MASTER)
    cfg = deckconfig.load(tmp_path / "absent.toml",
                          env={"DECK_STATE_DIR": str(tmp_path / "st")})
    app = FastAPI()
    pair_api.mount(app, cfg)
    secret, _ = PairingStore(tmp_path / "st").mint()
    client = _anonymous(app)
    resp = client.post("/v1/pair", json={"code": secret, "device": "mac"})
    assert resp.status_code == 200
    assert (tmp_path / "st" / "devices.json").exists()
    assert client.get("/v1/version", headers={"Authorization": "Bearer " + MASTER}).json()["api"] == 1
