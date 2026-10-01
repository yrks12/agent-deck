"""Every door that starts a desk must CHOOSE its channel, not assume a Mac.

The measured defect. There is a second install of this deck on a Linux box --
deployed, authenticated, running under systemd, answering 200 -- and it has
hired nobody, ever. The reason is not configuration. `server/api.py::Surface.
_start` and `server/app.py::roster_start` are the only two doors that put a
process at a desk, and both called `spawn.spawn_terminal`, which shells out to
`osascript` to open Terminal.app. Linux has neither. Every hire on that box
reached the last step and died there.

`spawn.start` now exists to make that choice in one place, exactly as `_vouch`
and `_approval_settings` already live inside spawn rather than in each caller.
This file pins the WIRING -- that both doors actually go through it. A seam
with no caller is what `spawn_background` was for months: real, tested, and
never once executed.

The sweep is the CLASS of doors, not one of them. That is the whole lesson of
the last regression here: `interview_agent` vouched for its workspace and
`start_agent` did not, because the gate lived in a caller instead of in the
thing every caller must pass through. So both doors are parametrized against
both channels, and every assertion names the GOOD signal -- the headless
launcher was called, the seed arrived, the pretrust verdict came back -- never
the absence of osascript.
"""

import asyncio
import json
from pathlib import Path

import pytest

from server import api as api_mod
from server import app as app_mod
from server import office, roster, spawn
from server.sources import comms as comms_mod

ACME = {"name": "acme", "cwd": "/tmp/p", "engine": "claude", "mission": "sell",
        "label": "Closer", "charter": "Own the deal.", "reports_to": None}

#: What `choose_channel` returns on the Linux box: osascript is not on PATH.
NO_TERMINAL_APP = spawn.Channel(
    "background", "no_osascript",
    "osascript is not on PATH, so there is no Terminal.app to open")

#: What it returns in a logged-in graphical session on this Mac.
HAS_TERMINAL_APP = spawn.Channel(
    "terminal", "aqua", "a logged-in graphical session")

#: The pretrust shape `_verdict` always publishes. `muted` is always present.
VOUCHED = {"ok": True, "reason": "", "detail": "", "muted": []}


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_file(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [ACME]}))
    return path


@pytest.fixture
def surface(bus, roster_file):
    return api_mod.Surface(
        snapshot=lambda: {"generated_at": 0.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=roster_file,
        prefs_path=bus / "agent_prefs.json", asks_path=bus / "asks.json",
        rules_path=bus / "autoreview.json", routines_path=bus / "routines.json",
    )


@pytest.fixture
def launchers(monkeypatch):
    """Record which channel was actually taken, and with what.

    Both real launchers are replaced, so a door that took the wrong one is a
    recorded call in the wrong bucket rather than a window on someone's screen
    or a `claude --bg` process nobody reaps.
    """
    calls = {"terminal": [], "background": []}

    def fake_terminal(desk, *, roster_path, seed=""):
        calls["terminal"].append({"desk": desk.name, "seed": seed,
                                  "roster_path": roster_path})
        return {"ok": True, "detail": "window opened", "pretrust": VOUCHED}

    def fake_background(desk, *, roster_path, seed=""):
        calls["background"].append({"desk": desk.name, "seed": seed,
                                    "roster_path": roster_path})
        return {"ok": True, "agent_id": "0b697cee", "pretrust": VOUCHED}

    monkeypatch.setattr(spawn, "spawn_terminal", fake_terminal)
    monkeypatch.setattr(spawn, "spawn_background", fake_background)
    return calls


def _door_v1(surface, monkeypatch):
    """`POST /v1/agents/{name}/start` -- the door an agent uses to start the
    colleague it just hired."""
    return surface.start_agent("acme")


def _door_roster(surface, monkeypatch):
    """`POST /api/roster/{name}/start` -- the board's own button. It has no
    prefs store, so it is a genuinely different path through the same gate."""
    monkeypatch.setattr(app_mod, "ROSTER_PATH", str(surface._roster_path))
    monkeypatch.setattr(app_mod, "_find_desk",
                        lambda name: roster.Desk(**ACME))
    response = asyncio.run(app_mod.roster_start("acme"))
    assert response.status_code == 200, response.body
    return json.loads(response.body)


DOORS = [("v1_start", _door_v1), ("roster_start", _door_roster)]


@pytest.mark.parametrize("label,door", DOORS, ids=[d[0] for d in DOORS])
def test_a_deck_with_no_terminal_app_still_hires(label, door, surface,
                                                 launchers, monkeypatch):
    """THE test. On the box, both doors must reach the headless launcher.

    Asserts the presence of the good signal -- the background launcher ran --
    because "osascript was not called" is also what a door that did nothing at
    all looks like.
    """
    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: NO_TERMINAL_APP)
    body = door(surface, monkeypatch)

    assert launchers["background"], (
        f"{label} never reached the headless launcher on a box with no "
        f"Terminal.app; terminal calls were {launchers['terminal']}")
    assert len(launchers["background"]) == 1
    assert launchers["background"][0]["desk"] == "acme"
    assert body["channel"] == "background", body


