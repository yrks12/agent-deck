""""<Agent>'s screen" on the wire: he can see it, and he can take it over.

Written before the routes exist.

`server/sandbox.py` can start a desk's computer, photograph its display and
inject a click. Nothing serves any of that, so the panel in the owner's
screenshot -- the agent's live desktop with an **Open** button -- has no
endpoint behind it. These are the three routes that give it one, and the four
ways serving them would be worse than not serving them:

* **It becomes an unauthenticated shell.** A route that photographs a
  signed-in browser and injects keystrokes is strictly more dangerous than
  `GET /v1/agents`, and the deck already has a surface with no token on it:
  `server/app.py`'s `/api/*`, loopback-only and open. Nothing here may go
  there. Every route is asserted to 401 without the bearer token, and 503 with
  no token configured at all -- the same fail-closed rule as the rest of /v1.
* **It shows him a still frame and lets him think it is live.** A JPEG of a
  checkout page is identical whether it was taken a second ago or before the
  capture died, and `screen.py` exists because those are opposite facts. The
  age travels with the frame, and `stale` is computed rather than left to the
  client to work out from a clock it does not share.
* **It reports "no screen" for four different reasons in the same words.**
  Docker not running, the desk's computer not started, and ffmpeg failing on a
  live display are three different things for the owner to do, so they are
  three slugs -- and each one is in `docs/client-api.md`, because a client
  that meets an undocumented slug renders its generic fallback forever.
* **A gesture is a command.** `{"action": "key", "key": "--file"}` is
  `xdotool key --file`, a file read wearing a keystroke. The route refuses it
  with a 400 rather than passing it down and hoping.

Hermetic: `sandbox` is monkeypatched at the seam, so nothing here starts a
container. The real container is `-m live` in tests/test_sandbox_live.py.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office, sandbox
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
ACME = {"name": "acme", "cwd": "/tmp/p", "engine": "claude", "mission": "sell",
        "label": "Closer", "charter": "Own the deal.", "reports_to": None}

#: The smallest thing that is unmistakably a JPEG: SOI, an APP0 marker, EOI.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32 + b"\xff\xd9"


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def surface(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [ACME]}))
    return api_mod.Surface(
        snapshot=lambda: {"generated_at": 0.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=path,
        prefs_path=bus / "agent_prefs.json", asks_path=bus / "asks.json",
        rules_path=bus / "autoreview.json", routines_path=bus / "routines.json",
    )


@pytest.fixture
def client(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


class _Ran:
    """A `docker` call that did not happen, and the argv it would have been."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout=None):
        self.calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout=JPEG, stderr=b"")


@pytest.fixture
def computer_up(monkeypatch):
    """A desk's computer that is running and photographs to `JPEG`.

    The seam is `sandbox._run` -- the one function that actually executes
    `docker` -- and NOT `sandbox.send_input`. Stubbing `send_input` would take
    the keysym and coordinate validation out with it, and those are exactly
    what the input tests below are about: a test that mocks the guard it is
    checking passes on code that has no guard.
    """
    ran = _Ran()
    monkeypatch.setattr(sandbox, "_run", ran)
    monkeypatch.setattr(sandbox, "is_up", lambda desk: True)
    return ran


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# ── the token, first ───────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path", [
    ("get", "/v1/agents/acme/screen"),
    ("get", "/v1/agents/acme/screen.jpg"),
    ("post", "/v1/agents/acme/screen/input"),
])
def test_no_screen_route_answers_without_the_bearer_token(client, method, path):
    """This is the back door, so it is the first test in the file. A route
    that photographs a signed-in browser and types into it must be no weaker
    than the one that lists desks."""
    response = client.request(method.upper(), path, json={})
    assert response.status_code == 401
    assert response.json()["reason"] == "unauthorized"


@pytest.mark.parametrize("path", ["/v1/agents/acme/screen",
                                  "/v1/agents/acme/screen.jpg"])
def test_with_no_token_configured_the_screen_is_closed_not_open(
        client, monkeypatch, path):
    """Fail closed. An unset env var must not be what decides whether the
    owner's signed-in browser is world-readable."""
    monkeypatch.delenv(api_mod.TOKEN_ENV, raising=False)
    response = client.get(path, headers=auth())
    assert response.status_code == 503
    assert response.json()["reason"] == "auth_not_configured"


# ── he can see it ──────────────────────────────────────────────────────────


def test_the_frame_arrives_as_an_image_with_its_age_beside_it(client, computer_up):
    """`screen.py`'s whole argument: a frame without an age is a lie."""
    response = client.get("/v1/agents/acme/screen.jpg", headers=auth())
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == JPEG
    assert response.headers["cache-control"] == "no-store"
    assert float(response.headers["x-frame-age"]) >= 0.0


