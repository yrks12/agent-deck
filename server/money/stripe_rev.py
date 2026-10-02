"""Money in: one Stripe account, READ-ONLY, attributed to companies.

Only restricted keys (`rk_...`) are used. A full secret key (`sk_...`) can
move money, so it is refused and the board says to make a restricted
read-only one instead. This module has a GET helper and nothing else.

A charge is attributed by what was sold: the product on its checkout session
(name + metadata), else the charge's own description and metadata, matched
against each company's keywords. A charge that matches nobody stays on the
board as unattributed rather than vanishing.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

API = "https://api.stripe.com/v1/"
_KEY = re.compile(r"\b(?:rk|sk)_(?:live|test)_[A-Za-z0-9]+")
_PER_MONTH = {"day": 365 / 12, "week": 52 / 12, "month": 1.0, "year": 1 / 12}
_PAGES = 20


class StripeError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason, self.detail = reason, detail or reason


@dataclass(frozen=True)
class Account:
    label: str
    key: str = field(repr=False)


def discover(config_home: Path) -> list[Account]:
    """Stripe keys the owner left on this machine: ~/.config/stripe-<label>/*."""
    out = []
    for d in sorted(Path(config_home).glob("stripe-*")):
        for f in sorted(d.iterdir()) if d.is_dir() else []:
            try:
                m = _KEY.search(f.read_text(errors="replace"))
            except OSError:
                continue
            if m:
                out.append(Account(d.name.removeprefix("stripe-"), m.group(0)))
                break
    return out


def _http_get(key: str) -> Callable[[str, dict], dict]:
    def get(path: str, params: dict) -> dict:
        r = httpx.get(API + path, params=params, auth=(key, ""), timeout=20)
        body = r.json()
        if r.status_code != 200 or "error" in body:
            err = body.get("error") or {}
            raise StripeError(str(err.get("type") or r.status_code),
                              str(err.get("message") or "")[:200])
        return body
    return get


def _all(get, path: str, params: dict) -> list[dict]:
    rows: list[dict] = []
    params = {**params, "limit": 100}
    for _ in range(_PAGES):
        page = get(path, params)
        rows += page.get("data") or []
        if not page.get("has_more") or not rows:
            break
        params = {**params, "starting_after": rows[-1].get("id")}
    return rows


def company_for(texts, companies: dict[str, list[str]]) -> str | None:
    hay = " ".join(str(t) for t in texts if t).lower()
    for name, words in companies.items():
        if any(w and w.lower() in hay for w in [name, *words]):
            return name
    return None


def _money(minor: int) -> float:
    return round((minor or 0) / 100, 2)


def fetch(key: str, *, since: float, companies: dict[str, list[str]],
          get: Callable[[str, dict], dict] | None = None,
          now: float | None = None) -> dict:
    """Charges since `since`, MRR, 30-day payouts and balance. Never raises."""
    if not key.startswith("rk_"):
        return {"state": "refused", "detail": "This is a full secret key, which "
                "can move money. Make a restricted read-only key instead."}
    get = get or _http_get(key)
    now = now or time.time()
    try:
        acct = get("account", {})
        products = {p["id"]: p for p in _all(get, "products", {})}

        def about(pid) -> list:
            p = products.get(pid) or {}
            return [p.get("name"), *(p.get("metadata") or {}).values()]

        sold: dict[str, list] = {}
        for s in _all(get, "checkout/sessions", {"created[gte]": int(since),
                                                 "expand[]": "data.line_items"}):
            items = (s.get("line_items") or {}).get("data") or []
            words = [*(s.get("metadata") or {}).values()]
            for it in items:
                words += about((it.get("price") or {}).get("product"))
            if s.get("payment_intent"):
                sold[s["payment_intent"]] = words
        charges = []
        for c in _all(get, "charges", {"created[gte]": int(since)}):
            if c.get("status") != "succeeded" or not c.get("paid"):
                continue
            net = (c.get("amount") or 0) - (c.get("amount_refunded") or 0)
            if net <= 0:
                continue
            words = sold.get(c.get("payment_intent") or "", []) + [
                c.get("description"), *(c.get("metadata") or {}).values()]
            charges.append({"ts": c.get("created"), "amount": _money(net),
                            "currency": str(c.get("currency") or "").upper(),
                            "company": company_for(words, companies)})
        mrr: dict[str, float] = {}
        for sub in _all(get, "subscriptions", {"status": "active"}):
            for it in (sub.get("items") or {}).get("data") or []:
                price = it.get("price") or {}
                rec = price.get("recurring") or {}
                monthly = (_money(price.get("unit_amount") or 0) * (it.get("quantity") or 1)
                           * _PER_MONTH.get(rec.get("interval"), 0)
                           / max(1, rec.get("interval_count") or 1))
                who = company_for(about(price.get("product")), companies) or "Unattributed"
                mrr[who] = round(mrr.get(who, 0) + monthly, 2)
        payouts = _all(get, "payouts", {"created[gte]": int(now - 30 * 86400)})
        bal = get("balance", {})
    except StripeError as exc:
        return {"state": "error", "detail": f"{exc.reason}: {exc.detail}"}
    except (httpx.HTTPError, ValueError) as exc:
        return {"state": "error", "detail": f"unreachable: {type(exc).__name__}"}
    return {
        "state": "connected",
        "label": ((acct.get("settings") or {}).get("dashboard") or {}).get("display_name")
        or acct.get("id"),
        "currency": str(acct.get("default_currency") or "").upper(),
        "charges": charges, "mrr": mrr,
        "payouts_30d": _money(sum(p.get("amount") or 0 for p in payouts
                                  if p.get("status") in ("paid", "in_transit"))),
        "balance": {k: _money(sum(b.get("amount") or 0 for b in bal.get(k) or []))
                    for k in ("available", "pending")},
    }
