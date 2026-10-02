"""Refresh the Money Board: read every source, build, cache, flag.

Run by the deck every `INTERVAL` seconds off the event loop. The result is
cached in `money/board.json`, which the API serves and a desk's
`mcp__deck__my_money` reads -- so a request never waits on Stripe or Google.
Credentials are read where the owner left them and never written anywhere:
Stripe from `~/.config/stripe-<label>/`, Google from the configured service
account (else the first key in `~/.config/gcloud-keys/`).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import atomic, office, roster
from ..paths import BUS_DIR
from ..sources.desk_tokens import DeskTokens
from . import board, cloud, companies, ledger, stripe_rev

BOARD_PATH = BUS_DIR / "money" / "board.json"
INTERVAL = 15 * 60
LOOKBACK_MAX = 365 * 86400.0

_meter: DeskTokens | None = None


def _sections() -> dict[str, str]:
    try:
        prefs = json.loads((BUS_DIR / "agent_prefs.json").read_text())
    except (OSError, ValueError):
        return {}
    rows = prefs.get("agents", prefs) if isinstance(prefs, dict) else {}
    return {k: str(v.get("section") or "") for k, v in rows.items() if isinstance(v, dict)}


def _service_account(cfg: companies.Config) -> str:
    if cfg.gcp_service_account:
        return cfg.gcp_service_account
    keys = sorted((Path.home() / ".config" / "gcloud-keys").glob("*.json"))
    return str(keys[0]) if keys else ""


def _send(to: str, text: str, extra: dict) -> dict:
    return office.send(to, text, sender="deck", extra=extra)


def refresh(*, roster_path=None, now: float | None = None, meter: DeskTokens | None = None,
            stripe_fetch=stripe_rev.fetch, cloud_fetch=cloud.fetch,
            accounts=None, send=_send, out: Path | None = None,
            ledger_path: Path = ledger.DEFAULT_PATH,
            flags_path: Path = board.FLAGS_PATH) -> dict:
    global _meter
    now = now or time.time()
    cfg = companies.load_config()
    desks = roster.load_roster(roster_path or roster.DEFAULT_PATH)
    owner = companies.assign(desks, cfg, _sections())
    since = max(now - LOOKBACK_MAX,
                min((d.created_at for d in desks if d.created_at), default=now) - 86400)
    words = companies.keywords(set(owner.values()), cfg)
    if accounts is None:
        accounts = stripe_rev.discover(Path(cfg.stripe_home or Path.home() / ".config"))
    stripe = [{"label": a.label, **stripe_fetch(a.key, since=since, companies=words)}
              for a in accounts]
    spend = cloud_fetch(_service_account(cfg), cfg.gcp_table, since=since,
                        projects=cfg.gcp_projects)
    if meter is None:
        meter = _meter = _meter or DeskTokens()
    tokens = meter.measure(desks)
    result = board.build(desks, owner, cfg, now=now, stripe=stripe, cloud=spend,
                         ledger=ledger.read(ledger_path), tokens=tokens)
    result["claude"] = sorted(
        ({"desk": d, "company": owner.get(d), "tokens": u.total,
          "api_usd": round(u.api_usd(), 2)} for d, u in tokens.items() if d in owner),
        key=lambda r: -r["api_usd"])
    result["stripe"] = [{k: a.get(k) for k in ("label", "state", "payouts_30d", "balance")}
                        for a in stripe]
    path = Path(out or BOARD_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, json.dumps(result))
    board.notify_chief(result, chief=roster.chief(desks), send=send, path=flags_path,
                       now=now)
    return result


def cached(path: Path | None = None) -> dict | None:
    try:
        return json.loads(Path(path or BOARD_PATH).read_text())
    except (OSError, ValueError):
        return None
