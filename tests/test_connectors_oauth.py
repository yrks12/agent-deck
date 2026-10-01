"""OAuth connectors: Connect -> the provider's consent page -> tokens in the vault.

The flow is the MCP authorization spec, run by the deck on the owner's behalf:

1. `POST <server>` unauthenticated -> 401 whose `WWW-Authenticate` names the
   protected-resource metadata (RFC 9728) -> its authorization server ->
   that server's metadata (RFC 8414).
2. Dynamic client registration (RFC 7591) with a LOOPBACK redirect,
   `http://127.0.0.1:47689/callback`. MEASURED 2026-09-30 against ten vendor
   servers: Notion and Zapier refuse a plain-http non-loopback redirect, Linear,
   Vercel and Airtable refuse the deck's own address -- and all ten accept the
   loopback one. The owner's Mac (or iPhone) app catches it and hands the code
   to the deck; pasting the final URL works too.
3. PKCE S256 authorize URL, opened in his browser. He consents; the deck never
   does.
4. Code -> tokens. Access and refresh token are two vault secrets, granted to
   the chosen desks, used only by `connector_run --headers`, refreshed before
   they expire. Never in a response, a log, the ledger or the MCP config.

Fixtures: Notion's real discovery documents and a real registration response
(captured 2026-09-30). The token responses are RFC 6749 section 5.1 shapes --
a real one needs his consent, which is the step this suite must never take.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import urllib.parse

import pytest

from server import deck_mcp, vault
from tests.test_connectors import FX, MS_LEARN, NOTION, Fake, Rig, connectors

SERVER = "https://mcp.notion.com/mcp"
REDIRECT = "http://127.0.0.1:47689/callback"
A1, R1 = "ntn_access_one_0123456789abcdef", "ntn_refresh_one_0123456789abcdef"
A2, R2 = "ntn_access_two_0123456789abcdef", "ntn_refresh_two_0123456789abcdef"
TOKENS = (A1, R1, A2, R2)


class OAuthFake(Fake):
    def __call__(self, url, headers=None, timeout=20.0):
        if url == "https://mcp.notion.com/.well-known/oauth-protected-resource/mcp":
            self.calls.append(url)
            return 200, (FX / "notion_protected_resource.json").read_bytes()
        if url == "https://mcp.notion.com/.well-known/oauth-authorization-server":
            self.calls.append(url)
            return 200, (FX / "notion_authorization_server.json").read_bytes()
        return super().__call__(url, headers, timeout)


class Net:
    """The POST side: the MCP server's 401, registration, the token endpoint."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, bytes]] = []
        self.refresh_ok = True

    def __call__(self, url, data=b"", headers=None, timeout=20.0):
        self.posts.append((url, data))
        if url == SERVER:
            return 401, {"WWW-Authenticate": (
                'Bearer realm="OAuth", resource_metadata="https://mcp.notion.com/'
                '.well-known/oauth-protected-resource/mcp", error="invalid_token"')}, b""
        if url == "https://mcp.notion.com/register":
            return 201, {}, (FX / "notion_register.json").read_bytes()
        if url == "https://mcp.notion.com/token":
            form = urllib.parse.parse_qs(data.decode())
            if form["grant_type"] == ["authorization_code"]:
                return 200, {}, json.dumps({"access_token": A1, "refresh_token": R1,
                                            "token_type": "Bearer",
                                            "expires_in": 3600}).encode()
            if form["grant_type"] == ["refresh_token"] and self.refresh_ok:
                return 200, {}, json.dumps({"access_token": A2, "refresh_token": R2,
                                            "token_type": "Bearer",
                                            "expires_in": 3600}).encode()
            return 400, {}, b'{"error":"invalid_grant"}'
        raise AssertionError(f"unexpected POST {url}")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    r.fetch = OAuthFake()
    r.net = Net()
    r.post = r.net
    r.store = r.make()
    return r


def _connect(rig, desks=("atlas", "globex")):
    out = rig.store.connect(NOTION, list(desks))
    query = urllib.parse.parse_qs(urllib.parse.urlparse(out["authorize_url"]).query)
    return out, {k: v[0] for k, v in query.items()}


def _form(rig, url):
    (data,) = [d for u, d in rig.net.posts if u == url][-1:]
    return {k: v[0] for k, v in urllib.parse.parse_qs(data.decode()).items()}


def _clean(rig, *texts):
    for text in texts:
        for token in TOKENS:
            assert token not in text


# ── Connect: discovery, registration, the consent URL ───────────────────────


