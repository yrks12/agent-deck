"""Money in: Stripe, read-only, attributed to a company."""

from __future__ import annotations

from server.money import stripe_rev

COMPANIES = {"Acme": ["acme"], "Shorts": ["shorts", "faceless"],
             "Listing": ["listing", "videovend"]}
DAY = 1790000000


def _page(*rows):
    return {"object": "list", "data": list(rows), "has_more": False}


def _fake(calls: list | None = None, **over):
    data = {
        "account": {"id": "acct_1", "settings": {"dashboard": {"display_name": "Example Ltd"}},
                    "default_currency": "gbp"},
        "charges": _page(
            {"id": "ch_1", "created": DAY, "amount": 1000, "amount_refunded": 0,
             "currency": "gbp", "status": "succeeded", "paid": True,
             "payment_intent": "pi_1", "description": None, "metadata": {"product": "pack_100"}},
            {"id": "ch_2", "created": DAY, "amount": 1900, "amount_refunded": 1900,
             "currency": "gbp", "status": "succeeded", "paid": True,
             "payment_intent": "pi_2", "description": None, "metadata": {}},
            {"id": "ch_3", "created": DAY, "amount": 500, "amount_refunded": 0,
             "currency": "gbp", "status": "succeeded", "paid": True,
             "payment_intent": None, "description": "Faceless Director 1 Short",
             "metadata": {}},
            {"id": "ch_4", "created": DAY, "amount": 700, "amount_refunded": 0,
             "currency": "gbp", "status": "failed", "paid": False,
             "payment_intent": None, "description": "acme", "metadata": {}}),
        "checkout/sessions": _page(
            {"payment_intent": "pi_1", "metadata": {},
             "line_items": {"data": [{"price": {"product": "prod_c"}}]}}),
        "products": _page({"id": "prod_c", "name": "Acme Reels - 100 credits",
                           "metadata": {"app": "acme-reels"}},
                          {"id": "prod_v", "name": "VideoVend full listing video",
                           "metadata": {}}),
        "subscriptions": _page({"items": {"data": [{"quantity": 2, "price": {
            "product": "prod_v", "unit_amount": 1500, "currency": "gbp",
            "recurring": {"interval": "year", "interval_count": 1}}}]}}),
        "payouts": _page({"amount": 2500, "currency": "gbp", "arrival_date": DAY,
                          "status": "paid"}),
        "balance": {"available": [{"amount": -49, "currency": "gbp"}],
                    "pending": [{"amount": 965, "currency": "gbp"}]},
    }
    data.update(over)

    def get(path, params):
        if calls is not None:
            calls.append((path, params))
        return data[path]
    return get


def test_revenue_is_attributed_to_companies_and_refunds_and_failures_drop():
    got = stripe_rev.fetch("rk_live_x", since=DAY - 10, companies=COMPANIES,
                           get=_fake())
    assert got["state"] == "connected" and got["label"] == "Example Ltd"
    by = {(c["company"], c["amount"]) for c in got["charges"]}
    assert by == {("Acme", 10.0), ("Shorts", 5.0)}
    # VideoVend: 2 x 15/yr -> 2.50 a month, for Listing
    assert got["mrr"] == {"Listing": 2.5}
    assert got["payouts_30d"] == 25.0
    assert got["balance"] == {"available": -0.49, "pending": 9.65}


def test_every_call_is_a_read():
    calls: list = []
    stripe_rev.fetch("rk_live_x", since=DAY, companies=COMPANIES, get=_fake(calls))
    assert calls and all(isinstance(p, str) for p, _ in calls)
    # the module has no write path: no POST/DELETE helper exists at all
    assert not any(hasattr(stripe_rev, n) for n in ("post", "delete", "create"))


def test_a_full_secret_key_is_refused_not_used():
    calls: list = []
    got = stripe_rev.fetch("sk_live_x", since=DAY, companies=COMPANIES,
                           get=_fake(calls))
    assert got["state"] == "refused" and "restricted" in got["detail"]
    assert calls == []


def test_unattributed_charge_stays_visible():
    charge = {"id": "ch_9", "created": DAY, "amount": 300, "amount_refunded": 0,
              "currency": "gbp", "status": "succeeded", "paid": True,
              "payment_intent": None, "description": "mystery", "metadata": {}}
    got = stripe_rev.fetch("rk_live_x", since=DAY, companies=COMPANIES,
                           get=_fake(charges=_page(charge)))
    assert got["charges"][0]["company"] is None


def test_an_api_error_is_reported_not_raised():
    def boom(path, params):
        raise stripe_rev.StripeError("permission", "This key lacks rak_charge_read")
    got = stripe_rev.fetch("rk_live_x", since=DAY, companies=COMPANIES, get=boom)
    assert got["state"] == "error" and "rak_charge_read" in got["detail"]


def test_key_discovery_reads_only_restricted_or_secret_keys(tmp_path):
    d = tmp_path / "stripe-example"
    d.mkdir()
    (d / "rk").write_text("rk_live_ABC123def\n")
    (tmp_path / "stripe-other").mkdir()
    (tmp_path / "stripe-other" / "notes").write_text("nothing here")
    found = stripe_rev.discover(tmp_path)
    assert [(a.label, a.key) for a in found] == [("example", "rk_live_ABC123def")]
    assert "rk_live_ABC123def" not in repr(found[0])     # never printed
