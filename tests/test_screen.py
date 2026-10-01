"""Watching an agent's screen: one frame at a time.

Written before `server/screen.py` exists.

`server/browser.py` can already build an ffmpeg x11grab command, and nothing
consumes it -- so there is no way to see what an agent's browser is doing, and
the take-over flow ("you drive, then hand back") has no surface to take over.
This is the missing half: a still frame per desk, refreshed while somebody is
looking, cached on disk, swept when it goes stale.

Four things are pinned here, each one a way the panel lies to Sam rather than
merely failing:

  * **A stale frame is indistinguishable from a live one.** A JPEG of a
    checkout page looks identical whether it was taken a second ago or twenty
    minutes ago, before the capture died. "The agent is sitting still" and
    "the feed is dead" are opposite facts and the panel must be able to tell
    them apart, so `newest()` returns an **age** and the age is asserted to be
    a real number of seconds -- not merely a path that exists.
  * **Frames accumulate until the disk is gone.** `sweep()` removes them. The
    assertion that matters is the one about what SURVIVES: a sweep that
    deleted every frame on the box would satisfy "the old one is gone", and
    would also mean the panel is permanently blank.
  * **One desk's frame served for another.** The frame path comes from a desk
    name that a human types into roster.json. A name is refused, not
    sanitised -- quietly turning "../../etc" into "etc" writes a chosen
    filename into a directory nobody chose.
  * **The capture reads the real desktop.** x11grab points at whatever display
    it is handed. Handed the wrong one -- or `$DISPLAY` from the shell that
    launched the daemon -- it publishes Sam's own screen, his mail and his
    terminals, into a panel. So `-i` is asserted to be **exactly** the virtual
    display passed in, and the ambient `DISPLAY` is asserted absent from the
    argv.

Nothing here runs ffmpeg, starts Xvfb, or touches a real display: every test
is an argv or a tmp_path. Anything needing a real X server is `-m live`.
"""

import os
import time
from pathlib import Path

import pytest

from server import screen as S

# A one-pixel-ish blob. Not a real JPEG; nothing here decodes it.
FRAME = b"\xff\xd8\xff\xe0frame-bytes\xff\xd9"


# ── 4. the capture must not leak the real desktop ──────────────────────────


def test_snapshot_grabs_exactly_the_display_it_was_given(tmp_path):
    """`-i` is the virtual display passed in, character for character.

    This is the whole safety property of the capture: ffmpeg grabs whatever
    display it is pointed at, and Sam's own desktop is `:0` on a Linux box.
    Asserted as an identity, not as "`:0` is absent" -- an argv that dropped
    the input flag entirely would pass that.
    """
    argv = S.snapshot_argv(":137", str(tmp_path / "f.jpg"))

    assert "-i" in argv, argv
    assert argv[argv.index("-i") + 1] == ":137"
    # And it is named once: a second `-i` would be a second source.
    assert argv.count("-i") == 1


def test_snapshot_ignores_the_ambient_display(tmp_path, monkeypatch):
    """The shell's own `$DISPLAY` never reaches the argv.

    The daemon is launched from a desktop session. If the builder ever falls
    back to the environment, the panel starts showing the human's screen and
    every window on it.
    """
    monkeypatch.setenv("DISPLAY", ":0")

    argv = S.snapshot_argv(":137", str(tmp_path / "f.jpg"))

    assert ":0" not in argv
    assert argv[argv.index("-i") + 1] == ":137"


def test_snapshot_takes_one_frame_and_stops(tmp_path):
    """One JPEG, then exit -- not a video pipeline left running.

    A frame grab that never terminates is a per-desk ffmpeg holding the CPU
    and the display forever, which is exactly the thing snapshots exist to
    avoid.
    """
    out = tmp_path / "f.jpg"
    argv = S.snapshot_argv(":137", str(out))

    assert "-frames:v" in argv, argv
    assert argv[argv.index("-frames:v") + 1] == "1"
    assert argv[-1] == str(out)
    assert argv[0] == "ffmpeg"
    assert "x11grab" in argv


