#!/usr/bin/env python3
"""Render the box's units from deck.toml (easy-setup plan K1, K4, K6).

The systemd units and the logrotate file used to be static files that named the
owner's box (its user, its tunnel address). They are now templates under
deploy/templates/*.in, filled from /etc/agent-deck/deck.toml. The owner's box
keeps running exactly what it runs today: tests/test_render_owner_profile.py
renders tests/fixtures/owner-box/deck.toml and compares every non-comment line
with the units copied off the live box.

Runs under the box's system python3 (3.11+, for tomllib) BEFORE any venv
exists, so stdlib only; it borrows the one validating loader, server/deckconfig.py,
from the tree it ships in.

    render.py render --config deck.toml --out DIR   write every rendered file into DIR
    render.py infer  --unit agentdeck.service [--docker yes|no] [--whatsapp yes|no]
                                                    print a deck.toml that reproduces a live unit
    render.py shell  --config deck.toml             print shell assignments for the installer
    render.py new    --ip A [--tls T] [--hostname H] [--acme-email E] [--docker yes|no]
                     --app-dir D --install-source S print a fresh box's deck.toml
    render.py set    --config deck.toml KEY VALUE   change one key, keep the rest

Templates use @NAME@ placeholders (systemd owns `%`, Caddy owns `{}`). A name
with no value is an error, never an empty string in a unit. A line that is ONLY
a placeholder and renders empty is dropped, which is how optional lines work.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import shlex
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
sys.path.insert(0, str(HERE.parent))

from server import deckconfig  # noqa: E402  (the tree this file ships in)

#: Template -> the name the installer installs it under.
UNITS = {
    "agentdeck.service": "agentdeck.service.in",
    "deckdoctor.service": "deckdoctor.service.in",
    "agent-deck-log.logrotate": "agent-deck-log.logrotate.in",
}

_PLACEHOLDER = re.compile(r"@([A-Z][A-Z0-9_]*)@")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_IFACE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
_UNSAFE = re.compile(r"[\s\"'\\$`;]")


def load_config(path) -> deckconfig.DeckConfig:
    """deck.toml -> validated config. The FILE is the truth for rendering:
    `DECK_PORT` in the installer's environment must not leak into a unit."""
    return deckconfig.load(path, env={})


