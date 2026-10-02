"""Mac control, the store half: an `input` job kind and a per-session grant.

Owner, 2026-10-01: desks reach his Mac only through run / read / write / open
and a still screenshot. He wants them to click, type, press keys and scroll
on it the way they drive their own computers -- gated behind a switch he
turns on per session, which expires.

The store is where the deck learns that state. The Mac reports its control
grant in every poll (the Mac decides; the deck only mirrors it), the deck
files at most one "turn on Mac control" card per desk, and a viewer opening
the live screen is what tells the Mac to start capturing.

Hermetic: a tmp store, a fake clock.
"""

import pytest

from server import mac_nodes


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def store(tmp_path):
    return mac_nodes.Store(tmp_path / "mac", clock=Clock())


def _pair(store, caps=None):
    row = store.register("m-1", "Studio", "macOS 26", "2.0",
                         caps or list(mac_nodes.CAPABILITIES), "ask", None)
    return row["node_id"]


SPACE = {"width": 1440, "height": 900}


# ── the input job kind ───────────────────────────────────────────────────────


def test_input_is_a_job_kind_and_a_capability():
    assert "input" in mac_nodes.KINDS
    assert "input" in mac_nodes.CAPABILITIES


@pytest.mark.parametrize("args, want", [
    ({"action": "click", "x": 10, "y": 20, "space": SPACE},
     {"action": "click", "x": 10, "y": 20, "button": "left", "count": 1,
      "space": SPACE}),
    ({"action": "click", "x": 0, "y": 0, "button": 3, "count": 2,
      "space": SPACE},
     {"action": "click", "x": 0, "y": 0, "button": "right", "count": 2,
      "space": SPACE}),
    ({"action": "move", "x": 5, "y": 6, "space": SPACE},
     {"action": "move", "x": 5, "y": 6, "space": SPACE}),
    ({"action": "drag", "x": 1, "y": 2, "to_x": 3, "to_y": 4, "space": SPACE},
     {"action": "drag", "x": 1, "y": 2, "to_x": 3, "to_y": 4,
      "button": "left", "space": SPACE}),
    ({"action": "scroll", "x": 1, "y": 2, "dy": -3, "space": SPACE},
     {"action": "scroll", "x": 1, "y": 2, "dy": -3, "space": SPACE}),
    ({"action": "type", "text": "hello"}, {"action": "type", "text": "hello"}),
    ({"action": "key", "key": "cmd+s"}, {"action": "key", "key": "cmd+s"}),
])
def test_input_args_are_normalised(args, want):
    assert mac_nodes.validate_args("input", args) == want


@pytest.mark.parametrize("args", [
    {"action": "punch"},
    {"action": "click", "x": 10, "y": 20},                       # no space
    {"action": "click", "x": 1440, "y": 20, "space": SPACE},     # off-screen
    {"action": "click", "x": -1, "y": 20, "space": SPACE},
    {"action": "click", "x": "1", "y": 20, "space": SPACE},
    {"action": "click", "x": 1, "y": 2, "button": 4, "space": SPACE},
    {"action": "click", "x": 1, "y": 2, "count": 4, "space": SPACE},
    {"action": "drag", "x": 1, "y": 2, "to_x": 1440, "to_y": 4, "space": SPACE},
    {"action": "scroll", "x": 1, "y": 2, "space": SPACE},
    {"action": "scroll", "x": 1, "y": 2, "dy": 1, "dx": 1, "space": SPACE},
    {"action": "scroll", "x": 1, "y": 2, "dy": 11, "space": SPACE},
    {"action": "type", "text": ""},
    {"action": "type", "text": "x" * 4097},
    {"action": "key", "key": "--file /etc/passwd"},
    {"action": "key", "key": ""},
])
def test_bad_input_args_are_refused(args):
    with pytest.raises(mac_nodes.MacError) as err:
        mac_nodes.validate_args("input", args)
    assert err.value.reason == "bad_input"


def test_the_summary_says_what_was_done_and_caps_typed_text():
    assert mac_nodes.summary_of("input", {"action": "click", "x": 3, "y": 4,
                                          "button": "left", "count": 2}) \
        == "double-click 3,4"
    assert mac_nodes.summary_of("input", {"action": "key", "key": "cmd+s"}) \
        == "key cmd+s"
    typed = mac_nodes.summary_of("input", {"action": "type",
                                           "text": "a" * 100})
    assert typed.startswith("type ") and len(typed) < 70


# ── the grant, mirrored from the Mac ─────────────────────────────────────────


def _poll(store, node, control=None, screen=None, perms=None):
    return store.poll(node, 4, [], "ask", {}, control=control, screen=screen,
                      perms=perms)