def test_connect_builds_a_pkce_consent_url_from_the_servers_own_discovery(rig):
    out, q = _connect(rig)
    assert out["ok"] is True and out["redirect_uri"] == REDIRECT
    assert out["authorize_url"].startswith("https://mcp.notion.com/authorize?")
    assert q["response_type"] == "code"
    assert q["client_id"] == "EGqWn4UKk4Fk8AJz"          # from the registration
    assert q["redirect_uri"] == REDIRECT
    assert q["code_challenge_method"] == "S256"
    assert q["state"] == out["state"] and len(out["state"]) >= 32
    assert q["resource"] == SERVER
    assert q["scope"] == "default"
    reg = json.loads([d for u, d in rig.net.posts if u.endswith("/register")][0])
    assert reg["redirect_uris"] == [REDIRECT]
    assert reg["token_endpoint_auth_method"] == "none"
    # Nothing is installed until he has consented.
    assert rig.item(NOTION)["installed_on"] == []
    assert rig.reloads == []
    pending = rig.root / "oauth_pending.json"
    assert oct(os.stat(pending).st_mode & 0o777) == "0o600"


def test_connect_is_only_for_oauth_connectors(rig):
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.connect(MS_LEARN, ["atlas"])
    assert exc.value.reason == "bad_input"


def test_a_plain_install_of_an_oauth_connector_says_to_press_connect(rig):
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.install(NOTION, ["atlas"])
    assert exc.value.reason == "needs_oauth"
    assert "Connect" in exc.value.detail


# ── the callback ─────────────────────────────────────────────────────────────


def test_his_consent_puts_the_tokens_in_the_vault_and_installs(rig, caplog):
    caplog.set_level(logging.DEBUG)
    out, q = _connect(rig)
    done = rig.store.complete(
        callback_url=f"{REDIRECT}?code=the-code&state={out['state']}")
    form = _form(rig, "https://mcp.notion.com/token")
    assert form["grant_type"] == "authorization_code" and form["code"] == "the-code"
    assert form["redirect_uri"] == REDIRECT and form["client_id"] == "EGqWn4UKk4Fk8AJz"
    assert form["resource"] == SERVER
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == q["code_challenge"]

    assert done["ok"] is True
    assert set(rig.item(NOTION)["installed_on"]) == {"atlas", "globex"}
    assert sorted(d for d, _ in rig.reloads) == ["atlas", "globex"]
    names = sorted(m.name for m in vault.meta(rig.vault_path))
    assert len(names) == 2 and all(n.startswith("CONNECTOR_") for n in names)
    assert set(vault.env_for(rig.vault_path, "atlas").values()) == {A1, R1}
    assert vault.env_for(rig.vault_path, "twin") == {}
    server = next(v for k, v in json.loads(deck_mcp.config("atlas"))["mcpServers"].items()
                  if k not in ("computer", "deck", "mac"))
    assert server["url"] == SERVER and "server.connector_run" in server["headersHelper"]
    _clean(rig, json.dumps(done), json.dumps(rig.store.installed()),
           json.dumps(rig.store.catalog(limit=200)), deck_mcp.config("atlas"),
           caplog.text, *(p.read_text() for p in rig.root.glob("*.json")))


def test_a_state_works_once_and_a_wrong_one_never(rig):
    out, _ = _connect(rig)
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.complete(code="c", state="not-the-state")
    assert exc.value.reason == "bad_state"
    rig.store.complete(code="c", state=out["state"])
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.complete(code="c", state=out["state"])
    assert exc.value.reason == "bad_state"


def test_a_stale_state_is_refused(rig):
    out, _ = _connect(rig)
    rig.now[0] += 16 * 60
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.complete(code="c", state=out["state"])
    assert exc.value.reason == "bad_state"


def test_a_refusal_at_the_provider_installs_nothing(rig):
    out, _ = _connect(rig)
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.complete(callback_url=f"{REDIRECT}?error=access_denied&state={out['state']}")
    assert exc.value.reason == "oauth_denied"
    assert rig.item(NOTION)["installed_on"] == [] and vault.meta(rig.vault_path) == []
    assert rig.store.connect_status(out["state"])["state"] == "failed"


def test_status_follows_a_connect_to_done(rig):
    out, _ = _connect(rig)
    assert rig.store.connect_status(out["state"])["state"] == "pending"
    rig.store.complete(code="c", state=out["state"])
    assert rig.store.connect_status(out["state"])["state"] == "done"


# ── using and refreshing the tokens ──────────────────────────────────────────


def _connected(rig):
    out, _ = _connect(rig)
    rig.store.complete(code="c", state=out["state"])


