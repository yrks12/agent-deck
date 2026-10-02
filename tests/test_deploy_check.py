"""`preflight()` is the thing that stops a silent bad deploy.

The box at 10.99.0.1 is not this Mac. It already runs other_app on :7788, it has
its own python, its own node, its own `claude`, and its own disk. Every one of
those is a way for the deck to come up, log nothing alarming, and be useless --
the worst possible failure, because a dead daemon is obvious and a live one that
answers 503 is not.

So `preflight(facts) -> list[str]` is a pure judge over measurements somebody
else took. It never touches the box; the installer and the human take the facts.
Two properties matter equally and are tested equally:

  * a fault it should catch appears, named, in the returned list;
  * a healthy, complete fact set returns `[]`. A preflight that always complains
    teaches Sam to ignore it, which is the same as not having one.

**Unknown is not OK.** A fact that was never measured is a blocker, not a pass --
"no error" is exactly what "nothing ran" looks like.

The fact contract this pins:

    python_version       "3.12.3" | (3, 12, 3)
    node_present         bool
    claude_present       bool
    claude_authenticated bool
    deck_port            int      -- the port the unit will bind
    ports_in_use         {port: holder}  -- what a listener scan found
    bus_dir              str      -- path of the agent-bus directory
    bus_dir_writable     bool
    token_configured     bool     -- AGENT_DECK_TOKEN present and non-empty
    disk_free_mb         int
"""

import pytest

from server.deploy_check import preflight

#: The exact sentence a deploy without a token must produce. Pinned verbatim,
#: not by keyword, because this is the one blocker whose wording has to carry
#: the *consequence* -- "missing token" alone reads like a nag, and a nag is
#: what somebody overrides at 1am.
NO_TOKEN_BLOCKER = (
    "AGENT_DECK_TOKEN is not configured: with no token every /v1 route "
    "answers 503, so the box would look healthy and answer nothing"
)


def good_facts(**overrides) -> dict:
    """A complete measurement of a box that is genuinely ready.

    Note what is *not* a fault here: 7788 is in use by deckop.service. That is
    the normal, expected state of this box -- other_app lives there. The deck
    binds 7789 and coexists. A preflight that blocked on it could never pass.
    """
    facts = {
        "python_version": "3.12.3",
        "node_present": True,
        "claude_present": True,
        "claude_authenticated": True,
        "deck_port": 7789,
        "reserved_ports": [7788],
        "ports_in_use": {7788: "deckop.service"},
        "bus_dir": "/home/deckop/.claude/agent-bus",
        "bus_dir_writable": True,
        "token_configured": True,
        "disk_free_mb": 12_000,
    }
    facts.update(overrides)
    return facts


# ── the go signal ────────────────────────────────────────────────────────────


def test_a_complete_healthy_box_returns_no_blockers():
    """The positive half. Without this, `return ["something"]` would pass."""
    assert preflight(good_facts()) == []


def test_blockers_are_plain_readable_strings():
    """Sam reads these on a phone. Not codes, not tuples, not exceptions."""
    blockers = preflight({})
    assert blockers, "an empty fact set measured nothing and must not read as go"
    for item in blockers:
        assert isinstance(item, str), repr(item)
        assert item.strip() == item and len(item) > 20, repr(item)


# ── 1. the token ─────────────────────────────────────────────────────────────


def test_a_missing_token_is_a_blocker_with_the_exact_wording():
    blockers = preflight(good_facts(token_configured=False))
    assert NO_TOKEN_BLOCKER in blockers, blockers


def test_a_missing_token_is_the_only_complaint_on_an_otherwise_good_box():
    """It blocks *and* nothing else fires -- so the message is actionable."""
    assert preflight(good_facts(token_configured=False)) == [NO_TOKEN_BLOCKER]


def test_an_unmeasured_token_blocks_just_as_hard_as_an_absent_one():
    """Nobody looked is not the same as it is fine, and must not read as fine."""
    facts = good_facts()
    del facts["token_configured"]
    blockers = preflight(facts)
    assert any("AGENT_DECK_TOKEN" in b for b in blockers), blockers


# ── 2. the port other_app already owns ─────────────────────────────────────────


def test_pointing_the_deck_at_7788_blocks_and_names_the_conflict():
    """other_app serves its own dashboard there. Taking it is a fight, not a deploy."""
    blockers = preflight(good_facts(deck_port=7788))
    hit = [b for b in blockers if "7788" in b]
    assert hit, blockers
    assert any("reserved" in b for b in hit), hit


def test_7788_is_refused_even_when_the_scan_shows_it_momentarily_free():
    """other_app restarting is not permission to steal its port.

    A listener scan taken during a `systemctl restart deckop` sees 7788 free.
    Installing there would then work once and break other_app for good, so the
    reservation is judged from the port number, not only from the scan.
    """
    blockers = preflight(good_facts(deck_port=7788, ports_in_use={}))
    hit = [b for b in blockers if "7788" in b]
    assert hit, blockers
    assert any("reserved" in b for b in hit), hit


def test_the_chosen_port_being_taken_by_anything_blocks_and_names_the_holder():
    blockers = preflight(
        good_facts(deck_port=7789, ports_in_use={7788: "deckop.service",
                                                 7789: "caddy"})
    )
    hit = [b for b in blockers if "7789" in b]
    assert hit, blockers
    assert any("caddy" in b for b in hit), hit


