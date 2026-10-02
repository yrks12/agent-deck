"""K1: the deck's one non-secret config file, `/etc/agent-deck/deck.toml`.

Every value that used to be the owner's box baked into a unit file or a
constant (the service user, the WireGuard address, a reserved port) becomes a key here with a
default. **A missing file is not an error and not a guess: it means every
default** -- which is exactly what the owner's Mac daemon gets, because nothing
on the Mac has ever read this file.

Rules this module keeps (and the tests pin):

* `load()` is pure apart from reading the one file. It never creates, writes
  or chmods anything.
* A bad value raises `ConfigError(key, reason)` whose `str()` is one plain
  sentence naming the key -- `deckctl` and the doctor print it as-is.
* Unknown keys are ignored. `deckctl update` can roll back to an older server;
  if a newer release added a key, the older one must still start.
* `network.mode = "public"` with a non-loopback `deck.bind` is refused (R7):
  in public mode the rate limiter trusts `X-Forwarded-For` from loopback,
  which is honest only while nothing but Caddy can reach the deck.

The secret half (`AGENT_DECK_TOKEN`, `OPENAI_API_KEY`) lives in
`/etc/agent-deck/agentdeck.env`, which this module never reads.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import os
import re
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

__all__ = ["DEFAULT_PATH", "ConfigError", "DeckConfig", "load"]

DEFAULT_PATH = "/etc/agent-deck/deck.toml"

_log = logging.getLogger(__name__)

_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class ConfigError(ValueError):
    """A deck.toml value Shaliach cannot use. `str()` is the whole message."""

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"{key} {reason}.")


# ── the sections ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeckSection:
    name: str = "Shaliach"
    user: str = "agentdeck"
    home: str = "/home/agentdeck"
    app_dir: str = "/opt/agent-deck/current"
    state_dir: str = "/var/lib/agent-deck"
    bind: str = "127.0.0.1"
    port: int = 7789
    reserved_ports: tuple[int, ...] = ()
    install_source: str = "release"


@dataclass(frozen=True)
class NetworkSection:
    mode: str = "public"
    tls: str = "sslip"
    hostname: str = ""
    acme_email: str = ""
    wireguard_iface: str = "wg0"


@dataclass(frozen=True)
class DesksSection:
    docker: bool = True
    image: str = "agent-deck/desk-computer"
    # One sign-in on any desk's screen signs every desk's browser in
    # (server/login_vault.py). False keeps each desk's logins to itself.
    shared_logins: bool = True
    # Sites whose logins stay on the desk they were made on -- the "this desk
    # only" opt-out (server/login_vault.py). A bare host ("example.com")
    # matches it and its subdomains; nothing in this list is ever read off a
    # desk or pushed to one by the live sync.
    login_sync_exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaudeSection:
    channel: str = "stable"
    oauth_env: str = "/home/agentdeck/.claude/oauth.env"
    token_issued: str = ""
    # The plan-usage meter reads an UNDOCUMENTED Anthropic endpoint
    # (server/sources/usage.py), so the API and the apps label it unofficial.
    # On by default everywhere (owner ruling 2026-10-01), public installs too.
    usage_meter: bool = True


@dataclass(frozen=True)
class AccountsSection:
    # docs/plans/2026-10-01-two-accounts.md. `fixed`: a desk stays on its
    # account. `failover`: an idle desk moves off an account at
    # `threshold_pct` of a window. `failover_api`: and on to `api_account`
    # when every subscription is spent.
    policy: str = "fixed"
    default: str = "main"
    failover_order: tuple[str, ...] = ("main",)
    threshold_pct: int = 90
    api_account: str = ""
    # Auto-failover is OFF unless this box's owner turned it on knowing the
    # terms question (owner ruling 2026-10-01). `policy` alone cannot move a desk.
    failover_allowed: bool = False


@dataclass(frozen=True)
class DoctorSection:
    public_probe_urls: tuple[str, ...] = ()
    whatsapp: bool = False


@dataclass(frozen=True)
class NotifySection:
    # Owner ruling 2026-09-30: the app is where he is told things; WhatsApp is
    # for urgent only. A blocking ask left unanswered in the app this long is
    # urgent and goes to WhatsApp once. `DECK_QUIET_SECONDS` still overrides.
    ask_urgent_after_minutes: int = 30
    # At most one urgent WhatsApp per this many minutes, whatever raised it.
    urgent_gap_minutes: int = 10
    # Pushes (ntfy, the iPhone, the Mac) wait out this window, box-local
    # time, e.g. "22:00-07:00". Empty is off. `DECK_QUIET_HOURS` overrides.
    quiet_hours: str = ""


@dataclass(frozen=True)
class OwnerSection:
    # What desks call the one person this deck belongs to, in briefs, calls
    # and receipts (server/owner.py). Empty means the neutral "the owner".
    name: str = ""
    # The id his messages carry on the wire and in history. Empty is "owner".
    handle: str = ""


@dataclass(frozen=True)
class UpdateSection:
    # Empty until Owner decision D2 names where releases live. `deckctl update`
    # treats "" as "no release channel configured".
    base_url: str = ""
    channel: str = "stable"


@dataclass(frozen=True)
class EventsSection:
    # Event wake-ups (server/events.py, server/event_pollers.py). Every source
    # the deck polls itself is off until it is turned on here.
    window_seconds: int = 60
    poll_seconds: int = 120
    deploy_poll: bool = False
    deploy_repos: tuple[str, ...] = ()
    gmail_poll: bool = False
    stripe_poll: bool = False
    # The webhook-only listener (server/stripe_hook.py). 0 = not started.
    stripe_port: int = 0
    stripe_bind: str = "127.0.0.1"


_SECTIONS = {
    "deck": DeckSection, "network": NetworkSection, "desks": DesksSection,
    "claude": ClaudeSection, "doctor": DoctorSection, "update": UpdateSection,
    "notify": NotifySection, "owner": OwnerSection, "accounts": AccountsSection,
    "events": EventsSection,
}

_CHOICES = {
    "deck.install_source": ("release", "rsync"),
    "network.mode": ("public", "wireguard", "local"),
    "network.tls": ("sslip", "domain", "self-signed", "tunnel", "none"),
    "accounts.policy": ("fixed", "failover", "failover_api"),
}
_ABSOLUTE = {"deck.home", "deck.app_dir", "deck.state_dir"}
_MINUTES = {"notify.ask_urgent_after_minutes", "notify.urgent_gap_minutes"}


@dataclass(frozen=True)
class DeckConfig:
    deck: DeckSection = DeckSection()
    network: NetworkSection = NetworkSection()
    desks: DesksSection = DesksSection()
    claude: ClaudeSection = ClaudeSection()
    doctor: DoctorSection = DoctorSection()
    update: UpdateSection = UpdateSection()
    notify: NotifySection = NotifySection()
    owner: OwnerSection = OwnerSection()
    accounts: AccountsSection = AccountsSection()
    events: EventsSection = EventsSection()
    #: The file these values came from; None when every value is a default.
    source: Path | None = None

    def get(self, dotted: str) -> Any:
        """`cfg.get("network.tls")` -- the addressing `deckctl config` uses."""
        section, _, key = dotted.partition(".")
        cls = _SECTIONS.get(section)
        if cls is None or key not in {f.name for f in fields(cls)}:
            raise KeyError(dotted)
        return getattr(getattr(self, section), key)


# ── coercion: one place decides what a legal value is ───────────────────────


def _coerce(key: str, default: Any, value: Any) -> Any:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(key, "must be true or false")
        return value
    if key in _MINUTES:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10080:
            raise ConfigError(key, f"must be a whole number of minutes from 0 to 10080, not {value!r}")
        return value
    if key == "accounts.threshold_pct":
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
            raise ConfigError(key, f"must be a whole percent from 1 to 100, not {value!r}")
        return value
    if isinstance(default, int):
        return _port(key, value)
    if isinstance(default, tuple):
        if not isinstance(value, list):
            raise ConfigError(key, "must be a list, like [ ... ]")
        if key == "deck.reserved_ports":
            return tuple(_port(key, v) for v in value)
        if not all(isinstance(v, str) for v in value):
            raise ConfigError(key, "must be a list of quoted strings")
        return tuple(value)
    if not isinstance(value, str):
        raise ConfigError(key, "must be a quoted string")
    return _string(key, value)


def _port(key: str, value: Any) -> int:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError(key, f"must be a port number from 1 to 65535, not {value!r}")
    return value


def _string(key: str, value: str) -> str:
    if key in _CHOICES and value not in _CHOICES[key]:
        raise ConfigError(key, f"must be one of {', '.join(_CHOICES[key])}, not {value!r}")
    if key in _ABSOLUTE and not value.startswith("/"):
        raise ConfigError(key, f"must be an absolute path starting with /, not {value!r}")
    if key in ("deck.name", "owner.name") and len(value) > 60:
        raise ConfigError(key, "must be at most 60 characters")
    if key == "network.hostname" and value and not _HOSTNAME.match(value):
        raise ConfigError(key, "must be a bare host name like deck.example.com, "
                               f"with no https:// or path, not {value!r}")
    if key == "claude.token_issued" and value:
        try:
            _dt.date.fromisoformat(value[:10])
        except ValueError:
            raise ConfigError(key, f"must be a date like 2026-09-30, not {value!r}") from None
    return value


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ── load ─────────────────────────────────────────────────────────────────────

_ENV_KEYS = {"DECK_BIND": ("deck", "bind"), "DECK_PORT": ("deck", "port"),
             "DECK_STATE_DIR": ("deck", "state_dir")}


def _parent_not_searchable(p: Path) -> bool:
    return not os.access(p.parent, os.X_OK)


def load(path: str | os.PathLike | None = None,
         env: Mapping[str, str] | None = None) -> DeckConfig:
    """Read deck.toml (or `$DECK_CONFIG`) and apply `DECK_BIND`, `DECK_PORT`,
    `DECK_STATE_DIR` on top. A missing file is all defaults."""
    env = os.environ if env is None else env
    named = path if path is not None else env.get("DECK_CONFIG")
    chosen = Path(named or DEFAULT_PATH)
    raw: dict[str, Any] = {}
    source: Path | None = None
    try:
        text = chosen.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    except OSError as exc:
        if not named and _parent_not_searchable(chosen):
            # Nobody named this file and its folder is closed to us, so we cannot
            # even tell whether it exists: no config, not a broken one.
            _log.warning("%s is not readable by this user (its folder %s cannot be "
                         "opened), so every default is in use", chosen, chosen.parent)
        else:
            raise ConfigError("deck.toml", f"at {chosen} cannot be read ({exc.strerror})") from None
    else:
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError("deck.toml", f"at {chosen} is not valid TOML ({exc})") from None
        source = chosen

    for var, (section, key) in _ENV_KEYS.items():
        if env.get(var):
            table = raw.setdefault(section, {})
            if isinstance(table, dict):
                table[key] = env[var]

    built: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        table = raw.get(name, {})
        if not isinstance(table, dict):
            raise ConfigError(name, f"must be a [{name}] section, not a single value")
        values = {}
        for f in fields(cls):
            if f.name in table:
                values[f.name] = _coerce(f"{name}.{f.name}", f.default, table[f.name])
        built[name] = cls(**values)
    cfg = DeckConfig(**built, source=source)

    if cfg.network.mode == "public" and not _is_loopback(cfg.deck.bind):
        raise ConfigError("deck.bind", f"is {cfg.deck.bind!r}, but network.mode is public, "
                                       "so the deck must bind 127.0.0.1 and be reached "
                                       "only through Caddy")
    return cfg
