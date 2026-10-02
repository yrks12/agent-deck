"""Which company each desk works for, and which desks are experiments.

Derived, so a new desk lands on the board with no setup: an app section other
than the default wins, then `desk_company` in the money config, then the
name's prefix (`acme-growth` -> Acme, `abc-ops` -> ABC). The chief is HQ.

An experiment is a desk with a start date that is expected to earn: not the
chief, not an overhead company, not a test or probe desk, not a new hire that
has not been set up yet.

The config (`money/config.json` beside the roster) holds nothing secret:
company names, keywords, FX rates, the Google Cloud table and projects.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import BUS_DIR
from .. import roster

DEFAULT_PATH = BUS_DIR / "money" / "config.json"
HQ = "HQ"
DEFAULT_SECTION = "Work"
_PROBE = re.compile(r"(probe|proof|^new-hire-|^test-)")
#: ASSUMED approximate rates, labelled as such on the board; set real ones in
#: the config's `fx_to_display`.
FX_DEFAULT = {"GBP": 1.0, "USD": 0.75, "EUR": 0.87}


@dataclass
class Config:
    display_currency: str = "GBP"
    fx_to_display: dict = field(default_factory=lambda: dict(FX_DEFAULT))
    desk_company: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)          # prefix -> display name
    keywords: dict = field(default_factory=dict)       # company -> [words]
    overhead: list = field(default_factory=lambda: ["Shared"])
    ignore: list = field(default_factory=list)          # desks never flagged
    gcp_service_account: str = ""
    gcp_table: str = ""
    gcp_projects: dict = field(default_factory=dict)    # project id -> company
    stripe_home: str = ""


def load_config(path: Path = DEFAULT_PATH) -> Config:
    try:
        raw = json.loads(Path(path).read_text())
        gcp = raw.get("gcp") or {}
        fx = {**FX_DEFAULT, **{k.upper(): float(v) for k, v in
                              (raw.get("fx_to_display") or {}).items()}}
        return Config(
            display_currency=str(raw.get("display_currency") or "GBP").upper(),
            fx_to_display=fx, desk_company=dict(raw.get("desk_company") or {}),
            names=dict(raw.get("names") or {}), keywords=dict(raw.get("keywords") or {}),
            overhead=list(raw.get("overhead") or ["Shared"]),
            ignore=list(raw.get("ignore") or []),
            gcp_service_account=str(gcp.get("service_account") or ""),
            gcp_table=str(gcp.get("table") or ""),
            gcp_projects=dict(gcp.get("projects") or {}),
            stripe_home=str(raw.get("stripe_home") or ""))
    except (OSError, ValueError, TypeError, AttributeError):
        return Config()


def _from_prefix(name: str, cfg: Config) -> str:
    prefix = name.split("-", 1)[0]
    if prefix in cfg.names:
        return cfg.names[prefix]
    return prefix.upper() if len(prefix) <= 3 else prefix.capitalize()


def _scratch(d) -> bool:
    """A test, probe or not-yet-set-up desk: overhead, not a company."""
    return bool(d.test or d.label == "New" or _PROBE.search(d.name))


def assign(desks, cfg: Config, sections: dict[str, str] | None = None) -> dict[str, str]:
    """desk name -> company name."""
    sections = sections or {}
    chief = roster.chief(list(desks))
    out = {}
    for d in desks:
        section = sections.get(d.name) or ""
        if section and section != DEFAULT_SECTION:
            out[d.name] = section
        elif d.name in cfg.desk_company:
            out[d.name] = cfg.desk_company[d.name]
        elif d.name == chief:
            out[d.name] = HQ
        elif _scratch(d):
            out[d.name] = cfg.overhead[0] if cfg.overhead else HQ
        else:
            out[d.name] = _from_prefix(d.name, cfg)
    return out


def experiments(desks, owner: dict[str, str], cfg: Config) -> list:
    chief = roster.chief(list(desks))
    skip = {HQ, *cfg.overhead}
    return [d for d in desks
            if d.name != chief and not _scratch(d) and d.name not in cfg.ignore
            and owner.get(d.name) not in skip]


def keywords(names, cfg: Config) -> dict[str, list[str]]:
    """Company -> words that put a Stripe product or charge on its line."""
    skip = {HQ, *cfg.overhead}
    return {n: [n.lower(), *[w.lower() for w in cfg.keywords.get(n, [])]]
            for n in names if n not in skip}