def test_the_deck_owning_its_own_port_on_a_re_run_is_not_a_conflict():
    """Re-running the installer must not read its own daemon as an enemy."""
    facts = good_facts(ports_in_use={7788: "deckop.service",
                                     7789: "agentdeck.service"})
    assert preflight(facts) == []


# ── every other fact it judges ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"python_version": "3.9.18"}, "3.9"),
        ({"python_version": (3, 10, 6)}, "3.10"),
        ({"node_present": False}, "node"),
        ({"claude_present": False}, "claude"),
        ({"claude_authenticated": False}, "authenticated"),
        ({"bus_dir_writable": False}, "/home/deckop/.claude/agent-bus"),
        ({"disk_free_mb": 120}, "120"),
    ],
    ids=["old-python", "old-python-tuple", "no-node", "no-claude",
         "claude-logged-out", "bus-read-only", "disk-nearly-full"],
)
def test_each_fault_produces_exactly_one_named_blocker(override, expected):
    blockers = preflight(good_facts(**override))
    assert len(blockers) == 1, blockers
    assert expected in blockers[0], blockers[0]


@pytest.mark.parametrize(
    "key, expected",
    [
        ("python_version", "python"),
        ("node_present", "node"),
        ("claude_present", "claude"),
        ("claude_authenticated", "claude"),
        ("deck_port", "port"),
        ("ports_in_use", "port"),
        ("bus_dir_writable", "bus"),
        ("token_configured", "AGENT_DECK_TOKEN"),
        ("disk_free_mb", "disk"),
    ],
)
def test_an_unmeasured_fact_blocks_and_says_which_one(key, expected):
    """The whole point: a preflight cannot bless what nobody looked at."""
    facts = good_facts()
    del facts[key]
    blockers = preflight(facts)
    assert blockers, f"dropping {key} produced a go signal"
    assert any(expected in b for b in blockers), (key, blockers)


def test_a_box_where_nothing_is_ready_reports_all_of_it_at_once():
    """One round trip, not seven. Sam fixes the box once and re-runs once."""
    blockers = preflight(
        {
            "python_version": "3.8.10",
            "node_present": False,
            "claude_present": False,
            "claude_authenticated": False,
            "deck_port": 7788,
            "ports_in_use": {7788: "deckop.service"},
            "bus_dir": "/home/deckop/.claude/agent-bus",
            "bus_dir_writable": False,
            "token_configured": False,
            "disk_free_mb": 40,
        }
    )
    assert len(blockers) >= 7, blockers
    assert NO_TOKEN_BLOCKER in blockers


def test_preflight_does_not_mutate_the_facts_it_was_given():
    """It is a judge. The caller may re-measure and re-judge with the same dict."""
    facts = good_facts()
    before = repr(facts)
    preflight(facts)
    assert repr(facts) == before


# ── the reservation comes from deck.toml, not from this file ─────────────────


def test_a_box_that_reserves_nothing_may_use_7788():
    """A stranger's box has no deckop.service. 7788 is just a free port there."""
    assert preflight(good_facts(deck_port=7788, reserved_ports=[],
                                ports_in_use={})) == []


def test_a_reservation_the_config_names_is_refused_even_when_the_scan_is_free():
    blockers = preflight(good_facts(deck_port=9000, reserved_ports=[9000],
                                    ports_in_use={}))
    hit = [b for b in blockers if "9000" in b]
    assert hit and any("reserved" in b for b in hit), blockers


def test_reserved_ports_may_arrive_as_strings_and_garbage_is_ignored():
    blockers = preflight(good_facts(deck_port=9000, reserved_ports=["9000", "x", None]))
    assert any("9000" in b for b in blockers), blockers


def test_no_reserved_ports_fact_means_none_reserved():
    """deckconfig's default is `()`; the judge must not invent an owner's port."""
    facts = good_facts(deck_port=7788, ports_in_use={})
    del facts["reserved_ports"]
    assert preflight(facts) == []


# ── the speak command is configurable, and the Mac keeps today's ─────────────


def test_speak_cmd_defaults_to_the_mlx_pipeline_under_the_callers_home(tmp_path):
    from server import manager
    (tmp_path / "Applications/mlx-tts").mkdir(parents=True)
    (tmp_path / "Applications/mlx-tts/speak.py").write_text("")
    cmd = manager.build_speak_cmd({}, home=tmp_path)
    assert cmd[:2] == [str(tmp_path / "Applications/mlx-tts/venv/bin/python3"),
                       str(tmp_path / "Applications/mlx-tts/speak.py")]
    assert cmd[2] == "{text}" and "--background" in cmd


def test_deck_speak_cmd_overrides_it_and_text_is_appended_when_unmarked(tmp_path):
    from server import manager
    cmd = manager.build_speak_cmd({"DECK_SPEAK_CMD": "say -v 'Daniel Premium'"}, home=tmp_path)
    assert cmd == ["say", "-v", "Daniel Premium", "{text}"]
    cmd = manager.build_speak_cmd({"DECK_SPEAK_CMD": "espeak {text} -s 150"}, home=tmp_path)
    assert cmd == ["espeak", "{text}", "-s", "150"]


def test_a_blank_or_broken_deck_speak_cmd_falls_back_to_the_default(tmp_path):
    from server import manager
    default = manager.build_speak_cmd({}, home=tmp_path)
    assert manager.build_speak_cmd({"DECK_SPEAK_CMD": "  "}, home=tmp_path) == default
    assert manager.build_speak_cmd({"DECK_SPEAK_CMD": "say 'unterminated"}, home=tmp_path) == default
