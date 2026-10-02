from server.roster import (
    Desk, live_desks, occupancy, load_roster, save_roster, upsert, remove,
)

ACME = Desk(name="acme-growth", cwd="/p", engine="claude", mission="ads")


def test_a_desk_with_no_live_session_is_offline_not_missing():
    """The whole point of the roster. Today a card vanishes when its Terminal
    tab closes; a desk must survive that. Asserts the GOOD signal: the desk is
    present AND labelled OFFLINE."""
    rows = occupancy([ACME], [])
    assert len(rows) == 1
    assert rows[0]["name"] == "acme-growth"
    assert rows[0]["state"] == "OFFLINE"
    assert rows[0]["session_id"] is None


def test_a_desk_with_a_live_session_shows_that_sessions_state():
    live = [{"name": "acme-growth", "state": "WORKING",
             "session_id": "abc", "pid": 42}]
    rows = occupancy([ACME], live)
    assert rows[0]["state"] == "WORKING"
    assert rows[0]["session_id"] == "abc"
    assert rows[0]["pid"] == 42


def test_a_live_session_with_no_desk_is_still_listed():
    """The deck must not start hiding ad-hoc sessions the moment rosters exist."""
    live = [{"name": "scratch-01", "state": "IDLE",
             "session_id": "z", "pid": 9}]
    rows = occupancy([], live)
    assert [r["name"] for r in rows] == ["scratch-01"]
    assert rows[0]["desk"] is False


def test_live_desks_ignores_sessions_that_are_not_desks():
    """A cap on the org counts desks, not every process the collector can see.
    GOOD signal: one seated desk plus five unrelated sessions still counts 1."""
    live = [{"name": "acme-growth", "state": "WORKING",
             "session_id": "abc", "pid": 42}] + [
        {"name": f"scratch-{i}", "state": "IDLE", "session_id": f"s{i}", "pid": i}
        for i in range(5)
    ]
    assert live_desks([ACME], live) == 1


def test_live_desks_counts_every_seated_desk():
    """GOOD signal, the other half: desks that are genuinely occupied all count."""
    other = Desk(name="acme-sales", cwd="/p", engine="claude", mission="sell")
    live = [
        {"name": "acme-growth", "state": "WORKING", "session_id": "abc", "pid": 42},
        {"name": "acme-sales", "state": "IDLE", "session_id": "def", "pid": 43},
    ]
    assert live_desks([ACME, other], live) == 2


def test_live_desks_excludes_offline_desks():
    assert live_desks([ACME], []) == 0


def test_live_desks_excludes_a_card_fading_out_as_dead():
    """A DEAD card lingers ~60s for the board's fade-out; it is not actually
    running and must not hold a hire slot open."""
    live = [{"name": "acme-growth", "state": "DEAD", "session_id": "abc", "pid": 42}]
    assert live_desks([ACME], live) == 0


def test_upsert_replaces_by_name_and_does_not_duplicate(tmp_path):
    path = tmp_path / "roster.json"
    upsert(path, ACME)
    upsert(path, Desk(name="acme-growth", cwd="/q", engine="claude", mission="x"))
    desks = load_roster(path)
    assert len(desks) == 1
    assert desks[0].cwd == "/q"


def test_roster_round_trips(tmp_path):
    path = tmp_path / "roster.json"
    save_roster(path, [ACME])
    assert load_roster(path) == [ACME]


def test_remove_leaves_the_others(tmp_path):
    path = tmp_path / "roster.json"
    save_roster(path, [ACME, Desk(name="b", cwd="/b", engine="claude", mission="")])
    remove(path, "acme-growth")
    assert [d.name for d in load_roster(path)] == ["b"]
