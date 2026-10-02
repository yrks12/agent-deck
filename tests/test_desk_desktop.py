"""The desk's computer as a desktop: the browser and a terminal side by side,
a taskbar, and a bigger screen.

Before: Xvfb at 1280x800 with no window manager and Chromium filling it, so
the owner watching saw a browser and nothing of the shell. Then: a window
manager (jwm) with a taskbar, Chromium on the left, an xterm on the right.
Now (owner, 2026-10-01: "why I see terminal with the browser"): the window
manager and taskbar stay, Chromium fills the screen above the taskbar, and
no terminal window opens by itself -- the app's Terminal view is the PTY,
and Both is the app showing the two side by side.

MEASURED on the box 2026-10-01, one probe container each, Chromium on a
Wikipedia page:

* jwm 16 MB RSS, xterm 9 MB, tmux 7 MB, bash 3 MB, tail 1 MB: ~36 MB per
  desk, against a 1 GB container limit and ~550 MB used by Chromium.
* The screen stream (`screen_stream.STREAM`, 12 fps, q 6) while the page
  scrolled non-stop for 20 s: 1280x800 used 20% of a core and 1.0 MB/s;
  1600x1000 used 31% of a core and 1.6 MB/s. Idle, both send nothing
  (mpdecimate). 1600x1000 was taken: the terminal needs the width.
"""

import pytest

from server import browser, sandbox
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "desk-computer"


def boxes_overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def test_the_screen_is_big_enough_for_a_browser_and_a_terminal():
    assert sandbox.SIZE == (1600, 1000)
    assert sandbox.IMAGE == "agent-deck/desk-computer:2"


def test_the_browser_fills_the_screen_above_the_taskbar():
    """OWNER 2026-10-01, of the app's Screen view: "why I see terminal with
    the browser". Screen is the browser, whole; the terminal is the app's own
    Terminal view (the PTY), and Both is the app putting the two side by
    side. The agent's screenshots get the full width too."""
    width, height = sandbox.SIZE
    layout = sandbox.desktop_layout(sandbox.SIZE)
    assert layout["browser"] == (0, 0, width, height - sandbox.TASKBAR)
    assert "terminal" not in layout, "the desktop opens no terminal window"
    assert not boxes_overlap(layout["browser"], layout["taskbar"])


def test_the_layout_is_pure_and_follows_the_size():
    small = sandbox.desktop_layout((1280, 800))
    big = sandbox.desktop_layout((1600, 1000))
    assert small != big
    assert sandbox.desktop_layout((1600, 1000)) == big


def test_chromium_opens_in_its_place_not_over_the_whole_screen():
    argv = sandbox.browser_argv("acme")
    x, y, w, h = sandbox.desktop_layout(sandbox.SIZE)["browser"]
    cw, ch = sandbox.client_size(w, h)
    assert f"--window-position={x},{y}" in argv
    assert f"--window-size={cw},{ch}" in argv
    assert sandbox.SIZE != (cw, ch)


def test_chromium_position_defaults_to_the_corner_on_the_mac_path():
    argv = browser.chrome_argv(display=":99", cdp_port=9222,
                               profile_dir="/p", url="about:blank")
    assert "--window-position=0,0" in argv


def test_the_desktop_starts_inside_the_container_detached():
    argv = sandbox.desktop_argv("acme")
    assert argv[:3] == ["docker", "exec", "--detach"]
    assert sandbox.container_name("acme") in argv
    assert f"{sandbox.DESK_UID}:{sandbox.DESK_GID}" in argv
    assert argv[-3:-1] == ["bash", "-c"]


def test_the_desktop_starts_the_window_manager_and_tmux_but_no_terminal_window():
    """The xterm tiled beside Chromium is what made Screen and Both look the
    same. The tmux sessions stay: the app's Terminal attaches to `deck`, and
    the taskbar's Terminal and Agent buttons still open a window on demand."""
    script = sandbox.desktop_argv("acme")[-1]
    assert "jwm" in script
    assert "new-session -d -s deck" in script
    assert "new-session -d -s agent" in script
    assert "xterm" not in script
    # An image's own copy of this would be a second answer (and the old one
    # opened the xterm); the deck's argv is the one that runs.
    assert "deck-desktop" not in script


class Docker:
    def __init__(self, fail_exec=False):
        self.calls = []
        self.fail_exec = fail_exec

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        bad = self.fail_exec and argv[1] == "exec"
        return type("P", (), {"returncode": 1 if bad else 0, "stdout": b"",
                              "stderr": b"no such file" if bad else b""})()


def test_a_new_computer_gets_its_desktop(tmp_path, monkeypatch):
    docker = Docker()
    monkeypatch.setattr(sandbox, "_run", docker)
    monkeypatch.setattr(sandbox, "is_up", lambda desk: False)
    sandbox.start("acme", home=tmp_path / "home")
    verbs = [argv[1] for argv in docker.calls]
    assert verbs.index("run") < len(verbs) - 1
    assert docker.calls[-1] == sandbox.desktop_argv("acme")


def test_an_old_image_without_a_desktop_still_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "_run", Docker(fail_exec=True))
    monkeypatch.setattr(sandbox, "is_up", lambda desk: False)
    computer = sandbox.start("acme", home=tmp_path / "home")
    assert computer.name == sandbox.container_name("acme")


def test_the_container_knows_its_desk_name_for_the_prompt(tmp_path):
    argv = sandbox.create_argv("acme", home=tmp_path / "home")
    assert "DECK_DESK=acme" in argv


# ── the image carries what the desktop and the terminal run ─────────────────


def test_the_image_installs_the_desktop_and_the_terminal():
    text = (DOCKERFILE / "Dockerfile").read_text()
    for package in ("jwm", "xterm", "tmux", "procps", "vim"):
        assert f"      {package} \\" in text, f"{package} is not installed"
    for name in ("tmux.conf", "jwmrc"):
        assert (DOCKERFILE / name).is_file()
        assert f"COPY {name} " in text
    assert "deck-desktop" not in text, "the deck starts the desktop, not the image"


def test_the_images_default_screen_is_the_decks_size():
    width, height = sandbox.SIZE
    text = (DOCKERFILE / "Dockerfile").read_text()
    assert f'"{width}x{height}x24"' in text


def test_tmux_is_told_the_shell_because_the_account_has_none():
    conf = (DOCKERFILE / "tmux.conf").read_text()
    assert "default-shell /bin/bash" in conf
    assert "escape-time" in conf  # vim's Escape is not half a second late
