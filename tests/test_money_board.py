"""The Money Board: in, out and ROI per company, and the 14-day rule.

The 14-day rule is the detector the owner asked for: an experiment with no
revenue signal 14 days after it started puts ONE card in the chief's thread.
"""

from __future__ import annotations

from server.money import board, companies
from server.roster import Desk
from server.sources.desk_tokens import Usage

DAY = 86400.0
NOW = 1_800_000_000.0


def _d(name, age_days, **kw):
    return Desk(name=name, cwd=f"/w/{name}", engine="claude", mission="",
                created_at=NOW - age_days * DAY, reports_to=kw.pop("boss", "atlas"), **kw)


DESKS = [_d("atlas", 30, boss=None), _d("acme-eng", 20), _d("acme-ui", 20),
         _d("globex-growth", 26), _d("music-ops", 10), _d("yes-netops", 20),
         _d("wake-probe", 30, test=True)]
CFG = companies.Config()
OWNER = companies.assign(DESKS, CFG)


def _tok(usd_out_tokens: int) -> Usage:
    u = Usage()
    u.add("claude-opus-5-5", "2026-01-01", inp=0, out=usd_out_tokens, cr=0, cw=0)
    return u


def _build(stripe=None, cloud=None, ledger=(), tokens=None):
    stripe = stripe if stripe is not None else [{"state": "connected", "label": "YT",
        "currency": "GBP", "mrr": {},
        "charges": [{"ts": NOW - 5 * DAY, "amount": 10.0, "currency": "GBP",
                     "company": "Acme"},
                    {"ts": NOW - 5 * DAY, "amount": 4.0, "currency": "GBP",
                     "company": None}]}]
    cloud = cloud or {"state": "missing", "detail": "No billing export found."}
    return board.build(DESKS, OWNER, CFG, now=NOW, stripe=stripe, cloud=cloud,
                       ledger=list(ledger), tokens=tokens or {})


def test_company_money_in_out_and_roi_include_claude_at_api_prices():
    b = _build(ledger=[{"ts": NOW - DAY, "desk": "acme-eng", "kind": "cost",
                        "amount": 2.0, "currency": "USD", "what": "Kling",
                        "category": "provider"}],
               tokens={"acme-eng": _tok(100_000)})   # $2.00 at $20/M out
    c = {x["name"]: x for x in b["companies"]}["Acme"]
    assert c["revenue"] == 10.0
    assert c["costs"]["providers"] == 1.5                 # $2 at 0.75
    assert c["claude"]["api_usd"] == 2.0 and c["costs"]["claude"] == 1.5
    assert c["cost_total"] == 3.0 and c["net"] == 7.0
    assert c["roi"] == round(7.0 / 3.0, 2)
    assert b["unattributed"]["revenue"] == 4.0
    assert b["currency"] == "GBP" and b["fx_assumed"] is True


def test_fourteen_day_rule_flags_only_experiments_without_a_revenue_signal():
    b = _build()
    flags = {e["desk"]: e["flag"] for e in b["experiments"]}
    assert flags["globex-growth"] == "no_revenue_14d"       # 26 days, nothing
    assert flags["yes-netops"] == "no_revenue_14d"
    assert flags["acme-eng"] is None                 # its company earned
    assert flags["music-ops"] is None                     # only 10 days old
    assert "atlas" not in flags and "wake-probe" not in flags


def test_a_revenue_signal_a_desk_logged_clears_the_flag():
    b = _build(ledger=[{"ts": NOW - DAY, "desk": "globex-growth", "kind": "revenue",
                        "amount": 1.0, "currency": "GBP", "what": "first sale",
                        "category": "other"}])
    assert {e["desk"]: e["flag"] for e in b["experiments"]}["globex-growth"] is None


def test_revenue_before_the_desk_started_is_not_its_signal():
    stripe = [{"state": "connected", "label": "YT", "currency": "GBP", "mrr": {},
               "charges": [{"ts": NOW - 40 * DAY, "amount": 9.0, "currency": "GBP",
                            "company": "Globex"}]}]
    b = _build(stripe=stripe)
    assert {e["desk"]: e["flag"] for e in b["experiments"]}["globex-growth"] == "no_revenue_14d"


def test_no_stripe_means_no_flags_and_a_connect_card():
    b = _build(stripe=[])
    assert all(e["flag"] is None for e in b["experiments"])
    cards = {c["source"]: c for c in b["connect"]}
    assert cards["stripe"]["state"] == "missing" and "read-only" in cards["stripe"]["detail"]
    assert cards["cloud"]["state"] == "missing"


def test_the_chief_gets_one_card_once_per_flagged_experiment(tmp_path):
    sent = []
    send = lambda to, text, extra: sent.append((to, text, extra)) or {"ok": True}
    path = tmp_path / "flags.json"
    b = _build()
    assert board.notify_chief(b, chief="atlas", path=path, send=send, now=NOW) == 2
    assert len(sent) == 1                                  # ONE card, both desks
    to, text, extra = sent[0]
    assert to == "atlas" and "globex-growth" in text and "yes-netops" in text
    assert "retire" in text and extra["kind"] == "money_flag"
    assert board.notify_chief(_build(), chief="atlas", path=path, send=send, now=NOW) == 0
    assert len(sent) == 1                                  # never twice


def test_a_failed_send_is_retried_next_time(tmp_path):
    path = tmp_path / "flags.json"
    fail = lambda to, text, extra: {"ok": False}
    assert board.notify_chief(_build(), chief="atlas", path=path, send=fail, now=NOW) == 0
    sent = []
    ok = lambda to, text, extra: sent.append(text) or {"ok": True}
    assert board.notify_chief(_build(), chief="atlas", path=path, send=ok, now=NOW) == 2
