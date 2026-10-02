"""The Open button had four gestures missing: scroll, right-click,
double-click and drag. `sandbox.send_input` (`server/sandbox.py`) accepted
only `click`, `type` and `key`, and `click_argv` hardcoded `click 1` -- so a
page that needed a scroll or a right-click menu could not be worked from the
panel at all.

Written before the new argv builders exist.

Pure argv identity, same discipline as `tests/test_sandbox.py`: no Docker, no
X server, no container. Every assertion is "the correct command was built",
never merely "nothing raised" -- a test that only checks for the absence of
an exception passes on a builder that silently does nothing.

The regression pin (`test_the_three_original_gestures_are_untouched`) is the
load-bearing one: the contract requires the three pre-existing actions to
keep building byte-identical argv, and this pins today's captured output so a
refactor of the new dispatch cannot quietly reshape the old one.
"""

import pytest

from server import sandbox

DESK = "acme"


# ── the regression pin ──────────────────────────────────────────────────────


def test_the_three_original_gestures_are_untouched():
    """Captured from `click_argv`/`type_argv`/`key_argv` before this change.
    A left click with no button/count given must still read `click 1` -- the
    long `--repeat --delay B` form is for the gestures that need it."""
    assert sandbox.click_argv(DESK, 301, 301) == [
        "docker", "exec", "--user", "1000:1000", "--env", "DISPLAY=:99",
        "--env", "HOME=/home/agent", "deck-desk-acme",
        "xdotool", "mousemove", "301", "301", "click", "1",
    ]
    assert sandbox.type_argv(DESK, "hi `there$") == [
        "docker", "exec", "--user", "1000:1000", "--env", "DISPLAY=:99",
        "--env", "HOME=/home/agent", "deck-desk-acme",
        "xdotool", "type", "--delay", "40", "--", "hi `there$",
    ]
    assert sandbox.key_argv(DESK, "ctrl+l") == [
        "docker", "exec", "--user", "1000:1000", "--env", "DISPLAY=:99",
        "--env", "HOME=/home/agent", "deck-desk-acme",
        "xdotool", "key", "--clearmodifiers", "ctrl+l",
    ]


def test_a_default_click_is_still_the_short_form():
    """`button=1, count=1` -- the only shape the old client ever sent -- must
    stay the two-token `click 1`, not grow a `--repeat --delay` nobody asked
    for on the common case."""
    argv = sandbox.click_argv(DESK, 10, 10)
    assert argv[-2:] == ["click", "1"]
    argv = sandbox.click_argv(DESK, 10, 10, button=1, count=1)
    assert argv[-2:] == ["click", "1"]


# ── click: button and count ─────────────────────────────────────────────────


def test_a_right_click_builds_the_long_form():
    argv = sandbox.click_argv(DESK, 50, 60, button=3)
    assert argv[-10:] == ["xdotool", "mousemove", "50", "60",
                         "click", "--repeat", "1", "--delay", "120", "3"]


def test_a_double_click_builds_the_long_form():
    argv = sandbox.click_argv(DESK, 50, 60, count=2)
    assert argv[-6:] == ["click", "--repeat", "2", "--delay", "120", "1"]


def test_a_middle_click_is_button_two():
    argv = sandbox.click_argv(DESK, 50, 60, button=2, count=3)
    assert argv[-6:] == ["click", "--repeat", "3", "--delay", "120", "2"]


def test_a_click_with_a_bad_button_or_count_is_refused():
    for kwargs in ({"button": 0}, {"button": 4}, {"button": -1},
                   {"count": 0}, {"count": 4}):
        with pytest.raises(ValueError):
            sandbox.click_argv(DESK, 10, 10, **kwargs)


def test_a_click_off_the_screen_is_still_refused_with_a_button_given():
    with pytest.raises(ValueError):
        sandbox.click_argv(DESK, 99999, 10, button=3)


# ── move ─────────────────────────────────────────────────────────────────


def test_move_builds_a_bare_mousemove_with_no_click():
    argv = sandbox.move_argv(DESK, 400, 300)
    assert argv[-3:] == ["mousemove", "400", "300"]
    assert "click" not in argv


def test_move_off_screen_is_refused():
    with pytest.raises(ValueError):
        sandbox.move_argv(DESK, -1, 300)


