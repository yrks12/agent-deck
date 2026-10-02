"""K7: the server doctor behind `deckctl doctor`.

Every check is `Check(id, ok, says, fix)`: `says` is one plain sentence, `fix` is
one plain instruction (empty on a clean PASS). `ok=True` with a non-empty `fix` is
a warning (or, for "update available", an info line); `ok=False` is a failure.
No check raises and none prints a stack trace: a question the host cannot answer
becomes a plain failure that says so.

Everything the doctor asks of the machine goes through one `Host` object, so the
tests hand it a fake and this module never needs root, docker or a network to be
exercised. `Host` is the real one.

It is deliberately separate from `bin/deckdoctor`, the 10-minute alarm on the
owner's box: that one speaks to the chief and keeps its own JSON contract; this
one answers a person who just installed the deck and asks "is it working?".
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import urllib.error
import urllib.request
from typing import Callable, NamedTuple

from server import deckconfig

__all__ = ["Check", "Host", "run_checks", "core_checks", "render", "newer",
           "UNIT", "LOGIN_CACHE_SECONDS"]

UNIT = "agentdeck"
LOGIN_CACHE_SECONDS = 6 * 3600
TOKEN_WARN_DAYS = 330
CERT_WARN_DAYS = 14
DISK_RED_PCT = 85
MIN_RAM_GB = 3.5
CORE = ("service", "local")


class Check(NamedTuple):
    id: str
    ok: bool
    says: str
    fix: str


def _pass(cid: str, says: str) -> Check:
    return Check(cid, True, says, "")


def _warn(cid: str, says: str, fix: str) -> Check:
    return Check(cid, True, says, fix)


def _fail(cid: str, says: str, fix: str) -> Check:
    return Check(cid, False, says, fix)


def newer(candidate: str, current: str) -> bool:
    """True when `candidate` is a strictly higher dotted version than `current`."""
    def parts(v: str):
        m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(v).strip())
        return tuple(int(x) for x in m.groups()) if m else None
    a, b = parts(candidate), parts(current)
    return a is not None and b is not None and a > b


# -- the real host -----------------------------------------------------------


def _run(argv: list[str], timeout: float = 15) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()


def claude_bin(cfg: deckconfig.DeckConfig) -> str:
    """Where the installer puts the service user's `claude` (its own installer's
    default). Called by path: sudo's secure_path and root's PATH do not include
    ~/.local/bin, so a bare `claude` there is "not found" on a working box."""
    return f"{cfg.deck.home}/.local/bin/claude"


class Host:
    """The one place the doctor touches the machine."""

    def __init__(self, cfg: deckconfig.DeckConfig, *, env: dict | None = None,
                 run: Callable[[list[str], float], tuple[int, str]] | None = None) -> None:
        self.cfg = cfg
        self.env = dict(os.environ if env is None else env)
        self._run = run or (lambda argv, timeout=15: _run(argv, timeout))

    def now(self) -> float:
        import time
        return time.time()

    def today(self) -> str:
        return _dt.date.today().isoformat()

    def version(self) -> str:
        from server import pair_api
        return pair_api.read_version(__import__("pathlib").Path(self.cfg.deck.app_dir))

    def config_folder_blocked(self) -> bool:
        """True when the deck user cannot open the folder deck.toml lives in."""
        import grp
        import pwd
        folder = os.path.dirname(deckconfig.DEFAULT_PATH)
        try:
            st = os.stat(folder)
            user = pwd.getpwnam(self.cfg.deck.user)
        except (OSError, KeyError):
            return False          # no folder, or no such user yet: nothing to report
        if st.st_mode & 0o001:
            return False
        if st.st_uid == user.pw_uid:
            return not st.st_mode & 0o100
        in_group = st.st_gid == user.pw_gid or \
            user.pw_name in grp.getgrgid(st.st_gid).gr_mem
        return not (in_group and st.st_mode & 0o010)

    def active(self, unit: str) -> bool:
        return self._run(["systemctl", "is-active", "--quiet", unit], 10)[0] == 0

    def get(self, url: str, timeout: float = 5) -> int | None:
        ctx = None
        if url.startswith("https://") and self.cfg.network.tls == "self-signed":
            ctx = ssl._create_unverified_context()  # its own certificate, by design
        try:
            with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            return err.code
        except (OSError, ValueError):
            return None

    def listeners(self, port: int) -> list[str]:
        code, out = self._run(["ss", "-H", "-ltn", f"sport = :{port}"], 10)
        found = []
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 4:
                addr = cols[3].rsplit(":", 1)[0].strip("[]")
                if addr not in ("127.0.0.1", "::1"):
                    found.append(addr)
        return found

    def cert_days(self, host: str) -> float | None:
        code, pem = self._run(["sh", "-c",
                               f"echo | openssl s_client -connect {host}:443 -servername {host} "
                               "2>/dev/null | openssl x509 -noout -enddate"], 20)
        m = re.search(r"notAfter=(.+)", pem)
        if code != 0 or not m:
            return None
        end = _dt.datetime.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z")
        return (end - _dt.datetime.utcnow()).total_seconds() / 86400

    def resolve(self, host: str) -> list[str]:
        return sorted({i[4][0] for i in socket.getaddrinfo(host, 443, socket.AF_INET)})

    def public_ip(self) -> str | None:
        code, out = self._run(["curl", "-fsS", "--max-time", "5", "https://api.ipify.org"], 10)
        return out if code == 0 and re.match(r"^\d+\.\d+\.\d+\.\d+$", out) else None

    def ufw_ports(self) -> set[int] | None:
        code, out = self._run(["ufw", "status"], 10)
        if code != 0 or "inactive" in out.lower():
            return None
        return {int(m.group(1)) for m in re.finditer(r"^(\d+)(?:/tcp)?\s+ALLOW", out, re.M)}

    def have(self, cmd: str) -> bool:
        if cmd == "claude" and os.access(claude_bin(self.cfg), os.X_OK):
            return True
        return shutil.which(cmd) is not None

    def claude_login(self) -> tuple[int, str]:
        user = self.cfg.deck.user
        # The CLI's own login (`.credentials.json`), never a token the deck
        # keeps: Agent Deck stores no Claude credential (terms check).
        return self._run(["sudo", "-u", user, "-H", claude_bin(self.cfg), "-p",
                          "reply with the single word ok"], 90)

    def docker_ok(self) -> bool:
        return self._run(["docker", "info"], 20)[0] == 0

    def docker_image(self, name: str) -> bool:
        return self._run(["docker", "image", "inspect", name], 20)[0] == 0

    def disk_pct(self) -> int:
        u = shutil.disk_usage("/")
        return -(-100 * u.used // (u.used + u.free))

    def mem_gb(self) -> float:
        with open("/proc/meminfo") as fh:
            kb = int(re.search(r"MemTotal:\s+(\d+)", fh.read()).group(1))
        return kb / 1024 / 1024

    def clock_synced(self) -> bool | None:
        code, out = self._run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], 10)
        return out.strip() == "yes" if code == 0 else None

    def openai_key(self) -> str | None:
        try:
            for line in open("/etc/agent-deck/agentdeck.env", encoding="utf-8"):
                if line.startswith("OPENAI_API_KEY=") and line.strip().split("=", 1)[1]:
                    return line.strip().split("=", 1)[1].strip("'\"")
        except OSError:
            pass
        return None

    def openai_ok(self, key: str) -> bool | None:
        req = urllib.request.Request("https://api.openai.com/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=10):
                return True
        except urllib.error.HTTPError as err:
            return False if err.code in (401, 403) else None
        except OSError:
            return None

    def latest_version(self) -> str | None:
        base = self.cfg.update.base_url
        if not base:
            return None
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/manifest.json", timeout=8) as r:
                return str(json.loads(r.read().decode()).get("version") or "") or None
        except (OSError, ValueError):
            return None

    def ssh_password(self) -> bool | None:
        code, out = self._run(["sshd", "-T"], 10)
        if code != 0:
            return None
        return bool(re.search(r"^passwordauthentication yes", out, re.M))


# -- the checks --------------------------------------------------------------


def _local_url(cfg: deckconfig.DeckConfig) -> str:
    bind = cfg.deck.bind
    if bind in ("0.0.0.0", "::", ""):
        bind = "127.0.0.1"
    return f"http://{bind}:{cfg.deck.port}/healthz"


def _public(cfg: deckconfig.DeckConfig) -> bool:
    return cfg.network.mode == "public"


def _has_certificate(cfg: deckconfig.DeckConfig) -> bool:
    return _public(cfg) and cfg.network.tls in ("sslip", "domain", "self-signed") \
        and bool(cfg.network.hostname)


def _service(cfg, host, state):
    if host.active(UNIT):
        return _pass("service", "Agent Deck is running.")
    return _fail("service", "Agent Deck is not running.",
                 f"Fix: run  sudo systemctl start {UNIT}  and, if it stops again, read "
                 f"journalctl -u {UNIT} -n 30.")


def _config_folder(cfg, host, state):
    blocked = getattr(host, "config_folder_blocked", None)
    if blocked is None:
        return None
    if not blocked():
        return _pass("config_folder", "The config folder can be read by the deck user.")
    return _fail("config_folder", "The config folder is not readable by the deck user.",
                 "Fix: run  sudo chmod 755 /etc/agent-deck  (the settings file inside it "
                 "keeps its own permissions).")


def _local(cfg, host, state):
    if host.get(_local_url(cfg)) == 200:
        return _pass("local", "Agent Deck answers on this machine.")
    return _fail("local", "Agent Deck is not answering on this machine.",
                 f"Fix: run  sudo systemctl restart {UNIT}  and check again in a few seconds.")


def _exposure(cfg, host, state):
    open_on = host.listeners(cfg.deck.port)
    if not open_on:
        return _pass("exposure", "Agent Deck is only reachable through Caddy, not directly.")
    return _fail("exposure",
                 f"Agent Deck is listening on {', '.join(open_on)}, where anyone on the network can reach it.",
                 "Fix: run  sudo deckctl config set deck.bind 127.0.0.1  so only Caddy can reach it.")


def _caddy(cfg, host, state):
    if host.active("caddy"):
        return _pass("caddy", "Caddy, which gives the deck its secure address, is running.")
    return _fail("caddy", "Caddy is not running, so nothing outside can reach the deck.",
                 "Fix: run  sudo systemctl start caddy.")


def _cert(cfg, host, state):
    name = cfg.network.hostname
    days = host.cert_days(name)
    if days is None:
        return _fail("cert", f"There is no working certificate for {name}.",
                     "Fix: run  sudo systemctl restart caddy  and read journalctl -u caddy -n 30.")
    if days < 0:
        return _fail("cert", f"The certificate for {name} has expired.",
                     "Fix: run  sudo systemctl restart caddy  to renew it.")
    if days < CERT_WARN_DAYS:
        return _warn("cert", f"The certificate for {name} expires in {int(days)} days.",
                     "Fix: run  sudo systemctl restart caddy  so it renews.")
    return _pass("cert", f"The certificate for {name} is valid for {int(days)} more days.")


def _dns(cfg, host, state):
    name = cfg.network.hostname
    want = host.public_ip()
    try:
        got = host.resolve(name)
    except OSError:
        got = []
    if not got:
        return _fail("dns", f"The address {name} does not resolve to anything.",
                     "Fix: point its DNS record at this machine's public address"
                     + (f" ({want})." if want else "."))
    if want and want not in got:
        return _fail("dns", f"{name} points at {', '.join(got)}, not at this machine ({want}).",
                     f"Fix: change its DNS record to {want}.")
    return _pass("dns", f"{name} points at this machine.")


def _hairpin(cfg, host, state):
    name = cfg.network.hostname
    if host.get(f"https://{name}/healthz") == 200:
        return _pass("hairpin", f"https://{name} answers.")
    return _fail("hairpin", f"https://{name} did not answer from this machine.",
                 "Fix: check that ports 80 and 443 are open at your provider, then run  "
                 "sudo systemctl restart caddy.")


def _firewall(cfg, host, state):
    ports = host.ufw_ports()
    if ports is None:
        return _warn("firewall", "The firewall (ufw) is off, so every port is open.",
                     "Fix: run  sudo ufw allow 22/tcp && sudo ufw allow 443/tcp && sudo ufw enable.")
    extra = sorted(ports - {22, 80, 443})
    if extra:
        return _fail("firewall", f"The firewall also lets in port {', '.join(map(str, extra))}.",
                     "Fix: run  sudo ufw delete allow " + str(extra[0]) + "  so only 22 and 443 stay open.")
    return _pass("firewall", "Only SSH and the secure web port are open.")


def _claude_cli(cfg, host, state):
    if host.have("claude"):
        return _pass("claude_cli", "The Claude command is installed.")
    return _fail("claude_cli", "The Claude command is not installed, so no agent can start.",
                 f"Fix: install Claude Code for the {cfg.deck.user} user (the installer does this), "
                 "then run  sudo deckctl login.")


def _claude_login(cfg, host, state):
    now = host.now()
    probe = state.get("probe") or {}
    if probe.get("ok") and now - float(probe.get("ts") or 0) < LOGIN_CACHE_SECONDS:
        return _pass("claude_login", "Claude is signed in and answering.")
    code, _out = host.claude_login()
    state["probe"] = {"ts": now, "ok": code == 0}
    if code == 0:
        return _pass("claude_login", "Claude is signed in and answering.")
    return _fail("claude_login", "Your Claude login has expired, so no agent can start.",
                 "Fix: run  sudo deckctl login  and follow the link.")


def _token_age(cfg, host, state):
    issued = cfg.claude.token_issued
    if not issued:
        return None
    age = (_dt.date.fromisoformat(host.today()) - _dt.date.fromisoformat(issued[:10])).days
    if age >= TOKEN_WARN_DAYS:
        return _warn("token_age", f"Your Claude login is {age} days old and will stop working soon.",
                     "Fix: run  sudo deckctl login  to get a fresh one.")
    return _pass("token_age", f"Your Claude login is {age} days old.")


def _docker(cfg, host, state):
    if not cfg.desks.docker:
        return None
    if not host.docker_ok():
        return _fail("docker", "Docker is not running, so agents cannot get their own computer.",
                     "Fix: run  sudo systemctl start docker.")
    if not host.docker_image(cfg.desks.image):
        return _fail("docker", f"The agent computer image {cfg.desks.image} is missing.",
                     "Fix: run the installer again to build it:  sudo /opt/agent-deck/current/deploy/install-deck.sh.")
    return _pass("docker", "Docker is running and the agent computer image is ready.")


def _disk(cfg, host, state):
    pct = host.disk_pct()
    if pct >= DISK_RED_PCT:
        return _fail("disk", f"The disk is {pct}% full.",
                     "Fix: free some space, for example  sudo journalctl --vacuum-size=200M  or  docker system prune.")
    return _pass("disk", f"The disk is {pct}% full.")


def _ram(cfg, host, state):
    gb = host.mem_gb()
    if gb < MIN_RAM_GB:
        return _fail("ram", f"This machine has {gb:.1f} GB of memory and agents need at least {MIN_RAM_GB:g}.",
                     "Fix: move to a server with 4 GB or more.")
    return _pass("ram", f"This machine has {gb:.1f} GB of memory.")


def _clock(cfg, host, state):
    synced = host.clock_synced()
    if synced is False:
        return _fail("clock", "This machine's clock is not in sync, which breaks certificates and pairing codes.",
                     "Fix: run  sudo timedatectl set-ntp true.")
    return _pass("clock", "The clock is in sync." if synced else "The clock could not be checked.")


def _openai(cfg, host, state):
    key = host.openai_key()
    if not key:
        return None
    ok = host.openai_ok(key)
    if ok is False:
        return _fail("openai", "OpenAI rejected the key, so voice and calls will not work.",
                     "Fix: run  sudo deckctl set-openai-key  with a working key.")
    if ok is None:
        return _warn("openai", "OpenAI could not be reached to check the key.",
                     "Fix: check this machine's internet, then run  sudo deckctl doctor  again.")
    return _pass("openai", "The OpenAI key works.")


def _update(cfg, host, state):
    latest = host.latest_version()
    if not latest:
        return None
    if newer(latest, host.version()):
        return _warn("update", f"Agent Deck {latest} is available; you have {host.version()}.",
                     "Fix: run  sudo deckctl update.")
    return _pass("update", "Agent Deck is up to date.")


def _ssh_password(cfg, host, state):
    if host.ssh_password():
        return _warn("ssh_password", "Anyone can try to guess this machine's SSH password.",
                     "Fix: use SSH keys and set PasswordAuthentication no in /etc/ssh/sshd_config.")
    return _pass("ssh_password", "SSH password login is off.")


def _plan(cfg: deckconfig.DeckConfig):
    public = _public(cfg)
    edge = public and cfg.network.tls not in ("none", "tunnel")
    steps = [_service, _local, _config_folder]
    if cfg.network.mode != "wireguard":
        steps.append(_exposure)
    if edge:
        steps.append(_caddy)
    if _has_certificate(cfg):
        steps.append(_cert)
    if edge and cfg.network.tls in ("sslip", "domain") and cfg.network.hostname:
        steps.append(_dns)
    if edge and cfg.network.hostname:
        steps.append(_hairpin)
    if public:
        steps.append(_firewall)
    steps += [_claude_cli, _claude_login, _token_age, _docker, _disk, _ram, _clock,
              _openai, _update, _ssh_password]
    return steps


def _guard(step, cfg, host, state) -> Check | None:
    cid = step.__name__.lstrip("_")
    try:
        return step(cfg, host, state)
    except Exception:  # noqa: BLE001 -- a check must never take the doctor down
        return _fail(cid, f"The {cid.replace('_', ' ')} check could not be completed.",
                     "Fix: run  sudo deckctl doctor --json  and send the output to whoever set this up.")


def run_checks(cfg: deckconfig.DeckConfig, host, state: dict) -> list[Check]:
    """Every check that applies to this install, in the documented order."""
    done = (_guard(s, cfg, host, state) for s in _plan(cfg))
    return [c for c in done if c is not None]


def core_checks(cfg: deckconfig.DeckConfig, host) -> list[Check]:
    """Just "is it up and answering": what `deckctl update` judges a release by."""
    return [_guard(s, cfg, host, {}) for s in (_service, _local)]


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        tag = "FAIL" if not c.ok else ("WARN" if c.fix else "PASS")
        lines.append(f"{tag}  {c.says}")
        if c.fix:
            lines.append(f"      {c.fix}")
    return "\n".join(lines)
