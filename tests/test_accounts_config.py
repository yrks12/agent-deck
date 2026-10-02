"""S6a: the two-accounts policy and the usage-meter switch live in deck.toml.

`[accounts]` (docs/plans/2026-10-01-two-accounts.md): policy, default,
failover_order, threshold_pct, api_account, failover_allowed. Every default is
the single-account deck: `fixed`, `main`, and auto-failover NOT allowed -- the
owner allowed it on his own box only, after being told the terms are unclear.

`claude.usage_meter` (terms check, claude-dashbaord-oss-terms.md): the meter
reads an undocumented endpoint. Owner ruling 2026-10-01: it is ON by default on
every install, public ones included, and the API and UI keep labelling it
unofficial. An operator turns it off with `deckctl config set claude.usage_meter false`.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

from server import deckconfig

ROOT = Path(__file__).resolve().parent.parent


def _load(tmp_path, text: str):
    p = tmp_path / "deck.toml"
    p.write_text(text)
    return deckconfig.load(p, env={})


def test_accounts_defaults_are_the_single_account_deck(tmp_path):
    cfg = deckconfig.load(tmp_path / "missing.toml", env={})
    a = cfg.accounts
    assert a.policy == "fixed"
    assert a.default == "main"
    assert a.failover_order == ("main",)
    assert a.threshold_pct == 90
    assert a.api_account == ""
    assert a.failover_allowed is False


def test_the_owner_box_profile_loads(tmp_path):
    cfg = _load(tmp_path, '[accounts]\npolicy = "failover"\nfailover_order = ["main", "work"]\n'
                          'threshold_pct = 90\nfailover_allowed = true\n')
    assert cfg.accounts.policy == "failover"
    assert cfg.accounts.failover_order == ("main", "work")
    assert cfg.accounts.failover_allowed is True
    assert cfg.get("accounts.threshold_pct") == 90


@pytest.mark.parametrize("text, key", [
    ('[accounts]\npolicy = "roulette"\n', "accounts.policy"),
    ('[accounts]\nthreshold_pct = 0\n', "accounts.threshold_pct"),
    ('[accounts]\nthreshold_pct = 101\n', "accounts.threshold_pct"),
    ('[accounts]\nfailover_allowed = "yes"\n', "accounts.failover_allowed"),
])
def test_a_bad_accounts_value_names_its_key(tmp_path, text, key):
    with pytest.raises(deckconfig.ConfigError) as err:
        _load(tmp_path, text)
    assert err.value.key == key


def test_usage_meter_defaults_on_where_there_is_no_deck_toml(tmp_path):
    assert deckconfig.load(tmp_path / "missing.toml", env={}).claude.usage_meter is True


def test_usage_meter_can_be_switched_off(tmp_path):
    assert _load(tmp_path, "[claude]\nusage_meter = false\n").claude.usage_meter is False


def test_a_fresh_public_install_writes_the_meter_on():
    spec = importlib.util.spec_from_file_location("deck_render", ROOT / "deploy" / "render.py")
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)
    text = render.new_profile(ip="203.0.113.7", tls="", hostname="", acme_email="",
                              docker=True, app_dir="/opt/agent-deck/current",
                              install_source="release")
    assert tomllib.loads(text)["claude"]["usage_meter"] is True
