"""The MCP authorization flow for a remote connector, as the deck runs it.

Spec: MCP authorization (OAuth 2.1): protected-resource metadata (RFC 9728) ->
authorization-server metadata (RFC 8414) -> dynamic client registration
(RFC 7591) -> authorization code with PKCE S256 and a `resource` indicator
(RFC 8707) -> token and refresh (RFC 6749).

THE REDIRECT IS LOOPBACK. MEASURED 2026-09-30 against ten vendor MCP servers
(Notion, Linear, Vercel, Atlassian, Stripe, Zapier, Webflow, Wix, Airtable,
GitLab): Notion and Zapier refuse any plain-http redirect that is not
loopback, Linear, Vercel and Airtable refuse the deck's own address -- and
all ten registered `http://127.0.0.1:47689/callback`. The owner's app listens
there while he signs in and hands the code to the deck (or he pastes the URL).

Every network edge is a parameter: `get(url) -> (status, body)` and
`post(url, data, headers) -> (status, headers, body)`. Nothing here logs, and
an error never carries a token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import urllib.parse

REDIRECT_URI = "http://127.0.0.1:47689/callback"
CLIENT_NAME = "Agent Deck"


class OAuthError(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _json(body: bytes) -> dict:
    try:
        value = json.loads(body or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _header(headers: dict, name: str) -> str:
    for key, value in (headers or {}).items():
        if key.lower() == name.lower():
            return str(value)
    return ""


INITIALIZE = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "agent-deck-store", "version": "1"}}}).encode()


def discover(server_url: str, *, get, post) -> dict:
    """Where to send him, where to trade the code, where to register."""
    status, headers, _ = post(server_url, INITIALIZE, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    if status not in (401, 403):
        raise OAuthError("not_oauth", "This server did not ask for a sign-in.")
    url = urllib.parse.urlparse(server_url)
    origin = f"{url.scheme}://{url.netloc}"
    found = re.search(r'resource_metadata="([^"]+)"',
                      _header(headers, "WWW-Authenticate"))
    candidates = [found.group(1)] if found else []
    candidates += [f"{origin}/.well-known/oauth-protected-resource{url.path.rstrip('/')}",
                   f"{origin}/.well-known/oauth-protected-resource"]
    resource_meta: dict = {}
    for cand in dict.fromkeys(candidates):
        code, body = get(cand)
        if code == 200 and _json(body):
            resource_meta = _json(body)
            break
    auth_server = (resource_meta.get("authorization_servers") or [origin])[0]
    a = urllib.parse.urlparse(auth_server)
    a_origin, a_path = f"{a.scheme}://{a.netloc}", a.path.rstrip("/")
    meta: dict = {}
    for cand in dict.fromkeys([
            f"{a_origin}/.well-known/oauth-authorization-server{a_path}",
            f"{a_origin}/.well-known/oauth-authorization-server",
            f"{a_origin}/.well-known/openid-configuration{a_path}",
            f"{a_origin}/.well-known/openid-configuration"]):
        code, body = get(cand)
        if code == 200 and _json(body).get("authorization_endpoint"):
            meta = _json(body)
            break
    if not meta.get("authorization_endpoint") or not meta.get("token_endpoint"):
        raise OAuthError("oauth_unsupported", "The provider publishes no sign-in endpoints.")
    return {
        "resource": resource_meta.get("resource") or server_url,
        "auth_server": auth_server,
        "authorization_endpoint": meta["authorization_endpoint"],
        "token_endpoint": meta["token_endpoint"],
        "registration_endpoint": meta.get("registration_endpoint"),
        "scopes": list(resource_meta.get("scopes_supported") or []),
    }


def register(found: dict, *, post, redirect_uri: str = REDIRECT_URI) -> dict:
    """Dynamic client registration. `{client_id, client_secret?}`."""
    if not found.get("registration_endpoint"):
        raise OAuthError("oauth_unsupported", "The provider does not let new apps "
                         "register themselves, so the deck cannot sign in to it yet.")
    status, _, body = post(found["registration_endpoint"], json.dumps({
        "client_name": CLIENT_NAME, "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"}).encode(),
        {"Content-Type": "application/json", "Accept": "application/json"})
    reply = _json(body)
    if status not in (200, 201) or not reply.get("client_id"):
        raise OAuthError("oauth_register_failed",
                         f"The provider refused to register the deck ({status}).")
    return {"client_id": str(reply["client_id"]),
            "client_secret": reply.get("client_secret") or None}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(found: dict, client_id: str, challenge: str, state: str,
                  redirect_uri: str = REDIRECT_URI) -> str:
    query = {"response_type": "code", "client_id": client_id,
             "redirect_uri": redirect_uri, "code_challenge": challenge,
             "code_challenge_method": "S256", "state": state,
             "resource": found["resource"]}
    if found.get("scopes"):
        query["scope"] = " ".join(found["scopes"])
    sep = "&" if "?" in found["authorization_endpoint"] else "?"
    return found["authorization_endpoint"] + sep + urllib.parse.urlencode(query)


def parse_callback(url: str) -> dict:
    """`{code, state, error}` from the URL his browser landed on."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(str(url or "")).query)
    return {k: (query.get(k) or [None])[0] for k in ("code", "state", "error")}


def _token_call(token_endpoint: str, form: dict, client_secret: str | None, *, post) -> dict:
    if client_secret:
        form = {**form, "client_secret": client_secret}
    status, _, body = post(token_endpoint, urllib.parse.urlencode(form).encode(), {
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    reply = _json(body)
    if status != 200 or not reply.get("access_token"):
        # The provider's error code only: never echo a body that may hold a token.
        code = str(reply.get("error") or status)[:60]
        raise OAuthError("oauth_token_failed", f"The provider refused the token request ({code}).")
    return reply


def exchange(found: dict, client_id: str, client_secret: str | None, code: str,
             verifier: str, *, post, redirect_uri: str = REDIRECT_URI) -> dict:
    return _token_call(found["token_endpoint"], {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": client_id, "code_verifier": verifier,
        "resource": found["resource"]}, client_secret, post=post)


def refresh(token_endpoint: str, resource: str, client_id: str,
            client_secret: str | None, refresh_token: str, *, post) -> dict:
    return _token_call(token_endpoint, {
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id, "resource": resource}, client_secret, post=post)
