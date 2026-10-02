"""Hiring: a boss creates a desk, and the desk is told who its boss is.

`brief()` is the payload of this whole feature. One sentence in it -- "your boss
is <name>, not the owner" -- is what stops every worker escalating to Sam. The
detector for that sentence is
`test_the_brief_would_break_if_the_reports_to_line_were_deleted`.

Everything here is hermetic: `tmp_path` for the roster and for the bus log, no
spawning, no network, no `~/.claude`.
"""

import json

import pytest

from server.hire import MAX_DEPTH, MAX_LIVE, HireError, brief, can_hire, fire, hire
from server.roster import Desk, load_roster


def desk(name, reports_to=None, label="", charter=""):
    return Desk(
        name=name,
        cwd="/p",
        engine="claude",
        mission="",
        label=label,
        charter=charter,
        reports_to=reports_to,
    )


COS = desk("cos", None, label="admin")
ACME = desk("acme", "cos", label="product")
ACME_GROWTH = desk("acme-growth", "acme", label="growth")
ORG = [COS, ACME, ACME_GROWTH]

CHARTER = (
    "You own the Acme & Line reading product itself: four-layer method, SKUs, "
    "PDF delivery, photo deletion, pricing inside the flow."
)


# ── brief(): the words a new hire is actually sent ─────────────────────────


def test_the_brief_names_the_boss():
    text = brief(desk("acme-growth", "acme", charter=CHARTER))
    assert "acme" in text


def test_the_brief_would_break_if_the_reports_to_line_were_deleted():
    """THE detector for the feature. A brief that does not, in one sentence,
    name the boss AND say the work is reported to them is a brief that sends
    the new hire straight to the owner."""
    text = brief(desk("harbor-sales", "harbor", charter="You own harbor sales."))
    reporting = [
        ln
        for ln in text.splitlines()
        if "harbor" in ln.lower() and "report" in ln.lower()
    ]
    assert reporting, f"no line names the boss and says it is reported to:\n{text}"


def test_the_brief_says_the_boss_is_the_boss_and_not_the_owner():
    text = brief(desk("acme-growth", "acme", charter=CHARTER)).lower()
    assert "owner" in text
    line = [ln for ln in text.splitlines() if "owner" in ln and "acme" in ln]
    assert line, "nothing contrasts the boss with the owner"


def test_the_brief_states_what_the_desk_owns():
    text = brief(desk("acme-growth", "acme", charter=CHARTER))
    assert CHARTER in text


def test_the_brief_names_the_desk_and_its_function():
    text = brief(desk("acme-growth", "acme", label="growth", charter=CHARTER))
    assert "acme-growth" in text
    assert "growth" in text


def test_the_brief_withholds_money_until_the_boss_says_so():
    text = brief(desk("acme-growth", "acme", charter=CHARTER)).lower()
    spend = [ln for ln in text.splitlines() if "spend" in ln or "money" in ln]
    assert spend, "the brief never mentions spending"
    assert any("acme" in ln for ln in spend), "spending is not gated on the boss"


def test_the_brief_withholds_anything_sent_outward_until_the_boss_says_so():
    text = brief(desk("acme-growth", "acme", charter=CHARTER)).lower()
    assert "send" in text or "outward" in text


def test_a_root_desks_brief_names_the_owner_as_its_boss():
    text = brief(desk("cos", None, label="admin", charter="You run the company."))
    assert "owner" in text.lower()
    line = [ln for ln in text.lower().splitlines() if "boss" in ln]
    assert line and "owner" in line[0]


def test_brief_is_pure_and_repeatable():
    d = desk("acme-growth", "acme", charter=CHARTER)
    assert brief(d) == brief(d)


# ── can_hire(): the caps ───────────────────────────────────────────────────


def test_can_hire_returns_quietly_when_the_org_has_room():
    assert can_hire(ORG, boss="acme", live_count=0) is None


def test_the_owner_may_hire_a_root_with_no_boss():
    assert can_hire([], boss=None, live_count=0) is None


def test_hiring_under_a_specialist_would_be_a_fourth_level_and_is_refused():
    with pytest.raises(HireError) as exc:
        can_hire(ORG, boss="acme-growth", live_count=0)
    assert exc.value.reason == "too_deep"


def test_the_depth_cap_is_two_so_a_specialist_is_the_deepest_desk():
    assert MAX_DEPTH == 2
    from server.roster import depth

    assert depth(ORG, "acme-growth") == MAX_DEPTH


def test_a_full_deck_refuses_another_hire():
    with pytest.raises(HireError) as exc:
        can_hire(ORG, boss="acme", live_count=MAX_LIVE)
    assert exc.value.reason == "too_many_live"


def test_the_last_free_slot_is_still_hireable():
    assert can_hire(ORG, boss="acme", live_count=MAX_LIVE - 1) is None


def test_a_boss_who_does_not_exist_is_refused():
    with pytest.raises(HireError) as exc:
        can_hire(ORG, boss="ghost", live_count=0)
    assert exc.value.reason == "no_such_boss"


def test_hire_error_carries_a_reason_slug_like_inject_error():
    from server.manager import InjectError

    err = HireError("too_deep", "three levels is the cap")
    assert err.reason == "too_deep"
    assert err.detail == "three levels is the cap"
    assert isinstance(InjectError("x").reason, str)


# ── hire(): the desk lands on the roster ───────────────────────────────────