def test_snapshot_refuses_a_display_that_is_not_one(tmp_path):
    """A display number is validated, so it cannot smuggle another argument."""
    for bad in (":99 -i :0", "$DISPLAY", "", "99", "localhost:0",
                ":99;whoami", None):
        with pytest.raises(ValueError):
            S.snapshot_argv(bad, str(tmp_path / "f.jpg"))


# ── 3. per-desk isolation ──────────────────────────────────────────────────


def test_frame_path_lands_inside_the_root(tmp_path):
    """A normal desk name gets a normal file, under the root it was given."""
    path = S.frame_path("acme", tmp_path)

    assert path.suffix == ".jpg"
    assert "acme" in path.name
    # Inside the root, not merely near it.
    assert tmp_path.resolve() in path.resolve().parents


def test_two_desks_never_share_a_frame(tmp_path):
    """Villas' screen can never be served as Acme's."""
    assert S.frame_path("acme", tmp_path) != S.frame_path("villas", tmp_path)


def test_frame_path_refuses_a_hostile_desk_name(tmp_path):
    """Refused, not sanitised.

    The name decides which file is written and which file is served back. A
    traversal that got quietly trimmed to its last segment would write a
    chosen filename into a directory nobody chose.
    """
    for bad in ("../../etc", "..", "a/b", "/etc/passwd", "", ".", "x" * 200):
        with pytest.raises(ValueError):
            S.frame_path(bad, tmp_path)


def test_cache_refuses_a_hostile_desk_name(tmp_path):
    """The same refusal on the way in and on the way out."""
    cache = S.ScreenCache(tmp_path)

    with pytest.raises(ValueError):
        cache.put("../../etc", FRAME)
    with pytest.raises(ValueError):
        cache.newest("../../etc")


# ── 1. a stale frame must be identifiable as stale ─────────────────────────


def test_newest_reports_a_real_age_for_an_old_frame(tmp_path):
    """The panel can tell "idle agent" from "dead feed".

    Both look like a still picture. The age is the only thing that separates
    them, so it is asserted as a number of seconds in the right region -- a
    path alone, or an age hard-coded to 0.0, would leave the panel confidently
    showing a twenty-minute-old checkout as live.
    """
    cache = S.ScreenCache(tmp_path)
    path = cache.put("acme", FRAME)

    old = time.time() - 1200
    os.utime(path, (old, old))

    found = cache.newest("acme")
    assert found is not None
    got_path, age = found
    assert got_path == path
    assert 1190 <= age <= 1260, age


def test_newest_reports_a_small_age_for_a_fresh_frame(tmp_path):
    """A frame written just now is young, and says so."""
    cache = S.ScreenCache(tmp_path)
    cache.put("acme", FRAME)

    found = cache.newest("acme")
    assert found is not None
    _, age = found
    assert 0.0 <= age < 30.0, age


def test_newest_is_none_when_nothing_was_ever_captured(tmp_path):
    """No frame is an honest answer, not an error and not a stale path."""
    cache = S.ScreenCache(tmp_path)
    assert cache.newest("acme") is None


def test_put_returns_the_bytes_that_were_put(tmp_path):
    """What the panel serves is what the capture produced."""
    cache = S.ScreenCache(tmp_path)
    path = cache.put("acme", FRAME)

    assert path.read_bytes() == FRAME


