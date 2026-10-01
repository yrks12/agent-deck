"""The org chart: who reports to whom, and what that lets you compute.

The observed shape we are copying is three levels -- COS at the top (the only
desk the owner talks to), project managers under it, function specialists under
those. Every test below asserts the GOOD signal: the boss is named, the chain is
the real chain, the orphan is visible. The cycle tests assert a raise, because
the alternative to raising is a hang, and a hang is not a test result.
"""

import pytest

from server.roster import Desk, boss_of, chain, depth, route_to, tree


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


# The observed org, verbatim in shape: COS -> managers -> specialists.
COS = desk("cos", None, label="admin")
ACME = desk("acme", "cos", label="product")
VILLAS = desk("villas", "cos", label="product")
PALM_GROWTH = desk("acme-growth", "acme", label="growth")
VILLAS_SALES = desk("villas-sales", "villas", label="sales")
VILLAS_STUDIO = desk("villas-studio", "villas", label="studio")
ORG = [COS, ACME, VILLAS, PALM_GROWTH, VILLAS_SALES, VILLAS_STUDIO]


# ── the three new fields ───────────────────────────────────────────────────


def test_a_desk_carries_its_label_charter_and_boss():
    d = Desk(
        name="acme-growth",
        cwd="/p",
        engine="claude",
        mission="",
        label="growth",
        charter="You own Acme's paid acquisition.",
        reports_to="acme",
    )
    assert d.label == "growth"
    assert d.charter == "You own Acme's paid acquisition."
    assert d.reports_to == "acme"


def test_the_new_fields_default_so_old_callers_still_work():
    d = Desk(name="x", cwd="/p", engine="claude", mission="m")
    assert d.label == ""
    assert d.charter == ""
    assert d.reports_to is None


def test_the_new_fields_survive_a_round_trip_through_the_roster_file(tmp_path):
    from server.roster import load_roster, save_roster

    path = tmp_path / "roster.json"
    save_roster(path, [PALM_GROWTH])
    assert load_roster(path) == [PALM_GROWTH]
    assert load_roster(path)[0].reports_to == "acme"


# ── boss_of ────────────────────────────────────────────────────────────────


def test_boss_of_names_the_desks_boss():
    assert boss_of(ORG, "acme-growth") == "acme"
    assert boss_of(ORG, "villas-studio") == "villas"


def test_the_root_desk_has_no_boss_because_its_boss_is_the_owner():
    assert boss_of(ORG, "cos") is None


def test_exactly_one_desk_reports_to_the_owner():
    assert [d.name for d in ORG if d.reports_to is None] == ["cos"]


# ── chain ──────────────────────────────────────────────────────────────────


def test_chain_walks_a_specialist_all_the_way_up_to_the_root():
    assert chain(ORG, "villas-sales") == ["villas-sales", "villas", "cos"]


def test_a_roots_chain_is_just_itself():
    assert chain(ORG, "cos") == ["cos"]


def test_chain_of_an_unknown_desk_raises():
    with pytest.raises(ValueError):
        chain(ORG, "nobody")


def test_chain_surfaces_the_named_boss_that_does_not_exist():
    """An orphan must be visible as an orphan: the missing boss is the last
    link in its chain, so a caller can see exactly which name is dangling."""
    orphaned = [desk("ghost", "vanished")]
    assert chain(orphaned, "ghost") == ["ghost", "vanished"]


def test_a_two_desk_cycle_raises_instead_of_looping_forever():
    ring = [desk("a", "b"), desk("b", "a")]
    with pytest.raises(ValueError) as exc:
        chain(ring, "a")
    assert "cycle" in str(exc.value).lower()


def test_a_desk_that_reports_to_itself_raises():
    with pytest.raises(ValueError) as exc:
        chain([desk("a", "a")], "a")
    assert "cycle" in str(exc.value).lower()


def test_a_three_desk_cycle_raises():
    ring = [desk("a", "b"), desk("b", "c"), desk("c", "a")]
    with pytest.raises(ValueError):
        chain(ring, "b")


# ── depth ──────────────────────────────────────────────────────────────────


def test_depth_is_zero_for_the_root_one_for_a_manager_two_for_a_specialist():
    assert depth(ORG, "cos") == 0
    assert depth(ORG, "acme") == 1
    assert depth(ORG, "acme-growth") == 2


def test_depth_raises_on_a_cycle():
    with pytest.raises(ValueError):
        depth([desk("a", "b"), desk("b", "a")], "a")


def test_depth_raises_on_an_unknown_desk():
    with pytest.raises(ValueError):
        depth(ORG, "nobody")


# ── tree ───────────────────────────────────────────────────────────────────


def test_tree_nests_every_desk_under_its_boss():
    roots = tree(ORG)
    assert [r["name"] for r in roots] == ["cos"]
    cos = roots[0]
    assert [r["name"] for r in cos["reports"]] == ["acme", "villas"]
    acme = cos["reports"][0]
    assert [r["name"] for r in acme["reports"]] == ["acme-growth"]
    villas = cos["reports"][1]
    assert [r["name"] for r in villas["reports"]] == ["villas-sales", "villas-studio"]


def test_tree_keeps_the_desk_fields_alongside_the_reports():
    cos = tree(ORG)[0]
    assert cos["label"] == "admin"
    assert cos["engine"] == "claude"
    assert cos["reports_to"] is None


def test_a_leaf_has_an_empty_reports_list_not_a_missing_key():
    leaf = tree(ORG)[0]["reports"][0]["reports"][0]
    assert leaf["name"] == "acme-growth"
    assert leaf["reports"] == []


def test_tree_loses_nobody_every_desk_appears_exactly_once():
    seen = []

    def walk(rows):
        for row in rows:
            seen.append(row["name"])
            walk(row["reports"])

    walk(tree(ORG))
    assert sorted(seen) == sorted(d.name for d in ORG)


def test_tree_surfaces_an_orphan_whose_boss_does_not_exist():
    """The dangerous failure is the quiet one: a typo'd boss name makes the
    desk vanish off the board. It must show up, at the top, flagged."""
    rows = tree([COS, desk("ghost", "vanished")])
    ghost = [r for r in rows if r["name"] == "ghost"]
    assert len(ghost) == 1
    assert ghost[0]["orphan"] is True
    assert ghost[0]["reports_to"] == "vanished"


def test_a_real_root_is_not_flagged_as_an_orphan():
    assert tree(ORG)[0]["orphan"] is False


def test_tree_raises_on_a_cycle_rather_than_dropping_the_desks():
    with pytest.raises(ValueError) as exc:
        tree([COS, desk("a", "b"), desk("b", "a")])
    assert "cycle" in str(exc.value).lower()


# ── route_to ───────────────────────────────────────────────────────────────


def test_a_specialist_routes_its_work_to_its_manager_not_to_the_top():
    assert route_to(ORG, "acme-growth") == "acme"


def test_a_manager_routes_its_work_to_the_cos():
    assert route_to(ORG, "villas") == "cos"


def test_the_root_routes_to_the_owner():
    assert route_to(ORG, "cos") is None


def test_a_desk_never_routes_to_itself():
    with pytest.raises(ValueError):
        route_to([desk("a", "a")], "a")
