"""Money a desk reports itself: a provider bill, a distributor fee, a payout.

Providers like Kling/Kie and distributors like DistroKid have no read API the
deck holds a key for, so the desk that spends (or earns) logs it here with
`mcp__deck__log_money`, and the Money Board adds it to that desk's company.
Append-only JSONL beside the roster; a bad entry is refused with a reason.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..paths import BUS_DIR

DEFAULT_PATH = BUS_DIR / "money" / "ledger.jsonl"
KINDS = ("cost", "revenue")
CATEGORIES = ("provider", "distributor", "cloud", "ads", "other")
WHAT_MAX = 200
AMOUNT_MAX = 1_000_000


class LedgerError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason, self.detail = reason, detail or reason


def add(path: Path, *, desk: str, kind: str, amount, currency: str, what: str,
        category: str = "other", now: float | None = None) -> dict:
    if kind not in KINDS:
        raise LedgerError("bad_kind", f"kind is one of {', '.join(KINDS)}")
    if category not in CATEGORIES:
        raise LedgerError("bad_category", f"category is one of {', '.join(CATEGORIES)}")
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise LedgerError("bad_amount", "amount is a number, e.g. 12.50") from None
    if not 0 < amount <= AMOUNT_MAX:
        raise LedgerError("bad_amount", "amount is above 0 (a refund is a revenue "
                          "or cost entry of its own)")
    currency = str(currency or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise LedgerError("bad_currency", "currency is a 3-letter code: GBP, USD, EUR")
    what = " ".join(str(what or "").split())[:WHAT_MAX]
    if not what:
        raise LedgerError("no_what", "say what it was for, e.g. 'Kling 500 credits'")
    row = {"ts": now or time.time(), "desk": desk, "kind": kind, "amount": amount,
           "currency": currency, "what": what, "category": category}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def read(path: Path = DEFAULT_PATH, since: float = 0.0) -> list[dict]:
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and float(row.get("ts") or 0) >= since:
            out.append(row)
    return out
