from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from server.routines import (Routine, next_fire, due, advance, record_run,
                             runs, load_routines, save_routines, MAX_RUNS)


def ts(y, m, d, hh, mm, tz="Europe/London"):
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).timestamp()


def test_weekday_8am_fires_at_8am_on_a_weekday():
    """The canonical routine: '0 8 * * 1-5'. Asserts the GOOD signal -- an exact
    expected timestamp -- because a cron parser that returned 'some time in the
    future' would pass a looser test and still be wrong every day."""
    after = ts(2026, 9, 1, 7, 0)          # Tue 07:00
    assert next_fire("0 8 * * 1-5", "Europe/London", after) == ts(2026, 9, 1, 8, 0)


def test_friday_evening_skips_the_weekend_to_monday():
    after = ts(2026, 9, 4, 9, 0)          # Fri 09:00, after that day's fire
    assert next_fire("0 8 * * 1-5", "Europe/London", after) == ts(2026, 9, 7, 8, 0)


def test_next_fire_is_strictly_after_never_equal():
    """If it can return `after` itself, a routine re-fires in a tight loop."""
    at8 = ts(2026, 9, 1, 8, 0)
    assert next_fire("0 8 * * 1-5", "Europe/London", at8) > at8


def test_every_three_hours_step_syntax():
    after = ts(2026, 9, 1, 10, 30)
    assert next_fire("0 */3 * * *", "Europe/London", after) == ts(2026, 9, 1, 12, 0)


def test_timezone_is_honoured_not_assumed_utc():
    london = next_fire("0 8 * * *", "Europe/London", ts(2026, 9, 1, 0, 0))
    tokyo = next_fire("0 8 * * *", "Asia/Tokyo", ts(2026, 9, 1, 0, 0))
    assert london != tokyo


def test_an_unparseable_spec_is_a_clear_error():
    with pytest.raises(ValueError):
        next_fire("not a cron", "Europe/London", 0.0)


def test_due_returns_a_routine_whose_time_has_passed():
    r = Routine(id="a", agent="acme", prompt="go",
                trigger={"kind": "cron", "spec": "0 8 * * *", "tz": "UTC"},
                next_run_at=100.0)
    assert due([r], now=200.0) == [r]


def test_due_ignores_a_disabled_routine():
    r = Routine(id="a", agent="acme", prompt="go",
                trigger={"kind": "cron", "spec": "0 8 * * *", "tz": "UTC"},
                next_run_at=100.0, enabled=False)
    assert due([r], now=200.0) == []


def test_advance_persists_so_a_restart_does_not_refire(tmp_path):
    """The LaunchAgent restarts this process. An in-memory schedule dies with
    it; a routine that already ran must not run again after a restart."""
    path = tmp_path / "routines.json"
    r = Routine(id="a", agent="acme", prompt="go",
                trigger={"kind": "cron", "spec": "0 8 * * *", "tz": "UTC"},
                next_run_at=ts(2026, 9, 1, 8, 0, "UTC"))
    save_routines(path, [r])
    now = ts(2026, 9, 1, 8, 0, "UTC") + 1
    advance(path, "a", now=now)
    reloaded = load_routines(path)[0]          # simulates the restart
    assert reloaded.next_run_at > now
    assert due([reloaded], now=now) == []


def test_run_history_is_capped(tmp_path):
    path = tmp_path / "routines.json"
    save_routines(path, [Routine(id="a", agent="p", prompt="g",
                                 trigger={"kind": "event", "event": "X"})])
    for i in range(MAX_RUNS + 5):
        record_run(path, "a", ts=float(i), ok=True, detail=f"run {i}")
    history = runs(path, "a")
    assert len(history) == MAX_RUNS
    assert history[-1]["detail"] == f"run {MAX_RUNS + 4}"   # newest kept
