"""A click in the take-over panel must not wait for the pointer to move.

MEASURED on the box, 2026-09-28, through the real `/v1` against desk atlas:
the panel sends `move` while the mouse travels (hover), so by the time the
owner presses, the pointer is already where he clicks. Every click then took

    POST .../screen/input {"action":"click"}   ->  15.43 s .. 15.55 s
    click -> next frame showing it             ->  15.91 s .. 15.99 s

and a bare `xdotool mousemove --sync 5 5` run twice in the container took
0.14 s the first time and 15.3 s the second. Debian 12's xdotool
(3.20160805) implements `--sync` as "wait until the pointer has moved", which
never happens when it is already there, so it sits out its own timeout before
the click is sent. That is the owner's "it has delay".

`--sync` bought nothing to begin with: the warp and the button press go down
ONE X connection inside ONE xdotool process, and the X server applies a
client's requests in order, so the press cannot overtake the move.

Swept across the class, not just `click`: every gesture that moves the pointer
(`click`, `move`, `scroll`, `drag`, every button and count) is built here and
none may carry `--sync`.
"""

import pytest

from server import sandbox

DESK = "acme"

GESTURES = [
    ("click", lambda: sandbox.click_argv(DESK, 301, 301)),
    ("right click", lambda: sandbox.click_argv(DESK, 301, 301, button=3)),
    ("double click", lambda: sandbox.click_argv(DESK, 301, 301, count=2)),
    ("hover", lambda: sandbox.move_argv(DESK, 400, 300)),
    ("scroll", lambda: sandbox.scroll_argv(DESK, 640, 400, dy=-3)),
    ("drag", lambda: sandbox.drag_argv(DESK, 10, 20, 300, 400)),
]


@pytest.mark.parametrize("name,build", GESTURES, ids=[g[0] for g in GESTURES])
def test_no_gesture_waits_for_the_pointer(name, build):
    argv = build()
    assert "xdotool" in argv, f"{name} is not an xdotool gesture any more"
    assert "--sync" not in argv, (
        f"{name} still waits for the pointer to move; a click where the hover "
        f"already left it sits out xdotool's timeout (15.5 s measured)")


def test_a_click_still_moves_there_first():
    """Dropping the wait must not drop the move: the press lands where he
    clicked, not wherever the pointer happened to be."""
    argv = sandbox.click_argv(DESK, 301, 302)
    tail = argv[argv.index("xdotool"):]
    assert tail == ["xdotool", "mousemove", "301", "302", "click", "1"]