# ── scroll ───────────────────────────────────────────────────────────────


def test_scroll_down_builds_exactly_the_documented_argv():
    """`scroll {x:640,y:400,dy:-3}` -- the detector's load-bearing example."""
    argv = sandbox.scroll_argv(DESK, 640, 400, dy=-3)
    assert argv[-8:] == ["xdotool", "mousemove", "640", "400",
                        "click", "--repeat", "3", "5"]


def test_scroll_up_is_button_four():
    argv = sandbox.scroll_argv(DESK, 1, 1, dy=2)
    assert argv[-4:] == ["click", "--repeat", "2", "4"]


def test_scroll_right_is_button_seven_left_is_button_six():
    right = sandbox.scroll_argv(DESK, 5, 5, dx=4)
    assert right[-4:] == ["click", "--repeat", "4", "7"]
    left = sandbox.scroll_argv(DESK, 5, 5, dx=-4)
    assert left[-1] == "6"


def test_scroll_has_no_delay_flag_unlike_click():
    argv = sandbox.scroll_argv(DESK, 5, 5, dy=-1)
    assert "--delay" not in argv


def test_scroll_requires_exactly_one_of_dy_dx():
    with pytest.raises(ValueError):
        sandbox.scroll_argv(DESK, 5, 5)
    with pytest.raises(ValueError):
        sandbox.scroll_argv(DESK, 5, 5, dy=1, dx=1)


def test_scroll_amount_must_be_in_one_to_ten():
    for bad in (0, 11, -11):
        with pytest.raises(ValueError):
            sandbox.scroll_argv(DESK, 5, 5, dy=bad)


def test_scroll_off_screen_raises():
    """`{"action":"scroll","x":9999,...}` -- the detector's off-screen case,
    the one the route must turn into `400 bad_input`."""
    with pytest.raises(ValueError):
        sandbox.scroll_argv(DESK, 9999, 5, dy=-3)


# ── drag ─────────────────────────────────────────────────────────────────


def test_drag_builds_mousedown_move_mouseup_in_that_order():
    argv = sandbox.drag_argv(DESK, 10, 20, 300, 400)
    assert argv[-11:] == [
        "xdotool", "mousemove", "10", "20",
        "mousedown", "1",
        "mousemove", "300", "400",
        "mouseup", "1",
    ]


def test_drag_uses_the_given_button_on_both_ends():
    argv = sandbox.drag_argv(DESK, 10, 20, 300, 400, button=3)
    assert argv.count("3") == 2
    assert argv[argv.index("mousedown") + 1] == "3"
    assert argv[argv.index("mouseup") + 1] == "3"


def test_drag_bounds_checks_the_start_point():
    with pytest.raises(ValueError):
        sandbox.drag_argv(DESK, -1, 20, 300, 400)


def test_drag_bounds_checks_the_end_point_too():
    """Both endpoints checked -- a drag that starts on-screen and ends off it
    is still a coordinate the client's arithmetic got wrong."""
    with pytest.raises(ValueError):
        sandbox.drag_argv(DESK, 10, 20, 99999, 400)


def test_drag_bad_button_is_refused():
    with pytest.raises(ValueError):
        sandbox.drag_argv(DESK, 10, 20, 300, 400, button=9)


# ── send_input dispatch ────────────────────────────────────────────────────


def test_send_input_routes_the_new_kinds(monkeypatch):
    """`send_input` is the one seam the route calls through; each new action
    name must reach its builder, not fall into the `else` branch that used to
    be the only outcome for anything but click/type/key."""
    calls = []
    monkeypatch.setattr(sandbox, "_run", lambda argv, **kw: calls.append(argv)
                        or __import__("types").SimpleNamespace(
                            returncode=0, stdout=b"", stderr=b""))
    monkeypatch.setattr(sandbox, "is_up", lambda desk: True)

    sandbox.send_input(DESK, {"action": "move", "x": 1, "y": 1})
    sandbox.send_input(DESK, {"action": "scroll", "x": 1, "y": 1, "dy": -1})
    sandbox.send_input(DESK, {"action": "drag", "x": 1, "y": 1,
                              "to_x": 2, "to_y": 2})
    assert len(calls) == 3
    assert "mousedown" in calls[2]
