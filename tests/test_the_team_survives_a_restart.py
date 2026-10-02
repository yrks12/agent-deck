"""Restarting the deck must not kill the agents it started.

MEASURED on the Linux box, with a control, on 2026-09-02:

    hired: new-hire-d222b0
    --- BEFORE restart: claude processes ---   5
    systemctl restart agentdeck
    --- AFTER restart: claude processes ---    0

Five working agents, gone. The unit carried `KillMode=mixed`, which sends
SIGTERM to the main process and then **SIGKILL to everything else left in the
cgroup** -- and a headless hire started by the daemon is in that cgroup.

Why this is the defect that matters most on that machine. The box exists so
work continues when the owner closes his laptop. Under `KillMode=mixed`, every
deploy, every config change and every `Restart=always` recovery silently wipes
the whole team, and the board afterwards shows OFFLINE with no reason -- which
is indistinguishable from a desk nobody ever started. The owner would find out
by asking an agent something and getting silence.

`KillMode=process` is the fix: systemd signals the main process only, and the
agents it started are reparented and carry on. Verified live by re-running the
same control after the change.

THE DETECTOR AND ITS LIMIT, stated plainly. This test reads the unit file. It
cannot boot systemd, so it cannot prove the kernel behaviour -- that is what
the live control above is for, and it is recorded here because a measurement
nobody wrote down is a measurement that gets re-litigated. What this test
*does* guarantee is that the setting cannot be quietly reverted or lost in an
edit, which is exactly how it came to be `mixed`.

The sweep is the CLASS: every unit this repo ships, not the one that bit.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"

#: Kill policies that take the daemon's children down with it.
#: `control-group` signals every process in the cgroup; `mixed` SIGKILLs them
#: after the main process is asked to stop. Both are correct for an ordinary
#: service and wrong for one whose children are the product.
FATAL_TO_CHILDREN = {"control-group", "mixed"}


def _units() -> list[Path]:
    # A `Type=oneshot` unit (deckdoctor) runs, exits and owns no desks: neither
    # KillMode=process nor Restart=always applies, and Restart=always on a
    # one-shot is rejected by systemd.
    # The units ship as templates (deploy/templates/*.service.in) rendered
    # from deck.toml; every setting checked here is literal in the template.
    return sorted(u for u in list(DEPLOY.glob("*.service"))
                  + list((DEPLOY / "templates").glob("*.service.in"))
                  if "Type=oneshot" not in u.read_text())


def test_there_is_a_unit_to_check():
    """Without this, every assertion below is vacuously true."""
    assert _units(), f"no systemd unit found under {DEPLOY}"


@pytest.mark.parametrize("unit", _units(), ids=lambda p: p.name)
def test_a_restart_does_not_kill_the_agents_the_deck_started(unit):
    """THE test. Asserts the presence of the setting that keeps the team alive.

    Not "KillMode is not mixed": an absent KillMode line would satisfy that and
    still kill everything, because systemd's DEFAULT is `control-group`.
    """
    body = unit.read_text()
    modes = [line.split("=", 1)[1].strip()
             for line in body.splitlines()
             if line.strip().startswith("KillMode=")]

    assert modes, (
        f"{unit.name} sets no KillMode, so systemd's default (control-group) "
        "applies and every agent the deck started dies with it")
    assert modes[-1] not in FATAL_TO_CHILDREN, (
        f"{unit.name} has KillMode={modes[-1]}, which kills the daemon's "
        "children. MEASURED on the box: 5 live agents before a restart, 0 "
        "after")
    assert modes[-1] == "process", (
        f"{unit.name} has KillMode={modes[-1]}; the agents survive only under "
        "'process', where systemd signals the main process alone")


@pytest.mark.parametrize("unit", _units(), ids=lambda p: p.name)
def test_the_unit_still_restarts_itself(unit):
    """The paired positive. Keeping children alive must not be bought by
    letting the daemon stay down -- an always-on box that does not come back
    after a crash is worse than one that loses its agents."""
    body = unit.read_text()
    assert "Restart=always" in body, (
        f"{unit.name} no longer restarts itself; the box stops being always-on")


@pytest.mark.parametrize("unit", _units(), ids=lambda p: p.name)
def test_the_reason_is_written_down_beside_the_setting(unit):
    """A bare `KillMode=process` reads like a stray tweak and gets 'tidied'
    back. It was `mixed` for exactly that reason. The measurement travels with
    the line."""
    body = unit.read_text()
    if "KillMode=process" not in body:
        pytest.skip("covered by the assertion above")
    where = body.index("KillMode=process")
    context = body[max(0, where - 900):where]
    assert "agent" in context.lower(), (
        f"{unit.name} sets KillMode=process with no comment saying it is what "
        "keeps the deck's agents alive across a restart")