def test_the_helper_hands_the_desk_a_bearer_header(rig):
    from server import connector_run
    _connected(rig)
    got = connector_run.headers("atlas", NOTION, vault_path=rig.vault_path,
                                post=rig.net, now=rig.now[0] + 60)
    assert got == {"Authorization": f"Bearer {A1}"}
    with pytest.raises(connector_run.LaunchError):
        connector_run.headers("twin", NOTION, vault_path=rig.vault_path,
                              post=rig.net, now=rig.now[0])


def test_an_expiring_token_is_refreshed_and_both_desks_get_the_new_one(rig):
    from server import connector_run
    _connected(rig)
    later = rig.now[0] + 3600 - 60          # inside the five-minute margin
    got = connector_run.headers("atlas", NOTION, vault_path=rig.vault_path,
                                post=rig.net, now=later)
    assert got == {"Authorization": f"Bearer {A2}"}
    form = _form(rig, "https://mcp.notion.com/token")
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == R1
    assert set(vault.env_for(rig.vault_path, "globex").values()) == {A2, R2}
    # No second refresh for the next desk: the new token is good for an hour.
    before = len(rig.net.posts)
    connector_run.headers("globex", NOTION, vault_path=rig.vault_path,
                          post=rig.net, now=later + 1)
    assert len(rig.net.posts) == before


def test_the_deck_refreshes_tokens_before_any_desk_asks(rig):
    _connected(rig)
    rig.now[0] += 3600 - 60
    out = rig.store.refresh_oauth_due()
    assert out == [{"id": NOTION, "ok": True}]
    assert set(vault.env_for(rig.vault_path, "atlas").values()) == {A2, R2}


def test_a_dead_refresh_token_is_reported_not_leaked(rig, caplog):
    caplog.set_level(logging.DEBUG)
    _connected(rig)
    rig.net.refresh_ok = False
    rig.now[0] += 3600 - 60
    out = rig.store.refresh_oauth_due()
    assert out[0]["ok"] is False and out[0]["reason"] == "reconnect"
    _clean(rig, json.dumps(out), caplog.text)


# ── more desks, and taking it away ───────────────────────────────────────────


def test_another_desk_joins_without_a_second_sign_in(rig):
    _connected(rig)
    rig.store.install(NOTION, ["twin"])
    assert "twin" in rig.item(NOTION)["installed_on"]
    assert set(vault.env_for(rig.vault_path, "twin").values()) == {A1, R1}


def test_removing_it_from_the_last_desk_forgets_the_tokens(rig):
    _connected(rig)
    rig.store.uninstall(NOTION, ["atlas"])
    assert len(vault.meta(rig.vault_path)) == 2
    rig.store.uninstall(NOTION, ["globex"])
    assert vault.meta(rig.vault_path) == []
    assert NOTION not in json.loads((rig.root / "oauth.json").read_text()).get("connectors", {})


# ── over HTTP, and from a desk ───────────────────────────────────────────────


def test_connect_and_complete_over_http_never_return_a_token(rig, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server import connectors_api
    monkeypatch.setenv("AGENT_DECK_TOKEN", "store-oauth-token-not-real")
    auth = {"Authorization": "Bearer store-oauth-token-not-real"}
    app = FastAPI()
    app.include_router(connectors_api.build_router(rig.store))
    c = TestClient(app)
    r = c.post("/v1/store/connect", headers=auth, json={"id": NOTION, "desks": ["atlas"]})
    assert r.status_code == 200, r.text
    state = r.json()["state"]
    assert c.post("/v1/store/connect/complete", json={"state": state, "code": "c"}
                  ).status_code == 401           # the bearer, like every /v1 route
    r2 = c.post("/v1/store/connect/complete", headers=auth,
                json={"callback_url": f"{REDIRECT}?code=c&state={state}"})
    assert r2.status_code == 200 and r2.json()["ok"] is True
    r3 = c.get("/v1/store/connect/status", params={"state": state}, headers=auth)
    assert r3.json()["state"] == "done"
    _clean(rig, r.text, r2.text, r3.text)


def test_a_desk_asking_for_an_oauth_connector_is_told_yair_must_connect(rig, monkeypatch):
    monkeypatch.setattr(connectors, "default_store", lambda: rig.store)
    reply = deck_mcp.handle("atlas", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "store_install",
                                                 "arguments": {"id": NOTION}}})
    out = json.loads(reply["result"]["content"][0]["text"])
    assert out["ok"] is False and out["reason"] == "needs_oauth"
    assert "Connect" in out["detail"]
