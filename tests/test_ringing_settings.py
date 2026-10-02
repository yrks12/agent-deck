"""Who may call him, and when: the settings and the gate (server/ringing.py).

Owner, 2026-10-01: "settings to let Atlas call us by himself". The public
default is OFF; turned on, the default is Atlas only, urgent only. Quiet hours
are his local evening (22:00-08:00 America/New_York): only urgent rings then.
At most 3 a day, one ring per reason.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from server import office, ringing

NY = ZoneInfo("America/New_York")


def at(hour, minute=0, day=1):
    return datetime(2026, 10, day, hour, minute, tzinfo=NY).timestamp()


@pytest.fixture(autouse=True)
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    return tmp_path


def gate(conf, *, desk="atlas", urgent=False, when=None, rang=0, key="db down",
         keys=()):
    return ringing.decide(conf, desk=desk, chief="atlas", urgent=urgent,
                          at=when or at(14), rang_today=rang, reason_key=key,
                          keys_today=set(keys))


def on(**more):
    return {**ringing.settings(), "when": "urgent", **more}


def test_the_public_default_is_off_and_atlas_only():
    conf = ringing.settings()
    assert conf["when"] == "off" and conf["who"] == "chief"
    assert conf["quiet_hours"] == "22:00-08:00"
    assert conf["tz"] == "America/New_York"
    assert conf["max_per_day"] == 3 and conf["call_me_now"] is False
    assert gate(conf, urgent=True)[0] == "calls_off"


def test_on_atlas_only_urgent_rings_for_an_urgent_call_from_atlas():
    assert gate(on(), urgent=True) is None


def test_urgent_only_refuses_a_normal_call():
    assert gate(on(), urgent=False)[0] == "urgent_only"
    assert gate(on(when="anytime"), urgent=False) is None


def test_only_atlas_unless_any_desk_is_allowed():
    assert gate(on(), desk="qa", urgent=True)[0] == "not_allowed"
    assert gate(on(who="any"), desk="qa", urgent=True) is None


def test_quiet_hours_are_his_new_york_night_and_ring_only_urgent():
    conf = on(when="anytime")
    assert gate(conf, when=at(23))[0] == "quiet_hours"
    assert gate(conf, when=at(7, 59))[0] == "quiet_hours"
    assert gate(conf, when=at(8)) is None
    assert gate(conf, when=at(23), urgent=True) is None


def test_at_most_three_a_day_and_one_ring_per_reason():
    assert gate(on(), urgent=True, rang=3)[0] == "daily_cap"
    assert gate(on(), urgent=True, keys=["atlas:db down"])[0] == "already_rang"


def test_call_me_now_rings_a_normal_call_even_at_night():
    conf = on(when="off", call_me_now=True)
    assert gate(conf, urgent=False, when=at(23)) is None
    assert gate(conf, rang=3)[0] == "daily_cap"


def test_settings_save_and_call_me_now_expires_after_an_hour():
    conf, turned_on = ringing.update({"when": "urgent", "call_me_now": True},
                                     at=1000.0)
    assert conf["when"] == "urgent" and conf["call_me_now"] and turned_on
    assert ringing.settings(at=1000.0 + 3599)["call_me_now"] is True
    assert ringing.settings(at=1000.0 + 3601)["call_me_now"] is False
    _, again = ringing.update({"call_me_now": True}, at=1001.0)
    assert again is False, "already on: Atlas is not told twice"


@pytest.mark.parametrize("bad", [{"who": "everyone"}, {"when": "sometimes"},
                                 {"quiet_hours": "late"}, {"tz": "Mars/Base"},
                                 {"max_per_day": 99}, {"max_per_day": True},
                                 {"call_me_now": "yes"}, {"volume": 3}, {}])
def test_a_bad_setting_is_refused_with_a_reason(bad):
    with pytest.raises(ringing.RingError):
        ringing.update(bad)
    assert ringing.settings()["when"] == "off"
