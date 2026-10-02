"""K1: `/etc/agent-deck/deck.toml` -> one frozen `DeckConfig`.

The promise under test: a box with no deck.toml (the owner's Mac daemon today)
gets the documented defaults and nothing is written, raised or guessed; a bad
value is refused by its key name in a plain sentence, never a traceback.
"""

import dataclasses

import pytest

from server import deckconfig
from server.deckconfig import ConfigError, load

# Every key in K1 (docs/plans/2026-09-30-easy-setup.md section 3.1) and its default.
CONTRACT_DEFAULTS = {
    "deck.name": "Shaliach",
    "deck.user": "agentdeck",
    "deck.home": "/home/agentdeck",
    "deck.app_dir": "/opt/agent-deck/current",
    "deck.state_dir": "/var/lib/agent-deck",
    "deck.bind": "127.0.0.1",
    "deck.port": 7789,
    "deck.reserved_ports": (),
    "deck.install_source": "release",
    "network.mode": "public",
    "network.tls": "sslip",
    "network.hostname": "",
    "network.acme_email": "",
    "network.wireguard_iface": "wg0",
    "desks.docker": True,
    "desks.image": "agent-deck/desk-computer",
    "claude.channel": "stable",
    "claude.oauth_env": "/home/agentdeck/.claude/oauth.env",
    "claude.token_issued": "",
    "doctor.public_probe_urls": (),
    "doctor.whatsapp": False,
    "update.base_url": "",
    "update.channel": "stable",
    "notify.ask_urgent_after_minutes": 30,
    "notify.urgent_gap_minutes": 10,
}

OWNER_BOX = """
[deck]
user = "deckop"
home = "/home/deckop"
app_dir = "/opt/agent-deck"
bind = "10.99.0.1"
port = 7789
reserved_ports = [7788]
install_source = "rsync"

[network]
mode = "wireguard"
tls = "none"

[claude]
oauth_env = "/home/deckop/.claude/oauth.env"

[doctor]
public_probe_urls = ["https://v.initech.example/"]
whatsapp = true
"""


def _write(tmp_path, text):
    path = tmp_path / "deck.toml"
    path.write_text(text)
    return path


def test_no_file_means_every_documented_default(tmp_path):
    cfg = load(tmp_path / "absent.toml", env={})
    assert {k: cfg.get(k) for k in CONTRACT_DEFAULTS} == CONTRACT_DEFAULTS
    assert cfg.source is None
    assert list(tmp_path.iterdir()) == []  # reading never creates anything


def test_default_path_is_etc_and_absent_is_not_an_error(monkeypatch):
    assert deckconfig.DEFAULT_PATH == "/etc/agent-deck/deck.toml"
    cfg = load(env={"DECK_CONFIG": "/nonexistent/deck.toml"})
    assert cfg.deck.port == 7789 and cfg.source is None


def test_owner_box_profile_reads_back(tmp_path):
    cfg = load(_write(tmp_path, OWNER_BOX), env={})
    assert cfg.deck.user == "deckop"
    assert cfg.deck.bind == "10.99.0.1"
    assert cfg.deck.reserved_ports == (7788,)
    assert cfg.deck.install_source == "rsync"
    assert cfg.network.mode == "wireguard"
    assert cfg.doctor.public_probe_urls == ("https://v.initech.example/",)
    assert cfg.doctor.whatsapp is True
    # a key the file does not name keeps its default
    assert cfg.desks.image == "agent-deck/desk-computer"
    assert cfg.source == tmp_path / "deck.toml"


def test_env_overrides_single_keys_and_the_path(tmp_path):
    path = _write(tmp_path, OWNER_BOX)
    cfg = load(env={"DECK_CONFIG": str(path), "DECK_BIND": "10.99.0.9",
                    "DECK_PORT": "7790", "DECK_STATE_DIR": str(tmp_path / "st")})
    assert cfg.deck.user == "deckop"  # DECK_CONFIG chose the file
    assert (cfg.deck.bind, cfg.deck.port) == ("10.99.0.9", 7790)
    assert cfg.deck.state_dir == str(tmp_path / "st")


