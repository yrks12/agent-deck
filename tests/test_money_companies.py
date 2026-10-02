"""Which company each desk works for, and which desks are experiments."""

from __future__ import annotations

import json

from server.money import companies
from server.roster import Desk


def _d(name, **kw):
    return Desk(name=name, cwd=f"/w/{name}", engine="claude", mission="", **kw)


DESKS = [_d("atlas"), _d("globex-lead", reports_to="atlas"),
         _d("globex-growth", reports_to="atlas"), _d("yye-ops", reports_to="atlas"),
         _d("yes-netops", reports_to="atlas"), _d("listing-closer", reports_to="atlas"),
         _d("channel-watch", reports_to="atlas"), _d("initech-lead", reports_to="atlas"),
         _d("wake-probe", test=True), _d("new-hire-82d9ab", label="New"),
         _d("acme-ui", reports_to="acme-eng")]


def test_companies_come_from_desk_names_sections_and_config():
    cfg = companies.Config(desk_company={"listing-closer": "Listing"},
                           overhead=["Shared"], names={})
    got = companies.assign(DESKS, cfg, sections={"channel-watch": "Shared",
                                                 "globex-lead": "Work"})
    assert got["atlas"] == "HQ"                     # the chief is overhead
    assert got["globex-lead"] == got["globex-growth"] == "Globex"
    assert got["yye-ops"] == "YYE" and got["yes-netops"] == "YES"
    assert got["listing-closer"] == "Listing"       # config wins
    assert got["channel-watch"] == "Shared"         # an app section wins
    assert got["initech-lead"] == "Initech" and got["acme-ui"] == "Acme"
    # test, probe and unset desks are overhead, never a company of their own
    assert got["wake-probe"] == got["new-hire-82d9ab"] == "Shared"


def test_experiments_skip_the_chief_overhead_tests_probes_and_unborn_desks():
    cfg = companies.Config(overhead=["Shared"])
    owner = companies.assign(DESKS, cfg, sections={"channel-watch": "Shared"})
    names = {d.name for d in companies.experiments(DESKS, owner, cfg)}
    assert names == {"globex-lead", "globex-growth", "yye-ops", "yes-netops",
                     "listing-closer", "initech-lead", "acme-ui"}


def test_keywords_match_stripe_products_to_companies():
    cfg = companies.Config(keywords={"Shorts": ["faceless"]})
    kw = companies.keywords(["Globex", "Shorts", "HQ"], cfg)
    assert kw["Shorts"] == ["shorts", "faceless"] and kw["Globex"] == ["globex"]
    assert "HQ" not in kw


def test_config_file_loads_and_a_broken_one_is_the_default(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"desk_company": {"x": "Y"}, "fx_to_display": {"USD": 0.8},
                             "gcp": {"projects": {"p1": "Globex"}}}))
    cfg = companies.load_config(p)
    assert cfg.desk_company == {"x": "Y"} and cfg.fx_to_display["USD"] == 0.8
    assert cfg.gcp_projects == {"p1": "Globex"} and cfg.display_currency == "GBP"
    p.write_text("{nope")
    assert companies.load_config(p) == companies.Config()