def test_the_status_route_says_whether_the_computer_is_even_on(client, computer_up):
    body = client.get("/v1/agents/acme/screen", headers=auth()).json()
    assert body["desk"] == "acme"
    assert body["computer"]["running"] is True
    assert body["display"] == sandbox.DISPLAY
    assert body["width"] == sandbox.SIZE[0] and body["height"] == sandbox.SIZE[1]
    assert body["frame_url"] == "/v1/agents/acme/screen.jpg"
    assert body["input_url"] == "/v1/agents/acme/screen/input"


def test_a_desk_with_no_computer_says_so_in_a_slug_the_doc_carries(
        client, monkeypatch):
    """Three different causes, three different things for him to do."""
    monkeypatch.setattr(sandbox, "is_up", lambda desk: False)
    body = client.get("/v1/agents/acme/screen", headers=auth()).json()
    assert body["computer"]["running"] is False

    def _no_computer(desk):
        raise sandbox.SandboxError("computer_not_running", "not up")

    monkeypatch.setattr(sandbox, "frame", _no_computer)
    response = client.get("/v1/agents/acme/screen.jpg", headers=auth())
    assert response.status_code == 409
    assert response.json()["reason"] == "computer_not_running"


def test_docker_being_off_is_its_own_answer_not_a_dead_panel(client, monkeypatch):
    def _no_docker(desk):
        raise sandbox.SandboxError("docker_unavailable", "not on PATH")

    monkeypatch.setattr(sandbox, "is_up", _no_docker)
    monkeypatch.setattr(sandbox, "frame", _no_docker)
    response = client.get("/v1/agents/acme/screen.jpg", headers=auth())
    assert response.status_code == 503
    assert response.json()["reason"] == "docker_unavailable"


def test_a_desk_that_is_not_on_the_roster_is_a_404_not_a_container(
        client, computer_up):
    """Otherwise the route names a container from a string off the wire, and
    `GET /v1/agents/../../x/screen` is the shape of that bug."""
    response = client.get("/v1/agents/ghost/screen.jpg", headers=auth())
    assert response.status_code == 404
    assert response.json()["reason"] == "unknown_agent"


# ── he can take it over ────────────────────────────────────────────────────


def test_a_click_he_sends_reaches_the_agents_display(client, computer_up):
    response = client.post("/v1/agents/acme/screen/input", headers=auth(),
                           json={"action": "click", "x": 301, "y": 301})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "action": "click"}
    argv = computer_up.calls[-1]
    assert argv[0] == "docker" and sandbox.container_name("acme") in argv
    assert argv[-6:] == ["xdotool", "mousemove", "301", "301",
                         "click", "1"]
    assert f"DISPLAY={sandbox.DISPLAY}" in argv


def test_typing_a_password_with_a_backtick_in_it_is_passed_through_whole(
        client, computer_up):
    """The take-over is where he types credentials. The route must not clean
    the text up -- a password with a backtick in it is a password -- and it
    must arrive as one argv element after `--`, never inside a shell string."""
    secret = "p`ass$word --window 1"
    response = client.post("/v1/agents/acme/screen/input", headers=auth(),
                           json={"action": "type", "text": secret})
    assert response.status_code == 200
    argv = computer_up.calls[-1]
    assert argv[-1] == secret and argv[-2] == "--"
    assert "sh" not in argv and "-c" not in argv


def test_a_keystroke_that_is_really_an_option_is_a_400(client, computer_up):
    """`xdotool key --file /etc/passwd`. Refused with a slug, and nothing is
    executed -- the count of `docker` calls is the assertion, not the reply."""
    response = client.post("/v1/agents/acme/screen/input", headers=auth(),
                           json={"action": "key", "key": "--file"})
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_input"
    assert computer_up.calls == [], "a refused gesture still ran a command"


def test_an_unknown_gesture_is_refused_rather_than_ignored(client, computer_up):
    response = client.post("/v1/agents/acme/screen/input", headers=auth(),
                           json={"action": "run", "cmd": "id"})
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_input"
    assert computer_up.calls == []


def test_a_click_off_the_screen_never_becomes_a_command(client, computer_up):
    """Coordinates come from a phone scaling a JPEG. Refused, not clamped:
    clamping hides the client's arithmetic by clicking somewhere plausible."""
    response = client.post("/v1/agents/acme/screen/input", headers=auth(),
                           json={"action": "click", "x": 99999, "y": 1})
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_input"
    assert computer_up.calls == []


# ── the contract is in the doc, not in a report ────────────────────────────


def test_every_slug_this_surface_can_return_is_in_the_client_doc():
    """A client that meets an undocumented slug shows its generic fallback for
    a specific cause, forever. Same rule as tests/test_client_api_doc.py."""
    doc = (Path(__file__).resolve().parent.parent
           / "docs" / "client-api.md").read_text()
    for slug in ("computer_not_running", "no_frame", "docker_unavailable",
                 "bad_input", "computer_not_responding", "input_refused"):
        assert f"`{slug}`" in doc, f"undocumented screen slug: {slug}"
    assert "/v1/agents/{name}/screen.jpg" in doc
    assert "X-Frame-Age" in doc