def _extras(cfg: deckconfig.DeckConfig) -> dict:
    """Keys the renderer reads that deckconfig does not model (it ignores
    unknown keys by contract): `deck.description`, the unit's Description=,
    kept so a migrated box keeps its own words."""
    if cfg.source is None:
        return {}
    try:
        raw = tomllib.loads(Path(cfg.source).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    deck = raw.get("deck", {})
    return {"description": deck.get("description")} if isinstance(deck, dict) else {}


def _url_host(bind: str) -> str:
    if bind in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return f"[{bind}]" if ":" in bind else bind


def values(cfg: deckconfig.DeckConfig) -> dict[str, str]:
    d, n = cfg.deck, cfg.network
    if not _USER.match(d.user):
        raise ValueError(f"deck.user {d.user!r} is not a valid Linux user name")
    for key, val in (("deck.home", d.home), ("deck.app_dir", d.app_dir),
                     ("claude.oauth_env", cfg.claude.oauth_env)):
        if _UNSAFE.search(val) or not val.startswith("/"):
            raise ValueError(f"{key} {val!r} must be an absolute path with no spaces or quotes")
    after = "network-online.target"
    if n.mode == "wireguard":
        if not _IFACE.match(n.wireguard_iface):
            raise ValueError(f"network.wireguard_iface {n.wireguard_iface!r} is not an interface name")
        after += f" wg-quick@{n.wireguard_iface}.service"
    description = _extras(cfg).get("description") or f"{d.name} - the board across your Claude sessions"
    if not isinstance(description, str) or "\n" in description or len(description) > 120:
        raise ValueError("deck.description must be one line of at most 120 characters")
    edge = _edge_values(cfg)
    return {
        **edge,
        "DESCRIPTION": description,
        "APP_DIR": d.app_dir,
        "UNIT_AFTER": after,
        "DECK_USER": d.user,
        "DECK_HOME": d.home,
        "OAUTH_ENV": cfg.claude.oauth_env,
        "DECK_URL": f"http://{_url_host(d.bind)}:{d.port}",
        "BIND": d.bind,
        "PORT": str(d.port),
    }


#: Where the installer mints the self-signed edge's one long-lived key (K10).
TLS_DIR = "/etc/agent-deck/tls"


def has_edge(cfg: deckconfig.DeckConfig) -> bool:
    """Caddy on 443 only for a public box with a certificate Caddy can serve.
    `tunnel` is terminated elsewhere; wireguard/local boxes have no edge."""
    return cfg.network.mode == "public" and cfg.network.tls in ("sslip", "domain", "self-signed")


def _edge_values(cfg: deckconfig.DeckConfig) -> dict[str, str]:
    n = cfg.network
    if not has_edge(cfg):
        return {"SITE": "", "EMAIL_LINE": "", "TLS_LINE": ""}
    if not n.hostname:
        raise ValueError(f"network.hostname is empty, but network.tls = {n.tls!r} serves "
                         "a public edge that needs a name (or an IP) to answer to")
    if n.acme_email and not re.fullmatch(r"[^@\s{}]+@[^@\s{}]+\.[^@\s{}]+", n.acme_email):
        raise ValueError(f"network.acme_email {n.acme_email!r} is not an email address")
    return {
        "SITE": n.hostname,
        "EMAIL_LINE": f"email {n.acme_email}" if n.acme_email else "",
        # A stable key, minted once by the installer: `tls internal` would issue
        # a 12-hour leaf with a fresh key and break the pin every paired Mac holds.
        "TLS_LINE": (f"tls {TLS_DIR}/cert.pem {TLS_DIR}/key.pem"
                     if n.tls == "self-signed" else ""),
    }


def fill(template: str, vals: dict[str, str]) -> str:
    """Substitute @NAME@. Unknown NAME -> KeyError. A line that is only a
    placeholder and comes out empty is removed (optional lines)."""
    out = []
    for line in template.splitlines(keepends=True):
        rendered = _PLACEHOLDER.sub(lambda m: vals[m.group(1)], line)
        if not rendered.strip() and _PLACEHOLDER.fullmatch(line.strip() or "-"):
            continue
        out.append(rendered)
    return "".join(out)


def render_all(cfg: deckconfig.DeckConfig) -> dict[str, str]:
    """{installed file name: content} for this profile."""
    vals = values(cfg)
    out = {name: fill((TEMPLATES / tpl).read_text(encoding="utf-8"), vals)
           for name, tpl in UNITS.items()}
    if has_edge(cfg):
        out["Caddyfile"] = fill((TEMPLATES / "Caddyfile.in").read_text(encoding="utf-8"), vals)
    return out


# ── a fresh box: the installer's answers as a deck.toml ─────────────────────


def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    return addr.version == 4 and addr.is_global and addr not in cgnat


def new_profile(*, ip: str, tls: str, hostname: str, acme_email: str, docker: bool,
                app_dir: str, install_source: str) -> str:
    """deck.toml for a box installed from nothing (K1 defaults + the answers).

    With no --tls: a public IPv4 gets sslip.io (`203-0-113-7.sslip.io`, a real
    certificate, no domain needed); anything else -- a VM behind NAT, CGNAT --
    gets the self-signed edge on its own address, trusted by pin.
    `--tls none` means no edge at all: a local deck on loopback."""
    if not tls:
        tls = "sslip" if _is_public(ip) else "self-signed"
    if tls == "sslip" and not hostname:
        if not _is_public(ip):
            raise ValueError(f"sslip.io needs this box's public IPv4 address; {ip or 'none'!s} "
                             "is not one. Use --tls=self-signed or --tls=domain --hostname <name>")
        hostname = ip.replace(".", "-") + ".sslip.io"
    if tls in ("self-signed",) and not hostname:
        hostname = ip
    if tls in ("domain", "tunnel") and not hostname:
        raise ValueError(f"--tls={tls} needs --hostname <the name that points at this box>")
    mode = "local" if tls == "none" else "public"
    raw = {
        "deck": {"app_dir": app_dir, "install_source": install_source},
        "network": {"mode": mode, "tls": tls, "hostname": hostname if mode == "public" else "",
                    "acme_email": acme_email},
        "desks": {"docker": bool(docker)},
        # The plan-usage meter reads an undocumented endpoint, so the API and the
        # apps label it unofficial. On by default (owner ruling 2026-10-01); an
        # operator turns it off with `deckctl config set claude.usage_meter false`.
        "claude": {"usage_meter": True},
    }
    return ("# Written by the installer on the first run (easy-setup plan K1). Every key\n"
            "# not listed has its default; change one with `deckctl config set`.\n\n"
            + dump_toml(raw))


def set_key(path, dotted: str, value: str) -> None:
    """Change one key in deck.toml, validated, keeping every other line's value
    (comments are not preserved; the file is rewritten whole, atomically)."""
    path = Path(path)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    section, _, key = dotted.partition(".")
    default = deckconfig.DeckConfig().get(dotted)  # KeyError on an unknown key
    if isinstance(default, bool):
        typed = value.lower() in ("1", "true", "yes", "on")
    elif isinstance(default, int):
        typed = int(value)
    elif isinstance(default, tuple):
        items = [v.strip() for v in value.split(",") if v.strip()]
        typed = [int(v) for v in items] if dotted == "deck.reserved_ports" else items
    else:
        typed = value
    raw.setdefault(section, {})[key] = typed
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump_toml(raw), encoding="utf-8")
    try:
        load_config(tmp)  # refuse a value deckconfig would refuse
    except Exception:
        tmp.unlink()
        raise
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ── K6: a deck.toml that reproduces a live unit ─────────────────────────────


