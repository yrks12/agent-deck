"""Public mode: a stranger's fresh VPS, reached over HTTPS with no VPN (plan K4, K10).

Two halves:

* the RENDERED Caddyfile (deploy/templates/Caddyfile.in through deploy/render.py),
  asserted by parsing what Caddy will read. It must forward exactly /v1/* and
  /healthz to the loopback deck and 404 everything else (the board and /api stay
  box-local), and it must not cut the two long-lived things the Mac depends on:
  the Mac-bridge long-poll (`POST /v1/nodes/{id}/poll`, held up to 25 s before
  the first byte -- server/mac_api.py) and the SSE streams (`/v1/stream`), which
  send headers at once and then trickle for hours.
* the installer's public-mode phases, asserted structurally (this Mac is not a
  box; the container run in the PR body is the behavioural proof).
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "deploy" / "render.py"
INSTALLER = ROOT / "deploy" / "install-deck.sh"
FIX = ROOT / "tests" / "fixtures" / "owner-box"

#: The Mac bridge holds a poll open this long before answering (server/mac_api.py).
LONG_POLL_S = 25


def _render():
    spec = importlib.util.spec_from_file_location("deck_render", RENDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def render():
    return _render()


def _toml(tmp_path, network: str, deck: str = "") -> Path:
    p = tmp_path / "deck.toml"
    p.write_text(f"[deck]\n{deck}\n[network]\n{network}\n")
    return p


def caddyfile(render, tmp_path, network='tls = "sslip"\nhostname = "203-0-113-7.sslip.io"\n',
              deck=""):
    out = render.render_all(render.load_config(_toml(tmp_path, network, deck)))
    assert "Caddyfile" in out, f"no Caddyfile rendered: {sorted(out)}"
    return out["Caddyfile"]


def code(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#"))


def seconds(value: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
    assert m, f"not a Caddy duration: {value!r}"
    n, unit = float(m.group(1)), m.group(2)
    return n * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


# ── the edge forwards /v1/* and /healthz, and nothing else ──────────────────


def test_only_v1_and_healthz_reach_the_deck(render, tmp_path):
    text = code(caddyfile(render, tmp_path))
    matchers = re.findall(r"^\s*@(\w+)\s+path\s+(.+)$", text, re.M)
    assert matchers, "no path matcher: the proxy would forward everything"
    assert len(matchers) == 1, matchers
    name, paths = matchers[0]
    assert sorted(paths.split()) == ["/healthz", "/v1/*"], paths
    proxies = re.findall(r"reverse_proxy\s+(\S+)", text)
    assert proxies == ["127.0.0.1:7789"], proxies
    # The proxy lives inside `handle @<name>` and the catch-all handle 404s.
    assert re.search(rf"handle\s+@{name}\s*\{{[^}}]*reverse_proxy", text, re.S)
    assert re.search(r"handle\s*\{\s*respond\s+404\s*\}", text), \
        "anything outside /v1 and /healthz must answer 404 (the board, /api)"
    assert "/api" not in text and "file_server" not in text


def test_the_proxy_follows_the_decks_port(render, tmp_path):
    text = caddyfile(render, tmp_path, deck="port = 7801")
    assert re.findall(r"reverse_proxy\s+(\S+)", code(text)) == ["127.0.0.1:7801"]


# ── long-lived requests pass through ───────────────────────────────────────


def test_streams_are_flushed_immediately(render, tmp_path):
    """Buffered SSE arrives in lumps or not at all; -1 flushes every write."""
    assert re.search(r"^\s*flush_interval\s+-1\s*$", caddyfile(render, tmp_path), re.M)


def test_the_bridge_long_poll_outlives_its_hold(render, tmp_path):
    """The poll's first byte comes up to 25 s after the request. A response-
    header timeout at or under that turns every idle poll into a 502."""
    text = code(caddyfile(render, tmp_path))
    rht = re.findall(r"response_header_timeout\s+(\S+)", text)
    assert rht, "response_header_timeout must be stated, not left to a version's default"
    assert seconds(rht[0]) >= 4 * LONG_POLL_S, rht


def test_nothing_times_out_an_open_stream(render, tmp_path):
    """SSE and websockets live for hours. A server `write` timeout or a
    transport `read_timeout` would cut them mid-stream; `idle` only closes
    keep-alive connections between requests, and must still exceed the poll."""
    text = code(caddyfile(render, tmp_path))
    assert not re.search(r"^\s*write\s+\S+", text, re.M), "a write timeout cuts SSE"
    assert not re.search(r"^\s*read_body\s+\S+", text, re.M), \
        "read_body is Go's whole-request ReadTimeout: it would cut a websocket"
    assert not re.search(r"^\s*(read_timeout|write_timeout)\s+\S+", text, re.M)
    idle = re.findall(r"^\s*idle\s+(\S+)", text, re.M)
    assert idle and seconds(idle[0]) > LONG_POLL_S, idle
    hdr = re.findall(r"^\s*read_header\s+(\S+)", text, re.M)
    assert hdr and seconds(hdr[0]) <= 30, "slow-header clients should be cut (slowloris)"


def test_uploads_are_bounded(render, tmp_path):
    assert re.search(r"max_size\s+10MB", caddyfile(render, tmp_path))


def test_the_edge_does_not_announce_itself(render, tmp_path):
    assert re.search(r"^\s*header\s+-Server\s*$", caddyfile(render, tmp_path), re.M)


# ── TLS modes ──────────────────────────────────────────────────────────────


def test_sslip_uses_public_acme_with_the_default_issuer_chain(render, tmp_path):
    text = code(caddyfile(render, tmp_path))
    assert re.search(r"^203-0-113-7\.sslip\.io\s*\{", text, re.M)
    assert not re.search(r"^\s*tls\s", text, re.M), \
        "any tls directive here would replace Caddy's LE -> ZeroSSL fallback chain"


def test_an_acme_email_goes_into_the_global_block(render, tmp_path):
    text = code(caddyfile(render, tmp_path, network=(
        'tls = "domain"\nhostname = "deck.example.com"\nacme_email = "me@example.com"\n')))
    assert re.search(r"^\{\s*\n\s*email\s+me@example\.com\s*$", text, re.M)


def test_no_email_line_when_none_is_set(render, tmp_path):
    assert "email" not in code(caddyfile(render, tmp_path))


def test_self_signed_serves_a_stable_key_so_the_pin_survives_renewal(render, tmp_path):
    """The pairing code pins the leaf SPKI. `tls internal` re-issues a 12-hour
    leaf with a new key, which would break every paired Mac twice a day. The
    installer mints one long-lived key+cert under /etc/agent-deck/tls instead."""
    text = code(caddyfile(render, tmp_path, network='tls = "self-signed"\nhostname = "192.168.64.5"\n'))
    assert "tls internal" not in text
    assert re.search(r"^\s*tls\s+/etc/agent-deck/tls/cert\.pem\s+/etc/agent-deck/tls/key\.pem\s*$",
                     text, re.M)
    assert re.search(r"^192\.168\.64\.5\s*\{", text, re.M)


@pytest.mark.parametrize("network", [
    'mode = "wireguard"\ntls = "none"\nhostname = ""\n',
    'mode = "local"\ntls = "none"\n',
    'tls = "tunnel"\nhostname = "deck.example.com"\n',
])
def test_no_edge_is_rendered_when_nothing_should_listen_on_443(render, tmp_path, network):
    deck = 'bind = "10.9.0.1"' if "wireguard" in network else ""
    out = render.render_all(render.load_config(_toml(tmp_path, network, deck)))
    assert "Caddyfile" not in out


def test_a_public_edge_needs_a_hostname(render, tmp_path):
    with pytest.raises(ValueError, match="hostname"):
        render.render_all(render.load_config(_toml(tmp_path, 'tls = "sslip"\nhostname = ""\n')))


def test_the_owner_box_gets_no_edge(render):
    """It serves another site on 443 through nginx (measured)."""
    assert "Caddyfile" not in render.render_all(render.load_config(FIX / "deck.toml"))


# ── a fresh deck.toml from the installer's answers ─────────────────────────


@pytest.mark.parametrize("ip,tls,host", [
    # A real public address (203.0.113.0/24 in the plan's example is TEST-NET-3,
    # which is not globally routable and correctly gets the pinned edge).
    ("5.161.10.20", "sslip", "5-161-10-20.sslip.io"),
    ("192.168.64.5", "self-signed", "192.168.64.5"),   # no public IP: pin mode
    ("100.64.1.2", "self-signed", "100.64.1.2"),       # CGNAT is not public either
])
def test_new_profile_picks_sslip_only_for_a_public_address(render, tmp_path, ip, tls, host):
    text = render.new_profile(ip=ip, tls="", hostname="", acme_email="", docker=True,
                              app_dir="/opt/agent-deck/current", install_source="release")
    p = tmp_path / "deck.toml"
    p.write_text(text)
    cfg = render.load_config(p)
    assert (cfg.network.mode, cfg.network.tls, cfg.network.hostname) == ("public", tls, host)
    assert cfg.deck.bind == "127.0.0.1" and cfg.deck.user == "agentdeck"


def test_new_profile_honours_an_explicit_domain_and_none(render, tmp_path):
    p = tmp_path / "deck.toml"
    p.write_text(render.new_profile(ip="203.0.113.7", tls="domain", hostname="deck.example.com",
                                    acme_email="a@b.c", docker=False, app_dir="/srv/deck",
                                    install_source="rsync"))
    cfg = render.load_config(p)
    assert (cfg.network.tls, cfg.network.hostname, cfg.desks.docker) == ("domain", "deck.example.com", False)
    p.write_text(render.new_profile(ip="203.0.113.7", tls="none", hostname="", acme_email="",
                                    docker=True, app_dir="/srv/deck", install_source="rsync"))
    assert render.load_config(p).network.mode == "local"


def test_set_changes_one_key_and_keeps_the_rest(render, tmp_path):
    p = tmp_path / "deck.toml"
    p.write_text((FIX / "deck.toml").read_text())
    render.set_key(p, "network.tls", "self-signed")
    cfg = render.load_config(p)
    assert cfg.network.tls == "self-signed"
    assert cfg.deck.reserved_ports == (7788,) and cfg.deck.user == "deckop"
    assert "description" in p.read_text()


# ── the installer's public-mode phases ─────────────────────────────────────


def script() -> str:
    return INSTALLER.read_text()


def code_lines() -> list[str]:
    return [ln for ln in script().splitlines() if ln.strip() and not ln.strip().startswith("#")]


@pytest.mark.parametrize("flag", ["--yes", "--hostname", "--tls=", "--acme-email", "--no-docker",
                                  "--claude-token-file", "--openai-key-file", "--skip-login",
                                  "--profile-from-legacy"])
def test_the_installer_takes_every_k4_flag(flag):
    assert re.search(rf"^\s*{re.escape(flag)}", script(), re.M), flag


def test_a_flag_that_disagrees_with_deck_toml_is_refused_by_key_name():
    text = "\n".join(code_lines())
    assert re.search(r"deck\.toml says .*network\.tls", text) or "disagree" in text
    assert "deckctl config set" in text


def test_the_public_firewall_opens_ssh_first_then_denies_then_443_only():
    lines = code_lines()
    allow = [ln for ln in lines if re.search(r"ufw\s+(--\S+\s+)*allow", ln)]
    public = [ln for ln in allow if " in on " not in ln]
    for ln in public:
        assert re.search(r"allow \"?\$\{?SSH_PORT\}?\"?/tcp|allow 443/tcp", ln), \
            f"a public rule for something other than ssh or 443: {ln}"
        assert "DECK_PORT" not in ln
    ssh = next(i for i, ln in enumerate(lines) if re.search(r"allow \"?\$\{?SSH_PORT", ln))
    deny = next(i for i, ln in enumerate(lines) if "default deny incoming" in ln)
    assert ssh < deny, "deny-incoming before the ssh rule locks the operator out"
    window = "\n".join(lines[max(0, deny - 8):deny + 1])
    assert re.search(r"Status: active", window), "deny is set even when ufw was already active"


def test_ssh_port_is_read_from_sshd_not_assumed():
    assert re.search(r"sshd -T", "\n".join(code_lines()))


def test_the_edge_is_refused_when_443_belongs_to_someone_else():
    text = "\n".join(code_lines())
    assert re.search(r"port_holder 443", text)
    assert re.search(r"caddy", text)


def test_caddy_owns_the_caddyfile_only_if_it_is_ours_or_the_package_default():
    text = "\n".join(code_lines())
    assert "dpkg-query" in text and "Conffiles" in text
    assert "managed by agent-deck" in (ROOT / "deploy" / "templates" / "Caddyfile.in").read_text()


def test_acme_failure_falls_back_to_the_pinned_self_signed_edge_and_says_so():
    text = script()
    assert re.search(r"set .*network\.tls self-signed|set_key.*self-signed|network\.tls\s+self-signed", text)
    assert re.search(r"120", "\n".join(code_lines())), "no bounded wait for the certificate"


def test_the_self_signed_key_is_minted_once_and_its_pin_recorded():
    text = "\n".join(code_lines())
    gen = [i for i, ln in enumerate(code_lines()) if "openssl req" in ln]
    assert gen, "no self-signed certificate is minted"
    assert "tls-pin" in text
    assert re.search(r"openssl dgst -sha256 -binary", text)


def test_the_service_user_is_created_as_a_system_user_only_for_a_fresh_box():
    lines = code_lines()
    idx = [i for i, ln in enumerate(lines) if "useradd" in ln]
    assert idx and "--system" in lines[idx[0]] and "--create-home" in lines[idx[0]]


def test_skip_login_keeps_every_other_preflight_blocker():
    """--skip-login drops exactly the login blocker, computed by the judge
    (preflight with claude_authenticated=True), never by matching its words."""
    text = "\n".join(code_lines())
    assert re.search(r"claude_authenticated.{0,20}True", text)


def test_no_secret_goes_through_argv():
    """curl -H "Authorization: Bearer $KEY" puts the key in `ps` for everyone."""
    for ln in code_lines():
        if "OPENAI" in ln.upper() and "curl" in ln:
            assert "@" in ln and "$OPENAI" not in ln, ln


# ── deploy/install.sh: the curl | sudo bash bootstrap (K4) ─────────────────
#
# Run for real against a local release (file:// manifest + tarball), with `id`
# stubbed to say root and the release root moved into tmp_path. The tarball's
# deploy/install-deck.sh is a stand-in that records its argv, so what is
# asserted is exactly what the bootstrap hands on.

import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import tarfile  # noqa: E402

BOOTSTRAP = ROOT / "deploy" / "install.sh"


def _release(tmp_path, version="0.9.0", corrupt=False):
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    root = f"agent-deck-server-{version}"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = b'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$DECK_ARGV_OUT"\n'
        info = tarfile.TarInfo(f"{root}/deploy/install-deck.sh")
        info.size, info.mode = len(body), 0o755
        tar.addfile(info, io.BytesIO(body))
        ver = version.encode()
        info = tarfile.TarInfo(f"{root}/VERSION")
        info.size = len(ver)
        tar.addfile(info, io.BytesIO(ver))
    data = buf.getvalue()
    tgz = dist / f"{root}.tar.gz"
    tgz.write_bytes(data)
    sha = hashlib.sha256(b"not it" if corrupt else data).hexdigest()
    (dist / "manifest.json").write_text(json.dumps({
        "version": version, "server": {"url": tgz.as_uri(), "sha256": sha}}))
    return dist


def _bootstrap(tmp_path, dist, *args, os_release='ID=ubuntu\nVERSION_ID="24.04"\n'):
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "id").write_text('#!/bin/sh\n[ "$1" = "-u" ] && echo 0 || /usr/bin/id "$@"\n')
    (stubs / "id").chmod(0o755)
    osr = tmp_path / "os-release"
    osr.write_text(os_release)
    env = {"PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path),
           "DECK_RELEASE_BASE": dist.as_uri(), "DECK_PREFIX": str(tmp_path / "opt"),
           "DECK_OS_RELEASE": str(osr), "DECK_ARGV_OUT": str(tmp_path / "argv")}
    return subprocess.run(["bash", str(BOOTSTRAP), *args], env=env, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=60)


def test_the_bootstrap_is_short_enough_to_read_before_running():
    lines = BOOTSTRAP.read_text().splitlines()
    assert len(lines) <= 150, len(lines)
    assert subprocess.run(["bash", "-n", str(BOOTSTRAP)]).returncode == 0


def test_the_bootstrap_verifies_unpacks_links_and_hands_on_every_flag(tmp_path):
    dist = _release(tmp_path)
    done = _bootstrap(tmp_path, dist, "--yes", "--tls=self-signed", "--hostname", "h.example")
    assert done.returncode == 0, done.stderr + done.stdout
    rel = tmp_path / "opt" / "releases" / "0.9.0"
    assert (rel / "deploy" / "install-deck.sh").is_file()
    assert (tmp_path / "opt" / "current").resolve() == rel.resolve()
    assert (tmp_path / "argv").read_text().split() == ["--yes", "--tls=self-signed", "--hostname", "h.example"]


def test_a_tarball_whose_sha256_does_not_match_is_refused_and_nothing_is_unpacked(tmp_path):
    dist = _release(tmp_path, corrupt=True)
    done = _bootstrap(tmp_path, dist, "--yes")
    assert done.returncode != 0
    assert "sha256" in (done.stderr + done.stdout).lower()
    assert not (tmp_path / "opt" / "releases" / "0.9.0").exists()
    assert not (tmp_path / "opt" / "current").exists()


def test_ubuntu_22_04_is_refused_with_the_reason(tmp_path):
    dist = _release(tmp_path)
    done = _bootstrap(tmp_path, dist, "--yes", os_release='ID=ubuntu\nVERSION_ID="22.04"\n')
    assert done.returncode != 0
    assert "3.11" in done.stderr and "24.04" in done.stderr


def test_no_terminal_and_no_yes_is_refused_rather_than_hanging_on_a_prompt(tmp_path):
    dist = _release(tmp_path)
    done = _bootstrap(tmp_path, dist)
    assert done.returncode != 0 and "--yes" in done.stderr


def test_a_rerun_keeps_the_installed_release_and_does_not_repoint_current(tmp_path):
    """Moving `current` to a newer release is `deckctl update` (with rollback),
    never a side effect of re-running the one-liner."""
    assert _bootstrap(tmp_path, _release(tmp_path, "0.9.0"), "--yes").returncode == 0
    done = _bootstrap(tmp_path, _release(tmp_path, "0.9.1"), "--yes")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "opt" / "current").resolve().name == "0.9.0"
    assert "deckctl update" in done.stdout + done.stderr


def test_installer_hands_the_deck_the_paths_it_writes_inside_the_unpacked_release():
    """A release unpacks root-owned. The deck writes its log (and cache) under
    the app dir, so the installer must create and chown them for the deck user."""
    text = INSTALLER.read_text()
    assert re.search(r'touch\s+"\$\{?REPO_DIR\}?/\.agent-deck\.log"', text)
    assert re.search(r'mkdir -p\s+"\$\{?REPO_DIR\}?/\.cache"', text)
    assert re.search(
        r'chown\s+"\$\{DECK_USER\}:\$\{DECK_USER\}"\s+"\$\{REPO_DIR\}/\.agent-deck\.log"\s+"\$\{REPO_DIR\}/\.cache"',
        text)


# ── the installer's own edge probe works on an IP-named box ────────────────
# MEASURED on a fresh Ubuntu 24.04 container (2026-09-30): a box with no public
# IPv4 gets the self-signed edge named by its IP. `curl --resolve 172.17.0.2:443:
# 127.0.0.1 https://172.17.0.2/...` sends no SNI and lands on 127.0.0.1, which
# Caddy has no certificate for: the handshake fails, and the install ended in
# "verification failed" on a box whose edge answered fine at its own address.


@pytest.mark.parametrize("host,want", [
    ("172.17.0.2", ""),
    ("5-161-10-20.sslip.io", "5-161-10-20.sslip.io:443:127.0.0.1"),
    ("deck.example.com", "deck.example.com:443:127.0.0.1"),
])
def test_the_edge_probe_pins_a_name_to_loopback_but_dials_an_ip_directly(render, tmp_path, host, want):
    tls = "self-signed" if host[0].isdigit() else "domain"
    cfg = render.load_config(_toml(tmp_path, f'tls = "{tls}"\nhostname = "{host}"\n'))
    lines = dict(ln.split("=", 1) for ln in render.shell_vars(cfg).splitlines())
    assert "EDGE_RESOLVE" in lines
    assert lines["EDGE_RESOLVE"].strip("'") == want


def test_the_installer_probes_the_edge_through_edge_resolve_only():
    text = INSTALLER.read_text()
    assert '--resolve "${NET_HOSTNAME}:443:127.0.0.1"' not in text
    assert "EDGE_RESOLVE" in text