def test_hire_writes_the_desk_with_its_label_charter_and_boss(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    made = hire(
        path,
        name="acme-content",
        label="content",
        charter=CHARTER,
        cwd=str(tmp_path),
        engine="claude",
        reports_to="acme",
    )
    assert made.name == "acme-content"
    assert made.reports_to == "acme"
    assert made.label == "content"
    assert made.charter == CHARTER
    on_disk = {d.name: d for d in load_roster(path)}
    assert on_disk["acme-content"].reports_to == "acme"
    assert on_disk["acme-content"].charter == CHARTER


def test_hire_stamps_the_created_at(tmp_path):
    path = tmp_path / "roster.json"
    made = hire(
        path,
        name="cos",
        label="admin",
        charter="You run the company.",
        cwd=str(tmp_path),
        engine="claude",
        reports_to=None,
    )
    assert made.created_at > 0


def test_hiring_a_name_that_already_exists_is_refused(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    with pytest.raises(HireError) as exc:
        hire(
            path,
            name="acme",
            label="product",
            charter="c",
            cwd=str(tmp_path),
            engine="claude",
            reports_to="cos",
        )
    assert exc.value.reason == "name_taken"


def test_hiring_into_a_directory_that_does_not_exist_is_refused(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    with pytest.raises(HireError) as exc:
        hire(
            path,
            name="acme-content",
            label="content",
            charter="c",
            cwd=str(tmp_path / "nowhere"),
            engine="claude",
            reports_to="acme",
        )
    assert exc.value.reason == "no_such_cwd"


def test_hiring_under_a_boss_who_does_not_exist_is_refused(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    with pytest.raises(HireError) as exc:
        hire(
            path,
            name="acme-content",
            label="content",
            charter="c",
            cwd=str(tmp_path),
            engine="claude",
            reports_to="ghost",
        )
    assert exc.value.reason == "no_such_boss"


def test_hiring_a_fourth_level_is_refused(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    with pytest.raises(HireError) as exc:
        hire(
            path,
            name="deeper",
            label="x",
            charter="c",
            cwd=str(tmp_path),
            engine="claude",
            reports_to="acme-growth",
        )
    assert exc.value.reason == "too_deep"


def test_hiring_past_the_live_cap_is_refused(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    with pytest.raises(HireError) as exc:
        hire(
            path,
            name="acme-content",
            label="content",
            charter="c",
            cwd=str(tmp_path),
            engine="claude",
            reports_to="acme",
            live_count=MAX_LIVE,
        )
    assert exc.value.reason == "too_many_live"


def test_a_refused_hire_leaves_the_roster_untouched(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    with pytest.raises(HireError):
        hire(
            path,
            name="acme-content",
            label="content",
            charter="c",
            cwd=str(tmp_path / "nowhere"),
            engine="claude",
            reports_to="acme",
        )
    assert [d.name for d in load_roster(path)] == [d.name for d in ORG]


# ── hire(): the ledger line ────────────────────────────────────────────────


def test_a_hire_appends_one_line_to_the_bus_in_the_hooks_shape(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    hire(
        path,
        name="acme-content",
        label="content",
        charter=CHARTER,
        cwd=str(tmp_path),
        engine="claude",
        reports_to="acme",
    )
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "hire"
    assert row["name"] == "acme-content"
    assert row["reports_to"] == "acme"
    assert row["by"] == "acme"
    assert isinstance(row["ts"], float)
    assert row["ts"] > 1_700_000_000


def test_a_root_hire_records_the_owner_as_the_hirer(tmp_path):
    path = tmp_path / "roster.json"
    hire(
        path,
        name="cos",
        label="admin",
        charter="You run the company.",
        cwd=str(tmp_path),
        engine="claude",
        reports_to=None,
    )
    row = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
    assert row["reports_to"] is None
    assert row["by"] == "owner"


def test_a_refused_hire_writes_no_ledger_line(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    with pytest.raises(HireError):
        hire(
            path,
            name="acme",
            label="product",
            charter="c",
            cwd=str(tmp_path),
            engine="claude",
            reports_to="cos",
        )
    assert not (tmp_path / "events.jsonl").exists()


def test_two_hires_append_two_lines_and_do_not_rewrite_the_first(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    for name in ("acme-content", "acme-ops"):
        hire(
            path,
            name=name,
            label="x",
            charter="c",
            cwd=str(tmp_path),
            engine="claude",
            reports_to="acme",
        )
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert [json.loads(ln)["name"] for ln in lines] == ["acme-content", "acme-ops"]


# ── hire() does not start a session ────────────────────────────────────────


def test_hire_never_opens_a_terminal(tmp_path, monkeypatch):
    """Hiring creates the desk; seating someone at it is a separate call. This
    is what keeps the default suite from opening windows on this Mac."""
    import server.spawn as spawn

    def boom(*args, **kwargs):
        raise AssertionError("hire() must not spawn anything")

    monkeypatch.setattr(spawn, "spawn_terminal", boom)
    monkeypatch.setattr(spawn, "spawn_background", boom)
    made = hire(
        path=tmp_path / "roster.json",
        name="cos",
        label="admin",
        charter="c",
        cwd=str(tmp_path),
        engine="claude",
        reports_to=None,
    )
    assert made.name == "cos"


# ── fire() ─────────────────────────────────────────────────────────────────


def test_fire_removes_the_desk(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    fire(path, "acme-growth")
    assert [d.name for d in load_roster(path)] == ["cos", "acme"]


def test_firing_a_boss_who_still_has_reports_is_refused(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    with pytest.raises(HireError) as exc:
        fire(path, "acme")
    assert exc.value.reason == "has_reports"
    assert [d.name for d in load_roster(path)] == [d.name for d in ORG]


def test_a_boss_can_be_fired_once_its_reports_are_gone(tmp_path):
    path = tmp_path / "roster.json"
    from server.roster import save_roster

    save_roster(path, ORG)
    fire(path, "acme-growth")
    fire(path, "acme")
    assert [d.name for d in load_roster(path)] == ["cos"]
