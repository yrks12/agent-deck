"""Money desks log themselves: provider bills, distributor fees, revenue."""

from __future__ import annotations

import pytest

from server.money import ledger

DAY = 1790000000


def test_ledger_records_costs_and_revenue_signals(tmp_path):
    p = tmp_path / "ledger.jsonl"
    ledger.add(p, desk="acme-eng", kind="cost", amount=12.5, currency="usd",
               what="Kling 50 credits", category="provider", now=DAY)
    ledger.add(p, desk="music-growth", kind="revenue", amount=3, currency="gbp",
               what="DistroKid payout", now=DAY)
    rows = ledger.read(p)
    assert [(r["desk"], r["kind"], r["amount"], r["currency"]) for r in rows] == [
        ("acme-eng", "cost", 12.5, "USD"), ("music-growth", "revenue", 3.0, "GBP")]


@pytest.mark.parametrize("bad", [
    {"kind": "gift"}, {"amount": -1}, {"amount": "lots"}, {"currency": "pounds"},
    {"what": ""}, {"category": "bribes"}])
def test_ledger_refuses_bad_entries(tmp_path, bad):
    args = dict(desk="d", kind="cost", amount=1, currency="usd", what="x",
                category="provider")
    args.update(bad)
    with pytest.raises(ledger.LedgerError):
        ledger.add(tmp_path / "l.jsonl", **args)
