from server.sources import comms

CARDS = [
    {"session_id": "sid-a", "name": "orion-4b", "pid": 84795, "state": "WORKING"},
    {"session_id": "sid-b", "name": "orion-5c", "pid": 67127, "state": "DONE"},
]
TEXT = "Owner ruling relayed from the main session: merge #372."


def directory():
    return comms.build_directory(CARDS)


def test_both_sides_of_one_message_collapse_to_a_single_edge():
    index = comms.CommsIndex()
    index.add({"dir": "out", "owner": "sid-a", "ts": 1000.0,
               "to_label": "orion-5c [648cc3]", "text": TEXT})
    index.add({"dir": "in", "owner": "sid-b", "ts": 1002.0,
               "from_sock": "uds:/tmp/cc-socks/84795.sock",
               "from_name": "orion-4b", "text": TEXT})

    edges = index.edges(directory())
    assert len(edges) == 1
    edge = edges[0]
    assert edge["from"]["session_id"] == "sid-a"
    assert edge["to"]["session_id"] == "sid-b"
    assert edge["seen"] == "both"
    assert edge["status"] == "delivered"


def test_sender_only_edge_is_kept_as_sent():
    index = comms.CommsIndex()
    index.add({"dir": "out", "owner": "sid-a", "ts": 1000.0,
               "to_label": "orion-5c [648cc3]", "text": TEXT})
    edge = index.edges(directory())[0]
    assert edge["seen"] == "sender"
    assert edge["status"] == "sent"
    assert edge["to"]["session_id"] == "sid-b", "target resolved by name"


def test_receiver_only_edge_resolves_the_sender_by_pid():
    index = comms.CommsIndex()
    index.add({"dir": "in", "owner": "sid-b", "ts": 1002.0,
               "from_sock": "uds:/tmp/cc-socks/84795.sock",
               "from_name": "whatever the sender called itself", "text": TEXT})
    edge = index.edges(directory())[0]
    assert edge["seen"] == "receiver"
    assert edge["status"] == "delivered"
    assert edge["from"]["session_id"] == "sid-a"
    assert edge["from"]["pid"] == 84795


def test_same_text_outside_the_pairing_window_stays_two_edges():
    index = comms.CommsIndex()
    index.add({"dir": "out", "owner": "sid-a", "ts": 1000.0,
               "to_label": "orion-5c", "text": TEXT})
    index.add({"dir": "out", "owner": "sid-a", "ts": 1000.0 + 10 * 60,
               "to_label": "orion-5c", "text": TEXT})
    assert len(index.edges(directory())) == 2


def test_unknown_endpoint_is_kept_never_dropped():
    index = comms.CommsIndex()
    index.add({"dir": "out", "owner": "sid-a", "ts": 1000.0,
               "to_label": "some-session-that-died", "text": TEXT})
    edge = index.edges(directory())[0]
    assert edge["to"]["session_id"] is None
    assert edge["to"]["name"] == "some-session-that-died"


def test_index_is_bounded_and_keeps_the_newest():
    index = comms.CommsIndex(max_edges=3)
    for i in range(10):
        index.add({"dir": "out", "owner": "sid-a", "ts": 1000.0 + i * 300,
                   "to_label": "orion-5c", "text": f"message {i}"})
    edges = index.edges(directory())
    assert len(edges) == 3
    assert edges[0]["text"] == "message 9", "newest first"
    assert edges[-1]["text"] == "message 7"


def test_a_replayed_record_does_not_duplicate_an_edge():
    index = comms.CommsIndex()
    raw = {"dir": "out", "owner": "sid-a", "ts": 1000.0,
           "to_label": "orion-5c", "text": TEXT}
    index.add(dict(raw))
    index.add(dict(raw))
    assert len(index.edges(directory())) == 1
