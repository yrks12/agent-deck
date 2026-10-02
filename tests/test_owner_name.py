"""The owner's name is configuration, never a literal in the product.

`server/owner.py`: `DECK_OWNER_NAME` first, then deck.toml `[owner] name`, then
the neutral "the owner". A broken config falls back instead of raising.
"""
from server import deckconfig, owner


def test_default_is_neutral(tmp_path):
    env = {"DECK_CONFIG": str(tmp_path / "missing.toml")}
    assert owner.name(env) == "the owner"
    assert owner.title(env) == "The owner"


def test_deck_toml_names_the_owner(tmp_path):
    cfg = tmp_path / "deck.toml"
    cfg.write_text('[owner]\nname = "Sam"\n')
    env = {"DECK_CONFIG": str(cfg)}
    assert deckconfig.load(env=env).owner.name == "Sam"
    assert owner.name(env) == "Sam"


def test_env_beats_deck_toml(tmp_path):
    cfg = tmp_path / "deck.toml"
    cfg.write_text('[owner]\nname = "Sam"\n')
    assert owner.name({"DECK_CONFIG": str(cfg), "DECK_OWNER_NAME": " Ada "}) == "Ada"


def test_a_broken_config_falls_back_rather_than_raising(tmp_path):
    cfg = tmp_path / "deck.toml"
    cfg.write_text("[owner\nname = ")
    assert owner.name({"DECK_CONFIG": str(cfg)}) == "the owner"


# -- the owner's wire id ------------------------------------------------------

import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from server import office  # noqa: E402

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "cc-office.js"


def test_handle_defaults_to_the_neutral_owner(tmp_path):
    assert owner.handle({"DECK_CONFIG": str(tmp_path / "none.toml")}) == "owner"


def test_an_older_deck_keeps_its_handle_through_deck_toml(tmp_path):
    cfg = tmp_path / "deck.toml"
    cfg.write_text('[owner]\nhandle = "sam"\n')
    assert owner.handle({"DECK_CONFIG": str(cfg)}) == "sam"
    assert owner.handle({"DECK_CONFIG": str(cfg), "DECK_OWNER_HANDLE": "ada"}) == "ada"


def test_a_handle_that_is_not_a_slug_is_ignored(tmp_path):
    assert owner.handle({"DECK_OWNER_HANDLE": "Sam Carter"}) == "owner"
    assert owner.handle({"DECK_OWNER_HANDLE": "../etc"}) == "owner"


def test_the_handle_is_on_every_v1_response_so_the_app_can_learn_it():
    from server.app import OWNER_HEADER, app
    r = TestClient(app).get("/v1/agents")
    assert r.headers.get(OWNER_HEADER) == office.OWNER_HANDLE


def test_his_display_name_rides_beside_the_handle(monkeypatch):
    """The apps' sidebar said "Owner" while the box knew his name: the handle
    is a wire id, not a name. `X-Deck-Owner-Name` carries `DECK_OWNER_NAME`,
    percent-encoded (a header is ASCII), and is absent when no name is set --
    the app's "Owner" is its own fallback, never "the owner" from here."""
    from server.app import OWNER_NAME_HEADER, app
    monkeypatch.setenv("DECK_OWNER_NAME", "Zoë Smith")
    r = TestClient(app).get("/v1/agents")
    assert r.headers.get(OWNER_NAME_HEADER) == "Zo%C3%AB%20Smith"
    monkeypatch.delenv("DECK_OWNER_NAME")
    monkeypatch.setattr(owner, "name", lambda env=None: owner.DEFAULT)
    r = TestClient(app).get("/v1/agents")
    assert OWNER_NAME_HEADER not in r.headers


def _hook_frames(tmp_path, sender, env_extra):
    home = tmp_path / "claude"
    bus = home / "agent-bus"
    bus.mkdir(parents=True)
    (bus / "office.json").write_text(json.dumps({"generated_at": time.time(), "sessions": {
        "sid-desk": {"name": "desk", "cwd": "/w", "toplevel": "/w", "branch": "main",
                     "state": "IDLE", "address": "uds:/tmp/1.sock"}}}))
    (bus / "messages.jsonl").write_text(json.dumps(
        {"ts": time.time(), "to": "sid-desk", "id": "m1", "from": sender,
         "text": "ship it"}) + "\n")
    env = {k: v for k, v in os.environ.items() if k not in ("DECK_OWNER_HANDLE", "DECK_CONFIG")}
    done = subprocess.run(
        ["node", str(HOOK)], capture_output=True, text=True, timeout=10,
        input=json.dumps({"session_id": "sid-desk", "hook_event_name": "UserPromptSubmit"}),
        env={**env, "CLAUDE_CONFIG_DIR": str(home),
             "DECK_CONFIG": str(tmp_path / "none.toml"), **env_extra})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


def test_the_hook_reads_an_older_decks_handle_from_deck_toml(tmp_path):
    cfg = tmp_path / "deck.toml"
    cfg.write_text('[deck]\nport = 7789\n\n[owner]\nname = "Sam"\nhandle = "sam"  # kept\n')
    text = _hook_frames(tmp_path, "sam", {"DECK_CONFIG": str(cfg)})
    assert office.OWNER_MARK in text


def test_without_config_the_hook_treats_only_the_neutral_id_as_him(tmp_path):
    assert office.OWNER_MARK in _hook_frames(tmp_path, "owner", {})
    assert office.OWNER_MARK not in _hook_frames(tmp_path / "b", "sam", {})
