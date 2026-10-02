"""`/api/standing-approvals*`: the browser board's door to standing approvals.

The board holds an HttpOnly cookie, not a bearer, so it cannot call `/v1`. Like
`/api/events`, these routes are gated by the board's own middleware
(`app.authorise_api`) and share the handlers with `/v1`.
"""

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import deckauth

EMAIL = {"desk": "atlas", "kind": "send_email", "limits": {"count_per_day": 80}}
ROUTES = [("get", "/api/standing-approvals"), ("post", "/api/standing-approvals"),
          ("get", "/api/standing-approvals/audit"),
          ("patch", "/api/standing-approvals/sa_1"),
          ("post", "/api/standing-approvals/sa_1/approve"),
          ("delete", "/api/standing-approvals/sa_1"),
          ("get", "/api/approvals"), ("post", "/api/approvals/abcde/standing")]


@pytest.fixture
def board(monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod, "STANDING_PATH", tmp_path / "s.json")
    monkeypatch.setattr(app_mod, "STANDING_USAGE_PATH", tmp_path / "u.json")
    monkeypatch.setattr(app_mod, "STANDING_AUDIT_PATH", tmp_path / "a.jsonl")
    return TestClient(app_mod.app)


def test_every_board_route_is_gated_like_every_api_route(board, monkeypatch):
    monkeypatch.setattr(deckauth, "authorised", lambda request: False)
    for method, url in ROUTES:
        kwargs = {"json": {}} if method in ("post", "patch") else {}
        assert getattr(board, method)(url, **kwargs).status_code == 401, url


def test_the_board_creates_lists_approves_and_revokes(board, monkeypatch):
    monkeypatch.setattr(deckauth, "authorised", lambda request: True)
    proposed = board.post("/api/standing-approvals", json={
        **EMAIL, "status": "proposed", "proposed_by": "atlas"}).json()["policy"]
    assert proposed["status"] == "proposed"
    listed = board.get("/api/standing-approvals").json()["policies"]
    assert [p["id"] for p in listed] == [proposed["id"]]
    assert listed[0]["usage"]["count"] == 0
    approved = board.post(f"/api/standing-approvals/{proposed['id']}/approve")
    assert approved.json()["policy"]["status"] == "active"
    edited = board.patch(f"/api/standing-approvals/{proposed['id']}",
                         json={"note": "n"}).json()["policy"]
    assert edited["note"] == "n"
    gone = board.delete(f"/api/standing-approvals/{proposed['id']}")
    assert gone.json()["policy"]["status"] == "revoked"
    assert "audit" in board.get("/api/standing-approvals/audit").json()


def test_refusals_keep_their_reason(board, monkeypatch):
    monkeypatch.setattr(deckauth, "authorised", lambda request: True)
    got = board.post("/api/standing-approvals", json={"desk": "atlas", "kind": "nope"})
    assert got.status_code == 400 and got.json()["reason"] == "bad_kind"


def test_the_board_lists_approvals_and_refuses_an_unknown_card(board, monkeypatch):
    monkeypatch.setattr(deckauth, "authorised", lambda request: True)
    assert "approvals" in board.get("/api/approvals").json()
    got = board.post("/api/approvals/abcde/standing", json={"limits": {"count_per_day": 5}})
    assert got.status_code == 404 and got.json()["reason"] == "unknown_ask"
