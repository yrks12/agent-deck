"""K6 detector: the owner's box renders exactly the units it runs today.

`tests/fixtures/owner-box/` holds the three files copied off the live box on
2026-09-30 (`systemctl cat` / `/etc/logrotate.d/agent-deck`) and the deck.toml
that describes that box. The units are now templates rendered from deck.toml by
`deploy/render.py`, so the promise "the owner's box keeps working unchanged" is
exactly this: render the owner profile and every non-comment line comes out
byte-identical, in the same order. Comments may change; nothing systemd or
logrotate reads may.

The same holds for the migration path (K6): a box with units but no deck.toml
gets a deck.toml INFERRED from its live unit, and that inferred profile must
render the same lines -- otherwise the first installer run after the upgrade
would silently rewrite the owner's box.

And the other direction: a stranger's default profile must render nothing of
the owner's (no deckop, no 10.88, no WireGuard ordering, loopback bind).
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "owner-box"
RENDER = ROOT / "deploy" / "render.py"
UNITS = ("agentdeck.service", "deckdoctor.service", "agent-deck-log.logrotate")
OWNERISH = re.compile(r"10\.99\.|deckop|samcarter|initech|/home/deckop\b|wg-quick", re.I)


def _render_module():
    spec = importlib.util.spec_from_file_location("deck_render", RENDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def live_lines(text: str) -> list[str]:
    """What systemd / logrotate read: every non-blank line that is not a comment."""
    return [ln.rstrip("\n") for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith(("#", ";"))]


@pytest.fixture(scope="module")
def render():
    assert RENDER.is_file(), "deploy/render.py does not exist yet"
    return _render_module()


@pytest.mark.parametrize("name", UNITS)
def test_the_owner_profile_renders_the_live_units_line_for_line(render, name):
    out = render.render_all(render.load_config(FIX / "deck.toml"))
    assert name in out, f"render produced no {name}: {sorted(out)}"
    want = live_lines((FIX / name).read_text())
    got = live_lines(out[name])
    assert got == want, f"{name} would change on the owner's box"


def test_the_owner_profile_is_not_a_public_edge(render):
    """The owner's box serves another site on 443 through nginx (measured:
    `ss -ltn` shows 0.0.0.0:443). A Caddyfile there would fight it."""
    out = render.render_all(render.load_config(FIX / "deck.toml"))
    assert "Caddyfile" not in out


@pytest.mark.parametrize("name", UNITS)
def test_a_profile_inferred_from_the_live_unit_renders_the_same_lines(render, name, tmp_path):
    """Auto-migration: no deck.toml, only the running unit. What the installer
    writes must reproduce the box, not a stranger's defaults."""
    toml = render.infer_from_unit((FIX / "agentdeck.service").read_text(), docker=True)
    path = tmp_path / "deck.toml"
    path.write_text(toml)
    cfg = render.load_config(path)
    assert cfg.network.mode == "wireguard"
    assert cfg.network.tls == "none"
    assert cfg.deck.install_source == "rsync"
    out = render.render_all(cfg)
    assert live_lines(out[name]) == live_lines((FIX / name).read_text())


def test_inference_refuses_a_unit_it_cannot_read(render):
    with pytest.raises(ValueError, match="ExecStart"):
        render.infer_from_unit("[Service]\nUser=someone\n", docker=False)


def _stranger(tmp_path):
    """A fresh box: every default, plus the hostname the installer always writes
    (a public edge cannot be rendered without one)."""
    p = tmp_path / "stranger.toml"
    p.write_text('[network]\nhostname = "5-161-10-20.sslip.io"\n')
    return p


def test_the_default_profile_carries_nothing_of_the_owners_box(render, tmp_path):
    cfg = render.load_config(_stranger(tmp_path))
    out = render.render_all(cfg)
    for name in UNITS:
        for line in live_lines(out[name]):
            assert not OWNERISH.search(line), f"{name}: {line}"
    unit = out["agentdeck.service"]
    assert "--host 127.0.0.1 --port 7789" in unit
    assert "User=agentdeck" in unit
    assert "Environment=DECK_URL=http://127.0.0.1:7789" in unit


def test_no_placeholder_survives_rendering(render, tmp_path):
    for cfg in (render.load_config(FIX / "deck.toml"),
                render.load_config(_stranger(tmp_path))):
        for name, text in render.render_all(cfg).items():
            assert not re.search(r"@[A-Z_]+@", text), name


def test_an_unknown_placeholder_is_refused_not_left_in_a_unit(render):
    with pytest.raises(KeyError):
        render.fill("User=@NOT_A_KEY@\n", {"DECK_USER": "x"})


def test_the_cli_renders_into_a_directory(tmp_path):
    """The installer calls it this way, under the box's system python3."""
    done = subprocess.run(
        [sys.executable, str(RENDER), "render", "--config", str(FIX / "deck.toml"),
         "--out", str(tmp_path)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    for name in UNITS:
        assert live_lines((tmp_path / name).read_text()) == live_lines((FIX / name).read_text())
