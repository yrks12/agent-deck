"""When the box runs out of RAM, a desk dies before the deck does.

MEASURED on the box, 2026-10-01: every desk session runs INSIDE
agentdeck.service's cgroup (`claude daemon run` and its pty hosts are the
deck's descendants), and every one of them, the deck and the box's engine all
had oom_score_adj 0. So the deck's own `OOMScoreAdjust=-500` alone would be
inherited by every desk and protect nothing. The unit lowers the deck; every
`claude --bg` the deck runs is raised back to DESK_OOM_SCORE through `choom`,
and the CLI daemon it starts inherits that.
"""

from pathlib import Path

from server import spawn

ROOT = Path(__file__).resolve().parents[1]


def _directive(text, key):
    return [line.split("=", 1)[1].strip() for line in text.splitlines()
            if line.strip().startswith(key + "=")]


def test_the_deck_unit_is_the_last_thing_the_kernel_kills():
    unit = (ROOT / "deploy" / "templates" / "agentdeck.service.in").read_text()
    assert _directive(unit, "OOMScoreAdjust") == ["-500"]


def test_a_desk_launch_is_raised_back_above_the_deck():
    argv = ["claude", "--bg", "hello"]
    lowered = spawn.lowered(argv, choom="/usr/bin/choom")
    assert lowered == ["/usr/bin/choom", "-n", str(spawn.DESK_OOM_SCORE), "--",
                       *argv]
    assert spawn.DESK_OOM_SCORE > 0


def test_without_choom_the_argv_is_untouched():
    argv = ["claude", "--bg", "hello"]
    assert spawn.lowered(argv, choom=None) == argv


def test_a_resumed_desk_runs_under_choom(monkeypatch, tmp_path):
    seen = []

    class Done:
        returncode, stdout, stderr = 0, "backgrounded · abcd1234\n", ""

    monkeypatch.setattr(spawn, "_choom", lambda: "/usr/bin/choom")
    monkeypatch.setattr(spawn.subprocess, "run",
                        lambda argv, **kw: seen.append(argv) or Done())
    spawn.resume_background("sid-1", cwd=str(tmp_path / "gone"), seed="hi",
                            account="main")
    assert seen and seen[0][:4] == ["/usr/bin/choom", "-n",
                                    str(spawn.DESK_OOM_SCORE), "--"]
    assert seen[0][4:7] == ["claude", "--bg", "--resume"]