@pytest.mark.parametrize("label,door", DOORS, ids=[d[0] for d in DOORS])
def test_a_mac_with_a_window_still_opens_one(label, door, surface,
                                             launchers, monkeypatch):
    """The other half. A fix that sent everything headless would pass the test
    above and silently take his Mac's windows away -- and a headless session on
    a machine he is sitting at is one he cannot watch or type into."""
    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: HAS_TERMINAL_APP)
    body = door(surface, monkeypatch)

    assert launchers["terminal"], (
        f"{label} stopped opening a window on a logged-in Mac; background "
        f"calls were {launchers['background']}")
    assert body["channel"] == "terminal", body


@pytest.mark.parametrize("label,door", DOORS, ids=[d[0] for d in DOORS])
def test_the_pretrust_verdict_survives_both_doors(label, door, surface,
                                                  launchers, monkeypatch):
    """A desk that started but is stuck on the trust dialog must stay tellable
    apart from one that never started -- on the box too, where the stuck
    session has no window in which anyone could ever notice."""
    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: NO_TERMINAL_APP)
    body = door(surface, monkeypatch)

    assert set(body["pretrust"]) == {"ok", "reason", "detail", "muted"}, body
    assert body["pretrust"]["ok"] is True


def test_the_headless_id_reaches_the_caller(surface, launchers, monkeypatch):
    """`agent_id` is how anything later attaches to, reads or stops a headless
    hire. A door that dropped it would start agents on the box that nobody
    could ever address again."""
    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: NO_TERMINAL_APP)
    body = surface.start_agent("acme")
    assert body["agent_id"] == "0b697cee", body


def test_the_seed_reaches_the_desk_through_the_v1_door(surface, launchers,
                                                       monkeypatch):
    """On the box the seed IS the job -- it carries the hire brief and the
    interview prompt. A headless agent started with no seed is what the CLI
    itself calls "idle -- send a prompt to start"."""
    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: NO_TERMINAL_APP)
    surface._start(roster.Desk(**ACME), seed="Ask the owner what the job is.")
    assert launchers["background"][0]["seed"] == (
        "Ask the owner what the job is."), launchers["background"]


def test_no_door_calls_the_terminal_launcher_directly(monkeypatch):
    """Aim the check where the fault IS. The bug was not that one door was
    wrong -- it was that the choice lived in the callers at all, so a third
    door added later inherits it. Parsing the source is the only check that
    still fails when someone adds that third door."""
    import ast
    import inspect

    offenders = []
    for module in (api_mod, app_mod):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and node.attr in {"spawn_terminal", "spawn_background"}
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "spawn"):
                offenders.append(f"{module.__name__}:{node.lineno} "
                                 f"calls spawn.{node.attr} directly")
    assert offenders == [], (
        "a door picks its own channel instead of going through spawn.start: "
        + "; ".join(offenders))
