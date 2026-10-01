"""After a wake, a lingering DEAD card must not outrank the live one."""

from server.roster import Desk, occupancy


def _desk():
    return Desk(name="acme", cwd="/tmp", engine="claude", mission="",
                label="", charter="", reports_to=None)


def _card(state, sid):
    return {"name": "acme", "state": state, "session_id": sid, "pid": 1}


def _row(sessions):
    return occupancy([_desk()], sessions)[0]


def test_live_card_wins_when_the_dead_one_comes_last():
    row = _row([_card("WORKING", "live"), _card("DEAD", "old")])
    assert row["session_id"] == "live" and row["state"] == "WORKING"


def test_live_card_wins_when_the_dead_one_comes_first():
    row = _row([_card("DEAD", "old"), _card("IDLE", "live")])
    assert row["session_id"] == "live"


def test_all_dead_still_seats_a_card():
    row = _row([_card("DEAD", "a"), _card("DEAD", "b")])
    assert row["state"] == "DEAD"
