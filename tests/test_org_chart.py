from server.sources import comms

M, A, B, X = "sid-m", "sid-a", "sid-b", "sid-x"
NAMES = {M: "mgr", A: "fixer", B: "reviewer", X: "stranger"}


def edge(frm, to, ts, text="hi"):
    return {
        "id": f"{frm}{to}{ts}", "ts": float(ts), "text": text,
        "from": {"session_id": frm, "name": NAMES[frm], "pid": None},
        "to": {"session_id": to, "name": NAMES[to], "pid": None},
        "seen": "both", "status": "delivered",
    }


def test_reports_are_whoever_the_manager_exchanged_with():
    groups = comms.groups([edge(M, A, 100), edge(B, M, 200)], M)
    assert {r["session_id"] for r in groups["reports"]} == {A, B}


def test_a_report_that_only_received_still_counts():
    groups = comms.groups([edge(M, A, 100)], M)
    assert [r["session_id"] for r in groups["reports"]] == [A]
    assert groups["reports"][0]["msg_count"] == 1
    assert groups["reports"][0]["name"] == "fixer"


def test_traffic_between_two_reports_is_a_peer_link():
    groups = comms.groups([edge(M, A, 100), edge(M, B, 110), edge(A, B, 120)], M)
    assert len(groups["peer_links"]) == 1
    link = groups["peer_links"][0]
    assert {link["a"], link["b"]} == {A, B}
    assert link["count"] == 1
    assert {link["a_name"], link["b_name"]} == {"fixer", "reviewer"}


def test_traffic_touching_no_report_lands_in_elsewhere():
    groups = comms.groups([edge(M, A, 100), edge(X, B, 120)], M)
    assert len(groups["elsewhere"]) == 1
    assert {groups["elsewhere"][0]["a"], groups["elsewhere"][0]["b"]} == {X, B}


def test_the_manager_s_own_links_are_not_repeated_as_peers():
    groups = comms.groups([edge(M, A, 100)], M)
    assert groups["peer_links"] == []
    assert groups["elsewhere"] == []


def test_an_unanswered_inbound_is_flagged_after_five_minutes():
    now = 10_000.0
    groups = comms.groups([edge(A, M, now - 400)], M, now=now)
    assert groups["reports"][0]["unanswered"] is True


def test_a_recent_inbound_is_not_yet_flagged():
    now = 10_000.0
    groups = comms.groups([edge(A, M, now - 60)], M, now=now)
    assert groups["reports"][0]["unanswered"] is False


def test_a_reply_clears_the_unanswered_flag():
    now = 10_000.0
    groups = comms.groups([edge(A, M, now - 400), edge(M, A, now - 300)], M, now=now)
    assert groups["reports"][0]["unanswered"] is False


def test_reports_are_ordered_by_most_recent_traffic():
    groups = comms.groups([edge(M, A, 100), edge(M, B, 900)], M)
    assert [r["session_id"] for r in groups["reports"]] == [B, A]


def test_no_manager_means_everything_is_elsewhere():
    groups = comms.groups([edge(A, B, 100)], None)
    assert groups["reports"] == []
    assert len(groups["elsewhere"]) == 1


def test_an_unresolved_endpoint_never_becomes_a_report():
    orphan = edge(M, A, 100)
    orphan["to"] = {"session_id": None, "name": "died-before-we-looked", "pid": None}
    groups = comms.groups([orphan], M)
    assert groups["reports"] == []
