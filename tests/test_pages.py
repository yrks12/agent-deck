"""The page-level things that are easy to ship broken.

Both of these shipped broken once: the manager page existed with no way to reach
it from the deck, and a stylesheet update never reached an open browser because
nothing told it to revalidate.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod

WEB = Path(__file__).resolve().parent.parent / "web"
PAGES = ("index.html", "feed.html", "manager.html")
ROUTES = ('href="/"', 'href="/feed"', 'href="/manager"')


@pytest.fixture
def client():
    return TestClient(app_mod.app)


@pytest.mark.parametrize("page", PAGES)
def test_every_page_links_to_every_other_page(page):
    html = (WEB / page).read_text()
    for href in ROUTES:
        assert href in html, f"{page} has no way to reach {href}"


@pytest.mark.parametrize("page", PAGES)
def test_exactly_one_tab_is_marked_current(page):
    html = (WEB / page).read_text()
    assert html.count("tab--on") == 1, f"{page} must mark exactly one tab current"


@pytest.mark.parametrize("route", ("/", "/feed", "/manager"))
def test_every_route_serves(client, route):
    assert client.get(route).status_code == 200


def test_static_assets_are_revalidated_not_cached(client):
    """An open tab must pick up a CSS or JS change on its next load."""
    for asset in ("/static/style.css", "/static/app.js", "/static/manager.js"):
        res = client.get(asset)
        assert res.status_code == 200, asset
        assert "no-cache" in res.headers.get("cache-control", ""), asset


def test_comms_payload_carries_the_state_of_every_session(client, monkeypatch):
    """The chart colours its cards by state, so the payload must carry it."""
    monkeypatch.setattr(app_mod, "_state", {
        "sessions": [
            {"session_id": "sid-a", "name": "mgr", "pid": 1, "state": "WORKING",
             "project": "proj", "git_branch": "develop"},
            {"session_id": "sid-b", "name": "fixer", "pid": 2, "state": "NEEDS_YOU",
             "project": "proj", "git_branch": "develop"},
        ],
    })
    body = client.get("/api/comms").json()
    assert body["sessions"]["sid-a"]["state"] == "WORKING"
    assert body["sessions"]["sid-b"]["state"] == "NEEDS_YOU"
    assert body["sessions"]["sid-b"]["name"] == "fixer"
