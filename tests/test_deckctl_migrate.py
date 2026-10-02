"""K6: `deckctl migrate` reads the live unit and writes the deck.toml that
reproduces it. The owner's box is the fixture: its unit is copied here (the
lines migrate reads), so "the owner's box keeps working unchanged" is a test."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from server import deckconfig

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "deckctl"

OWNER_UNIT = """\
# Agent Deck on the always-on box.
[Unit]
Description=Agent Deck - the board across Sam's Claude sessions
After=network-online.target wg-quick@wg0.service

[Service]
Type=exec
User=deckop
Group=deckop
WorkingDirectory=/opt/agent-deck
EnvironmentFile=-/etc/agent-deck/agentdeck.env
EnvironmentFile=-/home/deckop/.claude/oauth.env
Environment=PYTHONUNBUFFERED=1
Environment=DECK_URL=http://10.99.0.1:7789
ExecStart=/opt/agent-deck/.venv/bin/python -m uvicorn server.app:app --host 10.99.0.1 --port 7789
Restart=always
"""

NEW_BOX_UNIT = """\
[Service]
User=agentdeck
WorkingDirectory=/opt/agent-deck/current
EnvironmentFile=-/home/agentdeck/.claude/oauth.env
ExecStart=/opt/agent-deck/current/.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 7789
"""


def load():
    loader = importlib.machinery.SourceFileLoader("deckctl", str(SCRIPT))
    spec = importlib.util.spec_from_loader("deckctl", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Box:
    def __init__(self, tmp_path, unit=OWNER_UNIT, wg=None, docker=True):
        self.etc = tmp_path / "etc"
        self.etc.mkdir()
        self.toml = self.etc / "deck.toml"
        self.unit = tmp_path / "agentdeck.service"
        if unit is not None:
            self.unit.write_text(unit)
        self.wg = {"10.99.0.1": "wg0"} if wg is None else wg
        self.ran = []
        self.docker = docker

    def main(self, *argv):
        mod = load()

        class Host:
            def have(_, cmd): return self.docker
        ctl = mod.Ctl(config_path=self.toml, euid=0, unit_path=self.unit,
                      wg_addresses=lambda: dict(self.wg),
                      host_factory=lambda cfg: Host(),
                      run=lambda argv, timeout=60: (self.ran.append(argv) or (0, "")))
        return mod.main(list(argv), ctl)


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def written(box):
    return deckconfig.load(box.toml, env={})


def test_the_owners_unit_becomes_the_deck_toml_that_reproduces_it(box, capsys):
    assert box.main("migrate") == 0
    cfg = written(box)
    assert cfg.deck.user == "deckop"
    assert cfg.deck.home == "/home/deckop"
    assert cfg.deck.app_dir == "/opt/agent-deck"
    assert cfg.deck.bind == "10.99.0.1" and cfg.deck.port == 7789
    assert cfg.claude.oauth_env == "/home/deckop/.claude/oauth.env"
    assert cfg.network.mode == "wireguard" and cfg.network.tls == "none"
    assert cfg.network.wireguard_iface == "wg0"
    assert cfg.deck.install_source == "rsync"
    assert cfg.desks.docker is True


def test_it_says_the_two_things_it_cannot_infer_and_how_to_set_them(box, capsys):
    box.main("migrate")
    text = capsys.readouterr().out
    assert "deck.reserved_ports" in text and "doctor.public_probe_urls" in text
    assert "deckctl config set" in text


def test_no_docker_on_the_box_means_desks_docker_false(tmp_path):
    b = Box(tmp_path, docker=False)
    assert b.main("migrate") == 0
    assert written(b).desks.docker is False


def test_a_second_run_never_overwrites_an_existing_deck_toml(box, capsys):
    box.toml.write_text('[deck]\nname = "Mine"\n')
    assert box.main("migrate") == 0
    assert box.toml.read_text() == '[deck]\nname = "Mine"\n'
    assert "already" in capsys.readouterr().out.lower()


def test_a_dry_run_prints_the_toml_and_writes_nothing(box, capsys):
    assert box.main("migrate", "--dry-run") == 0
    assert not box.toml.exists()
    assert 'user = "deckop"' in capsys.readouterr().out


def test_no_unit_means_nothing_to_migrate_and_says_so(tmp_path, capsys):
    b = Box(tmp_path, unit=None)
    assert b.main("migrate") == 1
    assert "install" in capsys.readouterr().err.lower()
    assert not b.toml.exists()


def test_a_loopback_bind_is_local_mode_not_wireguard(tmp_path):
    b = Box(tmp_path, unit=NEW_BOX_UNIT, wg={})
    assert b.main("migrate") == 0
    cfg = written(b)
    assert cfg.network.mode == "local" and cfg.deck.bind == "127.0.0.1"
    assert cfg.deck.app_dir == "/opt/agent-deck/current"
    assert cfg.deck.user == "agentdeck"


def test_a_bind_that_is_not_a_wireguard_address_is_not_called_wireguard(tmp_path):
    unit = OWNER_UNIT.replace("10.99.0.1", "192.168.1.5")
    b = Box(tmp_path, unit=unit, wg={"10.99.0.1": "wg0"})
    assert b.main("migrate") == 0
    assert written(b).network.mode == "local"


def test_a_unit_with_no_execstart_is_refused_not_guessed(tmp_path, capsys):
    b = Box(tmp_path, unit="[Service]\nUser=x\n")
    assert b.main("migrate") == 1
    assert not b.toml.exists()
    assert "ExecStart" in capsys.readouterr().err


def test_migrate_json(box, capsys):
    import json
    assert box.main("migrate", "--json", "--dry-run") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["deck"]["user"] == "deckop" and data["cannot_infer"]
