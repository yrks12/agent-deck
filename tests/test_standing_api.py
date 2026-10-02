"""`/v1/standing-approvals`: the contract the Mac, iPhone and web clients use."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import asking, standing, standing_api, standing_usage
from server.sources import comms as comms_mod

TOKEN = "standing-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
EMAIL = {"desk": "atlas", "kind": "send_email",
         "limits": {"count_per_day": 80, "recipients": ["@acme.com"]}}


@pytest.fixture
def deck(tmp_path, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    surface = api_mod.Surface(snapshot=lambda: {"sessions": []},
                              comms=comms_mod.CommsIndex(),
                              asks_path=tmp_path / "asks.json")
    answered = []
    monkeypatch.setattr(surface, "_desk_for_ask", lambda ask: "atlas")
    monkeypatch.setattr(surface, "_refresh_holding", lambda: None)
    monkeypatch.setattr(surface, "answer_ask",
                        lambda ask_id, reply: answered.append((ask_id, reply))
                        or {"ok": True})
    paths = standing_api.Paths(tmp_path / "s.json", tmp_path / "u.json",
                               tmp_path / "a.jsonl")
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    app.include_router(standing_api.build_router(surface, paths=lambda: paths))
    client = TestClient(app, headers=AUTH)
    client.paths, client.surface, client.answered = paths, surface, answered
    return client


def test_every_route_needs_the_owners_credential(deck):
    bare = TestClient(deck.app, headers={"Authorization": ""})
    for method, url in [("get", "/v1/standing-approvals"),
                        ("post", "/v1/standing-approvals"),
                        ("get", "/v1/standing-approvals/audit"),
                        ("patch", "/v1/standing-approvals/sa_1"),
                        ("post", "/v1/standing-approvals/sa_1/approve"),
                        ("delete", "/v1/standing-approvals/sa_1"),
                        ("post", "/v1/approvals/abcde/standing")]:
        kwargs = {"json": {}} if method in ("post", "patch") else {}
        got = getattr(bare, method)(url, **kwargs)
        assert got.status_code == 401, url
        assert got.json()["reason"] == "unauthorized"


def test_create_list_with_live_usage_edit_and_revoke(deck):
    made = deck.post("/v1/standing-approvals", json=EMAIL).json()["policy"]
    assert made["status"] == "active" and made["created_by"] == "owner"
    assert made["limits"]["count_per_day"] == 80
    assert made["usage"]["count"] == 0 and made["live"] is True

    policy = standing.find(made["id"], deck.paths.policies)
    standing_usage.consume(policy, None, desk="atlas", tool_name="x",
                           subject="s", now=__import__("time").time(),
                           usage_path=deck.paths.usage,
                           audit_path=deck.paths.audit)
    [listed] = deck.get("/v1/standing-approvals").json()["policies"]
    assert listed["usage"]["count"] == 1
    assert deck.get("/v1/standing-approvals?desk=scout").json()[
        "policies"] == []

    edited = deck.patch(f"/v1/standing-approvals/{made['id']}",
                        json={"limits": {"count_per_day": 5}}).json()
    assert edited["policy"]["limits"]["count_per_day"] == 5
    gone = deck.delete(f"/v1/standing-approvals/{made['id']}").json()
    assert gone["policy"]["status"] == "revoked"
    [audit] = deck.get("/v1/standing-approvals/audit").json()["audit"]
    assert audit["policy_id"] == made["id"] and audit["count_after"] == 1


def test_a_proposal_is_listed_and_only_approve_activates_it(deck):
    body = {**EMAIL, "status": "proposed", "proposed_by": "atlas"}
    made = deck.post("/v1/standing-approvals", json=body).json()["policy"]
    assert made["status"] == "proposed" and made["created_by"] == "atlas"
    assert made["live"] is False
    pending = deck.get("/v1/standing-approvals?status=proposed").json()
    assert [p["id"] for p in pending["policies"]] == [made["id"]]
    on = deck.post(f"/v1/standing-approvals/{made['id']}/approve").json()
    assert on["policy"]["status"] == "active" and on["policy"]["live"]
    again = deck.post(f"/v1/standing-approvals/{made['id']}/approve")
    assert again.status_code == 409 and again.json()["reason"] == "not_proposed"


@pytest.mark.parametrize("body,status,reason", [
    ({"desk": "atlas", "kind": "run_command", "pattern": "rm -rf*",
      "limits": {"count_per_day": 3}}, 409, "never_coverable"),
    ({"desk": "atlas", "kind": "send_email"}, 400, "no_limit"),
    ({"desk": "atlas", "kind": "teleport",
      "limits": {"count_per_day": 3}}, 400, "bad_kind"),
])
def test_refusals_keep_their_reason(deck, body, status, reason):
    got = deck.post("/v1/standing-approvals", json=body)
    assert got.status_code == status
    assert got.json() == {"ok": False, "reason": reason,
                          "detail": got.json()["detail"]}


def test_unknown_policy_is_404_on_every_write(deck):
    for got in (deck.patch("/v1/standing-approvals/sa_x", json={}),
                deck.post("/v1/standing-approvals/sa_x/approve"),
                deck.delete("/v1/standing-approvals/sa_x")):
        assert (got.status_code, got.json()["reason"]) == (
            404, "unknown_policy")


def test_a_card_becomes_a_standing_approval_and_this_one_goes_through(deck):
    ask = asking.record(deck.surface.asks_path, agent="atlas", tool="Bash",
                        subject="gh pr list --limit 5", cwd="/w/atlas")
    got = deck.post(f"/v1/approvals/{ask.id}/standing",
                    json={"limits": {"count_per_day": 20}}).json()
    [policy] = got["policies"]
    assert (policy["desk"], policy["kind"], policy["tool"]) == (
        "atlas", "run_command", "Bash")
    assert policy["pattern"].startswith("gh") and policy["status"] == "active"
    assert deck.answered == [(ask.id, "once")]


def test_the_card_names_its_standing_option(deck):
    ask = asking.record(deck.surface.asks_path, agent="atlas", tool="Bash",
                        subject="gh pr list", cwd="/w/atlas")
    [card] = deck.get("/v1/approvals").json()["approvals"]
    assert card["standing_option"] == {
        "available": True, "kind": "run_command", "desk": "atlas",
        "route": f"/v1/approvals/{ask.id}/standing",
        "summary": "Let atlas do this up to a daily limit"}


@pytest.mark.parametrize("subject", ["rm -rf build", "cat ~/.ssh/id_rsa"])
def test_a_floor_card_cannot_become_a_standing_approval(deck, subject):
    ask = asking.record(deck.surface.asks_path, agent="atlas", tool="Bash",
                        subject=subject, cwd="/w/atlas")
    [card] = deck.get("/v1/approvals").json()["approvals"]
    assert card["standing_option"]["available"] is False
    got = deck.post(f"/v1/approvals/{ask.id}/standing",
                    json={"limits": {"count_per_day": 2}})
    assert (got.status_code, got.json()["reason"]) == (409, "never_coverable")
    assert standing.load(deck.paths.policies) == []
    assert deck.answered == []


def test_a_card_without_a_limit_is_refused_and_nothing_is_answered(deck):
    ask = asking.record(deck.surface.asks_path, agent="atlas", tool="Bash",
                        subject="gh pr list", cwd="/w/atlas")
    got = deck.post(f"/v1/approvals/{ask.id}/standing", json={})
    assert (got.status_code, got.json()["reason"]) == (400, "no_limit")
    assert deck.answered == []


def test_the_live_app_serves_the_routes():
    """Mounted on the real app: refused for want of a credential, not 404."""
    from server import app as app_mod
    bare = TestClient(app_mod.app, headers={"Authorization": ""})
    for method, url in [("get", "/v1/standing-approvals"),
                        ("get", "/v1/standing-approvals/audit"),
                        ("post", "/v1/standing-approvals/sa_1/approve"),
                        ("post", "/v1/approvals/abcde/standing")]:
        kwargs = {"json": {}} if method == "post" else {}
        got = getattr(bare, method)(url, **kwargs)
        assert got.status_code in (401, 503), (url, got.status_code)


def test_the_contract_names_every_refusal_and_every_row_field():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    doc = (root / "docs" / "client-api.md").read_text()
    section = doc[doc.index(". Standing approvals — "):]
    code = ((root / "server" / "standing.py").read_text()
            + (root / "server" / "standing_api.py").read_text())
    reasons = set(re.findall(r'(?:StandingError|Refused)\(\d+,\s*"(\w+)"', code))
    reasons |= {"never_coverable", "no_desk"}
    assert reasons and not {r for r in reasons if f"`{r}`" not in section}
    sample = standing_api.row(standing.Policy(id="x", desk="d",
                                              kind="send_email"), {}, 0.0)
    for key in [*sample, *sample["limits"], *sample["usage"]]:
        assert f'"{key}"' in section, key