def _directives(unit_text: str) -> list[tuple[str, str]]:
    out = []
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, sep, val = line.partition("=")
        if sep:
            out.append((key.strip(), val.strip()))
    return out


_EXEC = re.compile(r"^(/\S+)/\.venv/bin/python -m uvicorn server\.app:app"
                   r" --host (\S+) --port (\d+)$")


def infer_from_unit(unit_text: str, *, docker: bool, whatsapp: bool = False) -> str:
    """The deck.toml for a box that has a working unit and no deck.toml.

    Reads what the unit actually says -- User, ExecStart's app dir, host and
    port, the oauth EnvironmentFile, PATH's home, a wg-quick ordering -- and
    marks it `install_source = "rsync"` and `tls = "none"`: a box set up
    before deck.toml existed was deployed from source and has no public edge.
    `reserved_ports` and `doctor.public_probe_urls` cannot be read off a unit;
    they are left empty for the operator to set (`deckctl config set`).
    """
    kv = _directives(unit_text)
    get = lambda k: [v for key, v in kv if key == k]  # noqa: E731
    execs = [m for m in (_EXEC.match(v) for v in get("ExecStart")) if m]
    if not execs:
        raise ValueError("the unit's ExecStart is not `<app>/.venv/bin/python -m uvicorn "
                         "server.app:app --host H --port P`, so its profile cannot be inferred")
    app_dir, bind, port = execs[0].groups()
    users = get("User")
    if not users:
        raise ValueError("the unit has no User=, so its service user cannot be inferred")
    user = users[0]
    home = f"/home/{user}"
    for env in get("Environment"):
        m = re.match(r"^PATH=([^:]+)/\.local/bin:", env)
        if m:
            home = m.group(1)
    oauth = f"{home}/.claude/oauth.env"
    for f in get("EnvironmentFile"):
        if "oauth" in f:
            oauth = f.lstrip("-")
    wg = re.search(r"wg-quick@([A-Za-z0-9_.-]+)\.service", " ".join(get("After")))
    mode = "wireguard" if wg else "local"
    desc = (get("Description") or [""])[0]
    raw = {
        "deck": {"description": desc, "user": user, "home": home, "app_dir": app_dir,
                 "bind": bind, "port": int(port), "reserved_ports": [],
                 "install_source": "rsync"},
        "network": {"mode": mode, "tls": "none", "hostname": "",
                    "wireguard_iface": wg.group(1) if wg else "wg0"},
        "desks": {"docker": bool(docker)},
        "claude": {"oauth_env": oauth},
        "doctor": {"public_probe_urls": [], "whatsapp": bool(whatsapp)},
    }
    if not desc:
        del raw["deck"]["description"]
    return ("# Inferred from the live agentdeck.service by deploy/render.py (K6).\n"
            "# Not inferable from a unit -- set them if this box has them:\n"
            "#   deck.reserved_ports       ports other services here own\n"
            "#   doctor.public_probe_urls  public sites to check around a docker install\n\n"
            + dump_toml(raw))


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def dump_toml(raw: dict) -> str:
    """Flat [section] key = value TOML -- the only shape deck.toml has."""
    parts = []
    for section, table in raw.items():
        parts.append(f"[{section}]")
        parts += [f"{k} = {_toml_value(v)}" for k, v in table.items()]
        parts.append("")
    return "\n".join(parts)


