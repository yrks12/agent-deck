"""Per-desk Claude token usage, measured from the desks' own transcripts.

The Money Board charges each desk its Claude work, and the token-diet audit
measures the same thing; this module is the one place it is counted.
"""

from __future__ import annotations

import json
from pathlib import Path

from server.paths import slug_for
from server.roster import Desk
from server.sources import desk_tokens as dt

DAY = "2026-09-30T10:00:00.000Z"


def _line(mid: str, model: str = "claude-opus-5-5", out: int = 10,
          inp: int = 100, cr: int = 1000, cw: int = 50, ts: str = DAY) -> str:
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "id": mid, "model": model, "role": "assistant", "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cw}}}) + "\n"


def _write(root: Path, cwd: str, sid: str, *lines: str, sub: bool = False) -> Path:
    d = root / slug_for(cwd)
    p = (d / sid / "subagents" / "agent-1.jsonl") if sub else d / f"{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(lines))
    return p


def _desk(name: str, cwd: str) -> Desk:
    return Desk(name=name, cwd=cwd, engine="claude", mission="")


def test_counts_each_message_once_and_attributes_by_cwd(tmp_path):
    a = _desk("globex-lead", "/w/globex-lead")
    _write(tmp_path, a.cwd, "s1", _line("m1"), _line("m1"), _line("m2", out=5))
    meter = dt.DeskTokens(tmp_path, cache_path=tmp_path / "c.json")
    got = meter.measure([a])
    u = got["globex-lead"]
    assert u.output == 15                     # m1 streamed twice, counted once
    assert u.input == 200 and u.cache_read == 2000 and u.cache_write == 100
    assert u.by_day["2026-09-30"] == 15 + 200 + 2000 + 100


def test_longest_cwd_wins_and_worktrees_and_subagents_count(tmp_path):
    globex = _desk("globex", "/w/globex")
    lead = _desk("globex-lead", "/w/globex-lead")
    _write(tmp_path, "/w/globex-lead", "s1", _line("a"))
    _write(tmp_path, "/w/globex-lead/repo/.claude/worktrees/x", "s2", _line("b"))
    _write(tmp_path, "/w/globex-lead", "s1", _line("c"), sub=True)
    _write(tmp_path, "/w/globex", "s3", _line("d", out=1))
    got = dt.DeskTokens(tmp_path, cache_path=tmp_path / "c.json").measure(
        [globex, lead])
    assert got["globex-lead"].output == 30
    assert got["globex"].output == 1


def test_shared_cwd_goes_to_the_seated_desk_then_to_the_named_one(tmp_path):
    ops = _desk("yye-ops", "/r/yye")
    growth = _desk("yye-growth", "/r/yye")
    _write(tmp_path, "/r/yye", "seated", _line("a", out=7))
    brief = json.dumps({"type": "user", "message": {"content":
                        "You are yye-growth, the growth desk."}}) + "\n"
    _write(tmp_path, "/r/yye", "named", brief, _line("b", out=3))
    meter = dt.DeskTokens(tmp_path, cache_path=tmp_path / "c.json",
                          seats=lambda: {"seated": "yye-ops"})
    got = meter.measure([ops, growth])
    assert got["yye-ops"].output == 7
    assert got["yye-growth"].output == 3


def test_reads_only_new_bytes_and_survives_a_restart(tmp_path):
    a = _desk("d", "/w/d")
    p = _write(tmp_path, a.cwd, "s", _line("m1"))
    cache = tmp_path / "c.json"
    assert dt.DeskTokens(tmp_path, cache_path=cache).measure([a])["d"].output == 10
    with p.open("a") as fh:
        fh.write(_line("m1") + _line("m2", out=4))   # m1 again: still once
    fresh = dt.DeskTokens(tmp_path, cache_path=cache)   # a restarted deck
    assert fresh.measure([a])["d"].output == 14


def test_api_equivalent_price_by_model():
    u = dt.Usage()
    u.add("claude-opus-5-5", "2026-09-30", inp=1_000_000, out=1_000_000,
          cr=1_000_000, cw=1_000_000)
    # Opus 5.5: $4 in, $20 out, $0.20 cache read, 1.25x input for a cache write
    assert round(u.api_usd(), 2) == round(4 + 20 + 0.20 + 5.0, 2)
    unknown = dt.Usage()
    unknown.add("some-new-model", "2026-09-30", inp=1_000_000, out=0, cr=0, cw=0)
    assert unknown.api_usd() == 4.0          # unknown -> priced as the Opus default
