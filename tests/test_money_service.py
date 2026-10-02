"""A Money Board refresh reads every source, caches the board, and flags once."""

from __future__ import annotations

import json
import time

from server import roster
from server.money import service, stripe_rev
from server.sources.desk_tokens import DeskTokens

NOW = time.time()


def _roster(tmp_path):
    p = tmp_path / "roster.json"
    desks = [roster.Desk(name="atlas", cwd="/w/a", engine="claude", mission="",
                         created_at=NOW - 30 * 86400),
             roster.Desk(name="globex-growth", cwd="/w/p", engine="claude", mission="",
                         created_at=NOW - 20 * 86400, reports_to="atlas"),
             roster.Desk(name="acme-eng", cwd="/w/c", engine="claude", mission="",
                         created_at=NOW - 20 * 86400, reports_to="atlas")]
    roster.save_roster(p, desks)
    return p


def _refresh(tmp_path, sent, stripe_rows=None):
    def stripe_fetch(key, *, since, companies):
        assert key == "rk_live_k" and "Globex" in companies and "HQ" not in companies
        return {"state": "connected", "label": "YT", "currency": "GBP", "mrr": {},
                "charges": stripe_rows or [{"ts": NOW - 86400, "amount": 10.0,
                                            "currency": "GBP", "company": "Acme"}],
                "payouts_30d": 0.0, "balance": {"available": 0.0, "pending": 9.65}}
    return service.refresh(
        roster_path=_roster(tmp_path), now=NOW,
        meter=DeskTokens(tmp_path / "projects", cache_path=tmp_path / "dt.json"),
        accounts=[stripe_rev.Account("example", "rk_live_k")],
        stripe_fetch=stripe_fetch,
        cloud_fetch=lambda sa, table, **kw: {"state": "missing", "detail": "No export."},
        send=lambda to, text, extra: sent.append((to, extra)) or {"ok": True},
        out=tmp_path / "board.json", ledger_path=tmp_path / "ledger.jsonl",
        flags_path=tmp_path / "flags.json")


def test_refresh_builds_caches_and_flags_once(tmp_path):
    sent: list = []
    b = _refresh(tmp_path, sent)
    assert json.loads((tmp_path / "board.json").read_text())["totals"] == b["totals"]
    assert {c["name"] for c in b["companies"]} == {"HQ", "Globex", "Acme"}
    assert sent == [("atlas", {"kind": "money_flag", "desks": ["globex-growth"]})]
    _refresh(tmp_path, sent)
    assert len(sent) == 1
    assert b["stripe"][0] == {"label": "YT", "state": "connected", "payouts_30d": 0.0,
                              "balance": {"available": 0.0, "pending": 9.65}}
    assert "rk_live_k" not in (tmp_path / "board.json").read_text()