# ── what the installer reads ────────────────────────────────────────────────


def shell_vars(cfg: deckconfig.DeckConfig) -> str:
    """`eval`-safe assignments: every value passes through shlex.quote."""
    d, n = cfg.deck, cfg.network
    image = cfg.desks.image if ":" in cfg.desks.image.rsplit("/", 1)[-1] else cfg.desks.image + ":1"
    pairs = {
        "DECK_USER": d.user, "DECK_HOME": d.home, "APP_DIR": d.app_dir,
        "STATE_DIR": d.state_dir, "BIND_ADDR": d.bind, "DECK_PORT": str(d.port),
        "RESERVED_PORTS": " ".join(str(p) for p in d.reserved_ports),
        "INSTALL_SOURCE": d.install_source, "NET_MODE": n.mode, "NET_TLS": n.tls,
        "NET_HOSTNAME": n.hostname, "ACME_EMAIL": n.acme_email,
        "WG_IFACE": n.wireguard_iface, "DESKS_DOCKER": "1" if cfg.desks.docker else "0",
        "DESK_IMAGE": image, "CLAUDE_CHANNEL": cfg.claude.channel,
        "OAUTH_ENV": cfg.claude.oauth_env,
        "PROBE_URLS": " ".join(cfg.doctor.public_probe_urls),
        "WHATSAPP": "1" if cfg.doctor.whatsapp else "0",
        "DECK_URL": values(cfg)["DECK_URL"],
        "EDGE_RESOLVE": _edge_resolve(n.hostname),
    }
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in pairs.items())


def _edge_resolve(hostname: str) -> str:
    """curl's --resolve value for probing this box's own edge from the box.

    A NAME is pinned to 127.0.0.1 (public DNS may not hairpin) and still sends
    SNI. An IP literal sends no SNI, so Caddy picks a certificate by the address
    the connection arrived on -- pinning it to 127.0.0.1 finds none and the
    handshake fails. An IP is this box's own address: dial it as it is."""
    if not hostname:
        return ""
    try:
        ipaddress.ip_address(hostname.strip("[]"))
        return ""
    except ValueError:
        return f"{hostname}:443:127.0.0.1"


def _yes(s: str) -> bool:
    return s.lower() in ("1", "yes", "true", "on")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="render.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--config", required=True)
    r.add_argument("--out", required=True)
    i = sub.add_parser("infer")
    i.add_argument("--unit", required=True)
    i.add_argument("--docker", default="no")
    i.add_argument("--whatsapp", default="no")
    s = sub.add_parser("shell")
    s.add_argument("--config", required=True)
    n = sub.add_parser("new")
    n.add_argument("--ip", default="")
    n.add_argument("--tls", default="")
    n.add_argument("--hostname", default="")
    n.add_argument("--acme-email", default="")
    n.add_argument("--docker", default="yes")
    n.add_argument("--app-dir", required=True)
    n.add_argument("--install-source", required=True)
    st = sub.add_parser("set")
    st.add_argument("--config", required=True)
    st.add_argument("key")
    st.add_argument("value")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "render":
            out = Path(a.out)
            out.mkdir(parents=True, exist_ok=True)
            for name, text in render_all(load_config(a.config)).items():
                (out / name).write_text(text, encoding="utf-8")
        elif a.cmd == "infer":
            sys.stdout.write(infer_from_unit(Path(a.unit).read_text(encoding="utf-8"),
                                             docker=_yes(a.docker), whatsapp=_yes(a.whatsapp)))
        elif a.cmd == "shell":
            sys.stdout.write(shell_vars(load_config(a.config)))
        elif a.cmd == "new":
            sys.stdout.write(new_profile(ip=a.ip, tls=a.tls, hostname=a.hostname,
                                         acme_email=a.acme_email, docker=_yes(a.docker),
                                         app_dir=a.app_dir, install_source=a.install_source))
        elif a.cmd == "set":
            set_key(a.config, a.key, a.value)
    except (ValueError, OSError, KeyError) as exc:  # ConfigError is a ValueError
        print(f"render.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