def test_put_keeps_only_the_newest_frame_per_desk(tmp_path):
    """One file per desk: the disk cost of a watched desk is bounded.

    Asserted by listing the directory rather than by checking the old file is
    gone -- a second frame landing beside the first is how "capped on disk"
    quietly stops being true.
    """
    cache = S.ScreenCache(tmp_path)
    cache.put("acme", FRAME)
    cache.put("acme", b"\xff\xd8second\xff\xd9")

    path = S.frame_path("acme", tmp_path)
    assert path.read_bytes() == b"\xff\xd8second\xff\xd9"
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_put_refuses_a_frame_too_big_to_cache(tmp_path):
    """The on-disk cap is enforced at the door.

    A capture pointed at a huge display, or an ffmpeg writing something that
    is not a still, must not be allowed to fill the state directory one desk
    at a time.
    """
    cache = S.ScreenCache(tmp_path)

    with pytest.raises(ValueError):
        cache.put("acme", b"x" * (S.MAX_FRAME_BYTES + 1))
    with pytest.raises(ValueError):
        cache.put("acme", b"")

    assert cache.newest("acme") is None


# ── 2. frames must not accumulate forever ──────────────────────────────────


def test_sweep_drops_the_stale_frame_and_keeps_the_current_one(tmp_path):
    """The current frame SURVIVES.

    A sweep that deleted everything would also make "the old one is gone"
    true, and would leave every panel on the box permanently blank. So the
    live desk is asserted still readable *after* the sweep, with its bytes
    intact, and the swept desk is named in the return value so a caller can
    say which feed went away.
    """
    cache = S.ScreenCache(tmp_path)
    fresh = cache.put("acme", FRAME)
    stale = cache.put("villas", FRAME)

    now = time.time()
    os.utime(stale, (now - 3600, now - 3600))

    dropped = cache.sweep(now=now, max_age=600)

    assert dropped == ["villas"]
    assert not stale.exists()
    assert fresh.exists()
    assert fresh.read_bytes() == FRAME
    assert cache.newest("acme") is not None
    assert cache.newest("villas") is None


def test_sweep_with_nothing_stale_removes_nothing(tmp_path):
    """A quiet sweep is a no-op, and says so by returning an empty list."""
    cache = S.ScreenCache(tmp_path)
    cache.put("acme", FRAME)
    cache.put("villas", FRAME)

    dropped = cache.sweep(now=time.time(), max_age=600)

    assert dropped == []
    assert cache.newest("acme") is not None
    assert cache.newest("villas") is not None


def test_sweep_survives_a_root_that_was_never_written_to(tmp_path):
    """Called on a cold daemon before any desk has been watched."""
    cache = S.ScreenCache(tmp_path / "never-used")
    assert cache.sweep(now=time.time(), max_age=600) == []


def test_sweep_ignores_files_that_are_not_frames(tmp_path):
    """Only this cache's own frames are ever unlinked.

    `sweep` runs on a timer against a directory under the deck's state dir. It
    deletes by pattern, so anything that is not a frame it wrote is left
    exactly where it is.
    """
    cache = S.ScreenCache(tmp_path)
    frame = cache.put("acme", FRAME)
    stranger = frame.parent / "notes.txt"
    stranger.write_text("keep me", encoding="utf-8")

    # Both are equally ancient. Only the one this cache wrote may go.
    old = time.time() - 9999
    os.utime(stranger, (old, old))
    os.utime(frame, (old, old))

    dropped = cache.sweep(now=time.time(), max_age=600)

    assert dropped == ["acme"]
    assert stranger.read_text(encoding="utf-8") == "keep me"


# ── the module says why it is snapshots ────────────────────────────────────


def test_module_explains_why_snapshots_and_not_a_stream():
    """The reasoning is in the file, so nobody swaps in a socket by reflex.

    A still frame every second or two is enough to see what a browser is
    doing, costs nothing when nobody is looking, and cannot wedge the event
    loop. That trade-off is the design, and a design that is not written down
    gets replaced by the first person who thinks "this should be a stream".
    """
    doc = (S.__doc__ or "").lower()
    assert "snapshot" in doc or "still" in doc
    assert "stream" in doc
    assert "event loop" in doc


def test_screen_module_is_stdlib_only():
    """No new dependency enters the deck for a picture."""
    source = Path(S.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            assert "requests" not in line and "PIL" not in line, line