def test_it_is_frozen(tmp_path):
    cfg = load(tmp_path / "absent.toml", env={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.deck.port = 1


def test_unknown_keys_are_ignored_so_a_rollback_still_starts(tmp_path):
    cfg = load(_write(tmp_path, '[deck]\nfuture_key = 1\n[future]\nx = 2\n'), env={})
    assert cfg.deck.port == 7789


@pytest.mark.parametrize("text, env, key", [
    ('[deck]\nport = "abc"\n', {}, "deck.port"),
    ('[deck]\nport = 70000\n', {}, "deck.port"),
    ('[deck]\nport = true\n', {}, "deck.port"),
    ('', {"DECK_PORT": "seven"}, "deck.port"),
    ('[deck]\nreserved_ports = 7788\n', {}, "deck.reserved_ports"),
    ('[deck]\nreserved_ports = ["x"]\n', {}, "deck.reserved_ports"),
    ('[deck]\ninstall_source = "git"\n', {}, "deck.install_source"),
    ('[deck]\nstate_dir = "relative/dir"\n', {}, "deck.state_dir"),
    ('[deck]\nname = "' + "x" * 61 + '"\n', {}, "deck.name"),
    ('[network]\nmode = "vpn"\n', {}, "network.mode"),
    ('[network]\ntls = "letsencrypt"\n', {}, "network.tls"),
    ('[network]\nhostname = "https://x.example/"\n', {}, "network.hostname"),
    ('[desks]\ndocker = "yes"\n', {}, "desks.docker"),
    ('[claude]\ntoken_issued = "last spring"\n', {}, "claude.token_issued"),
    ('[doctor]\npublic_probe_urls = "https://x/"\n', {}, "doctor.public_probe_urls"),
    ('[deck\nport = 1\n', {}, "deck.toml"),
    ('deck = 3\n', {}, "deck"),
])
def test_a_bad_value_is_refused_by_key_in_a_sentence(tmp_path, text, env, key):
    with pytest.raises(ConfigError) as err:
        load(_write(tmp_path, text), env=env)
    assert err.value.key == key
    assert key in str(err.value)
    assert "Traceback" not in str(err.value) and str(err.value).endswith(".")


@pytest.mark.parametrize("bind", ["0.0.0.0", "203.0.113.7", "::"])
def test_public_mode_refuses_a_non_loopback_bind(tmp_path, bind):
    """R7: behind Caddy the client IP is read from X-Forwarded-For. That is only
    honest while the deck is unreachable except through Caddy."""
    with pytest.raises(ConfigError) as err:
        load(_write(tmp_path, f'[deck]\nbind = "{bind}"\n'), env={})
    assert err.value.key == "deck.bind"
    with pytest.raises(ConfigError):
        load(tmp_path / "absent.toml", env={"DECK_BIND": bind})


def test_wireguard_and_local_modes_may_bind_elsewhere(tmp_path):
    cfg = load(_write(tmp_path, '[deck]\nbind = "0.0.0.0"\n[network]\nmode = "local"\n'),
               env={})
    assert cfg.deck.bind == "0.0.0.0"


def test_get_refuses_an_unknown_key(tmp_path):
    cfg = load(tmp_path / "absent.toml", env={})
    with pytest.raises(KeyError):
        cfg.get("deck.nope")


# ── an unsearchable parent directory is "no file", not "unreadable file" ─────
# Measured outage: /etc/agent-deck was 0700 root, deck.toml did not exist, the
# deck user's stat() hit EACCES on the parent and load() killed the import.

import os

root_skip = pytest.mark.skipif(os.geteuid() == 0, reason="root can read through chmod 000")


@pytest.fixture
def sealed_default(tmp_path, monkeypatch):
    folder = tmp_path / "agent-deck"
    folder.mkdir()
    (folder / "deck.toml").write_text("[deck]\nport = 9000\n")
    monkeypatch.setattr(deckconfig, "DEFAULT_PATH", str(folder / "deck.toml"))
    folder.chmod(0o000)
    yield folder
    folder.chmod(0o755)


@root_skip
def test_default_path_behind_unsearchable_folder_is_defaults_with_one_warning(
        sealed_default, caplog):
    with caplog.at_level("WARNING"):
        cfg = load(env={})
    assert cfg.deck.port == 7789 and cfg.source is None
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warns) == 1 and "not readable" in warns[0].getMessage()


@root_skip
def test_explicit_deck_config_behind_unsearchable_folder_still_fails(sealed_default):
    with pytest.raises(ConfigError, match="cannot be read"):
        load(env={"DECK_CONFIG": str(sealed_default / "deck.toml")})


@root_skip
def test_explicit_path_argument_behind_unsearchable_folder_still_fails(sealed_default):
    with pytest.raises(ConfigError, match="cannot be read"):
        load(sealed_default / "deck.toml", env={})


@root_skip
def test_existing_but_unreadable_default_file_still_fails(tmp_path, monkeypatch):
    f = tmp_path / "deck.toml"
    f.write_text("[deck]\nport = 9000\n")
    f.chmod(0o000)
    monkeypatch.setattr(deckconfig, "DEFAULT_PATH", str(f))
    try:
        with pytest.raises(ConfigError, match="cannot be read"):
            load(env={})
    finally:
        f.chmod(0o644)