def test_a_poll_records_the_macs_control_grant_screen_and_permissions(store):
    node = _pair(store)
    now = store.now()
    _poll(store, node, control={"scope": "atlas", "until": now + 1800},
          screen={"width": 1440, "height": 900},
          perms={"accessibility": True, "screen_recording": False})
    row = store.node(node)
    assert row["control"] == {"scope": "atlas", "until": now + 1800}
    assert row["screen"] == {"width": 1440, "height": 900}
    assert row["perms"] == {"accessibility": True, "screen_recording": False}
    _poll(store, node)   # an older app, or the grant ended: no control
    assert store.node(node).get("control") is None


def test_control_is_live_only_for_its_scope_and_until_it_expires(store):
    node = _pair(store)
    now = store.now()
    _poll(store, node, control={"scope": "atlas", "until": now + 60})
    row = store.node(node)
    assert mac_nodes.control_live(row, "atlas", now)
    assert not mac_nodes.control_live(row, "scout", now)
    assert mac_nodes.control_live(row, mac_nodes.OWNER_DESK, now)
    assert not mac_nodes.control_live(row, "atlas", now + 61)
    _poll(store, node, control={"scope": "all", "until": now + 60})
    assert mac_nodes.control_live(store.node(node), "scout", now)


def test_a_bad_control_report_is_refused(store):
    node = _pair(store)
    with pytest.raises(mac_nodes.MacError):
        _poll(store, node, control={"scope": "atlas", "until": "soon"})
    with pytest.raises(mac_nodes.MacError):
        _poll(store, node, control={"scope": "", "until": 5.0})


def test_listing_says_whether_control_is_on_and_the_screen_size(store):
    node = _pair(store)
    now = store.now()
    _poll(store, node, control={"scope": "all", "until": now + 60},
          screen={"width": 1440, "height": 900},
          perms={"accessibility": True, "screen_recording": True})
    (row,) = store.listing()
    assert row["control"] == {"live": True, "scope": "all", "until": now + 60}
    assert row["screen"] == {"width": 1440, "height": 900}
    assert row["perms"] == {"accessibility": True, "screen_recording": True}


# ── one ask per desk; it closes when the grant covers it ─────────────────────


def test_control_asks_are_one_per_desk_and_close_when_granted(store):
    node = _pair(store)
    assert store.control_ask(node, "atlas") is None
    store.set_control_ask(node, "atlas", "h-1")
    store.set_control_ask(node, "scout", "h-2")
    assert store.control_ask(node, "atlas") == "h-1"
    assert sorted(store.control_asks(node)) == ["atlas", "scout"]
    now = store.now()
    _poll(store, node, control={"scope": "atlas", "until": now + 60})
    assert store.take_control_granted(node) == {"atlas": "h-1"}
    assert store.control_ask(node, "atlas") is None
    assert store.control_asks(node) == ["scout"]
    assert store.take_control_granted(node) == {}


# ── someone is watching the live screen ──────────────────────────────────────


def test_watch_marks_the_node_for_a_few_seconds(store):
    node = _pair(store)
    assert not mac_nodes.watching(store.node(node), store.now())
    store.watch(node)
    assert mac_nodes.watching(store.node(node), store.now())
    store.clock.t += mac_nodes.WATCH_TTL + 1
    assert not mac_nodes.watching(store.node(node), store.now())


def test_watch_does_not_rewrite_the_node_file_on_every_frame(store):
    node = _pair(store)
    store.watch(node)
    before = store.nodes_path.stat().st_mtime_ns
    store.clock.t += 1
    store.watch(node)
    assert store.nodes_path.stat().st_mtime_ns == before


# ── input jobs: the owner's own hands, and no pile of files ─────────────────


def _press(store, node, desk):
    """Queue one key, let the Mac claim and finish it."""
    store.enqueue(desk, "input", {"action": "key", "key": "a"}, mac=None)
    claimed, _ = _poll(store, node)
    for j in claimed:
        store.record_events(node, j["id"], [{"seq": 1, "type": "result",
                                             "data": {"state": "done"}}])


def test_the_owner_may_type_faster_than_a_desk_may_queue(store):
    node = _pair(store)
    _poll(store, node)
    for _ in range(mac_nodes.PER_DESK_PER_MIN + 5):
        _press(store, node, mac_nodes.OWNER_DESK)
    with pytest.raises(mac_nodes.MacError) as err:
        for _ in range(mac_nodes.PER_DESK_PER_MIN + 1):
            _press(store, node, "atlas")
    assert err.value.reason == "rate_limited"


def test_settled_input_jobs_are_purged_after_minutes_not_days(store):
    node = _pair(store)
    _poll(store, node)
    job = store.enqueue("atlas", "input", {"action": "key", "key": "a"},
                        mac=None)
    run = store.enqueue("atlas", "run", {"command": "ls"}, mac=None)
    (claimed, _) = _poll(store, node)
    for j in claimed:
        store.record_events(node, j["id"], [{"seq": 1, "type": "result",
                                             "data": {"state": "done"}}])
    store.clock.t += mac_nodes.INPUT_KEEP + 1
    store.sweep()
    ids = {j["id"] for j in store._all_jobs()}
    assert job["id"] not in ids and run["id"] in ids
