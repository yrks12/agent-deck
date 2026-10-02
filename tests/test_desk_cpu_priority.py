"""A desk's heavy work never starves the deck's own API of CPU or disk.

MEASURED on the box, 2026-10-02 03:11: load ~25 on 4 cores, and the deck went
unreachable several times. Every desk session runs INSIDE agentdeck.service's
cgroup (`claude daemon run` is the deck's descendant), so a systemd CPUWeight
on the unit cannot tell the deck from its desks. The desks ran at nice 5 (the
CLI's own offset) beside the deck's API at nice 0: a vitest/ffmpeg suite or a
`harvest.py` crawl competed almost evenly with the board.

So every `claude --bg` the deck runs starts under `nice -n DESK_NICE` and
`ionice -c 2 -n 7`. The CLI daemon the first launch starts inherits both, and
every session, shell and test run it hosts inherits them from it.
"""

from pathlib import Path

from server import roster, spawn

ROOT = Path(__file__).resolve().parents[1]
CHOOM, NICE, IONICE = "/usr/bin/choom", "/usr/bin/nice", "/usr/bin/ionice"


class _Ran:
    returncode, stdout, stderr = 0, "backgrounded · abcd1234 · atlas\n", ""


def _capture(monkeypatch):
    seen = []
    monkeypatch.setattr(spawn, "_choom", lambda: CHOOM)
    monkeypatch.setattr(spawn, "_nice", lambda: NICE)
    monkeypatch.setattr(spawn, "_ionice", lambda: IONICE)
    monkeypatch.setattr(spawn.subprocess, "run",
                        lambda argv, **kw: seen.append(argv) or _Ran())
    return seen


def _assert_lowered(argv):
    assert argv[:4] == [CHOOM, "-n", str(spawn.DESK_OOM_SCORE), "--"]
    assert argv[4:8] == [NICE, "-n", str(spawn.DESK_NICE), "--"]
    assert argv[8:14] == [IONICE, "-c", "2", "-n", "7", "--"]
    assert argv[14:16] == ["claude", "--bg"]


def test_a_desk_runs_below_the_deck():
    assert spawn.DESK_NICE >= 10
    unit = (ROOT / "deploy" / "templates" / "agentdeck.service.in").read_text()
    nice = [line.split("=", 1)[1].strip() for line in unit.splitlines()
            if line.strip().startswith("Nice=")]
    assert all(int(n) <= 0 for n in nice), "the deck itself must not be niced"


def test_lowered_chains_oom_cpu_and_io():
    argv = ["claude", "--bg", "hello"]
    _assert_lowered(spawn.lowered(argv, choom=CHOOM, nice=NICE, ionice=IONICE))


def test_a_missing_tool_is_skipped_not_fatal():
    argv = ["claude", "--bg", "hello"]
    assert spawn.lowered(argv, choom=None, nice=None, ionice=None) == argv
    assert spawn.lowered(argv, choom=None, nice=NICE, ionice=None) == [
        NICE, "-n", str(spawn.DESK_NICE), "--", *argv]


def test_a_hired_desk_starts_niced(monkeypatch, tmp_path):
    monkeypatch.setattr(spawn, "_vouch",
                        lambda desk, roster_path: spawn.pretrust.Trust(True, "ok"))
    monkeypatch.setattr(spawn, "_approval_settings", lambda desk: "")
    monkeypatch.setattr(spawn, "_boss_address", lambda desk: "")
    monkeypatch.setattr(spawn.rules, "mark_seen", lambda name: None)
    seen = _capture(monkeypatch)
    spawn.spawn_background(
        roster.Desk(name="atlas", cwd="/nonexistent/atlas", engine="claude",
                    mission="m"),
        roster_path=tmp_path / "roster.json")
    _assert_lowered(seen[0])


def test_a_resumed_desk_starts_niced(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    spawn.resume_background("sid-1", cwd=str(tmp_path / "gone"), seed="hi",
                            account="main")
    _assert_lowered(seen[0])
    assert seen[0][16] == "--resume"
