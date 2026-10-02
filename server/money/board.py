"""The Money Board: money in, money out and ROI per company and experiment.

`build` is pure: it takes what the sources read (Stripe, Google Cloud, the
desk ledger, Claude tokens per desk) and returns the board the apps draw.
Every amount is in the display currency; a foreign amount is converted at the
config's FX rate (`fx_assumed` says when those are still the defaults).

Cost includes each desk's Claude work at API prices. The desks run on a plan,
so that is not cash out -- it is what the work would cost bought by the token,
which is the fair price to weigh an experiment against.

The 14-day rule: an experiment (a desk expected to earn) with no revenue
signal -- a Stripe sale for its company or a revenue entry it logged, after
it started -- 14 days in is flagged. `notify_chief` puts ONE card in the
chief's thread for the new flags, once per desk, never to the owner.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import atomic
from ..paths import BUS_DIR
from . import companies as co

FLAG_DAYS = 14
FLAGS_PATH = BUS_DIR / "money" / "flags.json"
DAY = 86400.0
_COST_LINE = {"provider": "providers", "ads": "providers", "distributor": "distributor",
              "cloud": "cloud", "other": "other"}


def _to(cfg: co.Config, amount: float, currency: str) -> float | None:
    rate = cfg.fx_to_display.get((currency or cfg.display_currency).upper())
    return None if rate is None else float(amount) * float(rate)


def _r(x: float) -> float:
    return round(x + 0.0, 2)


def _cards(stripe: list[dict], cloud: dict) -> list[dict]:
    if not stripe:
        s = {"state": "missing", "title": "Connect Stripe", "detail": "Create a "
             "restricted read-only Stripe key (read: charges, checkout sessions, "
             "products, subscriptions, payouts, balance) and save it on the deck."}
    else:
        bad = [a for a in stripe if a.get("state") != "connected"]
        s = ({"state": bad[0]["state"], "title": "Fix Stripe", "detail": bad[0].get("detail", "")}
             if bad else {"state": "connected", "title": "Stripe",
                          "detail": ", ".join(str(a.get("label")) for a in stripe)})
    c = {"state": cloud.get("state", "missing"),
         "title": "Cloud billing" if cloud.get("state") == "connected" else "Connect Cloud billing",
         "detail": cloud.get("detail") or cloud.get("table", "")}
    return [{"source": "stripe", **s}, {"source": "cloud", **c}]


def build(desks, owner: dict[str, str], cfg: co.Config, *, now: float,
          stripe: list[dict], cloud: dict, ledger: list[dict], tokens: dict) -> dict:
    names = sorted(set(owner.values()))
    start = {n: min((d.created_at for d in desks if owner.get(d.name) == n and d.created_at),
                    default=0.0) for n in names}
    row = {n: {"name": n, "started_at": start[n], "revenue": 0.0, "mrr": 0.0,
               "desks": sorted(d.name for d in desks if owner.get(d.name) == n),
               "costs": {"cloud": 0.0, "providers": 0.0, "distributor": 0.0,
                         "other": 0.0, "claude": 0.0},
               "claude": {"tokens": 0, "api_usd": 0.0}} for n in names}
    unattributed = 0.0
    sales: list[tuple[float, str, float]] = []           # (ts, company, amount)
    connected = [a for a in stripe if a.get("state") == "connected"]
    for acct in connected:
        for ch in acct.get("charges") or []:
            amt = _to(cfg, ch["amount"], ch.get("currency"))
            if amt is None:
                continue
            if ch.get("company") in row:
                row[ch["company"]]["revenue"] += amt
                sales.append((float(ch.get("ts") or 0), ch["company"], amt))
            else:
                unattributed += amt
        for who, m in (acct.get("mrr") or {}).items():
            amt = _to(cfg, m, acct.get("currency"))
            if who in row and amt:
                row[who]["mrr"] += amt
                sales.append((now, who, 0.0))              # a live subscription
    if cloud.get("state") == "connected":
        for who, amt in (cloud.get("by_company") or {}).items():
            amt = _to(cfg, amt, cloud.get("currency"))
            if who in row and amt is not None:
                row[who]["costs"]["cloud"] += amt
    desk_in: dict[str, float] = {}
    desk_out: dict[str, float] = {}
    for e in ledger:
        who = owner.get(e.get("desk"))
        amt = _to(cfg, e.get("amount") or 0, e.get("currency"))
        if who not in row or amt is None:
            continue
        if e.get("kind") == "revenue":
            row[who]["revenue"] += amt
            desk_in[e["desk"]] = desk_in.get(e["desk"], 0) + amt
        else:
            row[who]["costs"][_COST_LINE.get(e.get("category"), "other")] += amt
            desk_out[e["desk"]] = desk_out.get(e["desk"], 0) + amt
    claude_of: dict[str, float] = {}
    for desk, u in tokens.items():
        who = owner.get(desk)
        if who in row:
            usd = u.api_usd()
            claude_of[desk] = _to(cfg, usd, "USD") or 0.0
            row[who]["claude"]["tokens"] += u.total
            row[who]["claude"]["api_usd"] += usd
            row[who]["costs"]["claude"] += claude_of[desk]
    out_rows = []
    for n in names:
        r = row[n]
        r["costs"] = {k: _r(v) for k, v in r["costs"].items()}
        r["cost_total"] = _r(sum(r["costs"].values()))
        r["revenue"], r["mrr"] = _r(r["revenue"]), _r(r["mrr"])
        r["claude"]["api_usd"] = _r(r["claude"]["api_usd"])
        r["net"] = _r(r["revenue"] - r["cost_total"])
        r["roi"] = round(r["net"] / r["cost_total"], 2) if r["cost_total"] else None
        r["overhead"] = n in {co.HQ, *cfg.overhead}
        out_rows.append(r)
    exps = []
    for d in co.experiments(desks, owner, cfg):
        who, since = owner[d.name], d.created_at or now
        got = sum(a for ts, c, a in sales if c == who and ts >= since)
        signal = (any(c == who and ts >= since for ts, c, _ in sales)
                  or d.name in desk_in)
        cost = desk_out.get(d.name, 0.0) + claude_of.get(d.name, 0.0)
        revenue = got + desk_in.get(d.name, 0.0)
        days = int((now - since) // DAY)
        exps.append({"desk": d.name, "company": who, "started_at": since, "days": days,
                     "revenue": _r(revenue), "cost": _r(cost),
                     "roi": round((revenue - cost) / cost, 2) if cost else None,
                     "signal": signal,
                     "flag": ("no_revenue_14d" if connected and not signal
                              and days >= FLAG_DAYS else None)})
    rev = sum(r["revenue"] for r in out_rows) + unattributed
    cost = sum(r["cost_total"] for r in out_rows)
    return {"currency": cfg.display_currency, "generated_at": now,
            "fx_assumed": cfg.fx_to_display == co.Config().fx_to_display,
            "flag_days": FLAG_DAYS,
            "totals": {"revenue": _r(rev), "costs": _r(cost), "net": _r(rev - cost),
                       "mrr": _r(sum(r["mrr"] for r in out_rows))},
            "companies": sorted(out_rows, key=lambda r: (r["overhead"], -r["revenue"],
                                                         -r["cost_total"])),
            "experiments": sorted(exps, key=lambda e: (e["flag"] is None, -e["days"])),
            "unattributed": {"revenue": _r(unattributed)},
            "connect": _cards(stripe, cloud)}


def notify_chief(board: dict, *, chief: str | None, send, path: Path = FLAGS_PATH,
                 now: float | None = None) -> int:
    """ONE card in the chief's thread for experiments newly flagged. Returns
    how many desks it named (0 when nothing new, no chief, or a failed send)."""
    try:
        done = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        done = {}
    new = [e for e in board.get("experiments") or []
           if e.get("flag") and e["desk"] not in done]
    if not new or not chief:
        return 0
    cur = board.get("currency", "")
    lines = [f"- {e['desk']} ({e['company']}): {e['days']} days, no revenue yet, "
             f"cost so far ~{cur} {e['cost']:.2f} (incl. Claude at API prices)"
             for e in new]
    text = ("Money check, the 14-day rule: these experiments have had no revenue "
            "signal for 14+ days.\n" + "\n".join(lines) + "\nFor each, review it: "
            "keep it with a reason and a date for the first sale, or retire it "
            "(mcp__deck__retire_desk). A desk that earns off-Stripe should log it "
            "with mcp__deck__log_money. This card comes once per desk.")
    sent = send(chief, text, {"kind": "money_flag", "desks": [e["desk"] for e in new]})
    if not (sent or {}).get("ok"):
        return 0
    done.update({e["desk"]: now or time.time() for e in new})
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(Path(path), json.dumps(done))
    return len(new)
