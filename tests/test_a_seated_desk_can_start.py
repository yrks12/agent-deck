"""A desk the deck cannot pre-trust can never start. Do not seat one.

MEASURED on his live box, 2026-09-07, `roster.load_roster` on the real roster:

    new-hire-82d9ab | /home/deckop/.claude/agent-bus/workspaces/new-hire-82d9ab
    atlas           | /home/deckop/.claude/agent-bus/workspaces/new-hire-77ec17
    listing-closer  | /tmp
    acme-lead       | /tmp
    acme-product    | /tmp
    acme-growth     | /tmp

**Four of his six desks are seated in `/tmp`.** Starting one answers 200:

    listing-closer -> {"ok":true,"channel":"background",
      "detail":"started headless (no_osascript)",
      "pretrust":{"ok":false,"reason":"not_deck_workspace",
                  "detail":"/tmp is not a workspace the deck allocated"}}

`ok: true` and a desk that cannot run. `server/pretrust.py` will only
pre-accept a *direct child* of the deck's own workspaces root, so a session in
`/tmp` sits on Claude Code's trust dialog forever. The board then reads
OFFLINE, or WORKING with nothing happening, and the owner's only clue is a
`blocked` line he has to go looking for. His four `/tmp` desks are also where
the pile of pending `Bash` approvals in `/tmp` came from.

**Refusing the hire was tried first and is WRONG, which is worth writing down
so nobody tries it again.** Adding `_refuse_unstartable_cwd` to `create_agent`
turned 41 existing tests red, and those tests are right: a desk that works on a
real repository has to be seated in that repository. `pretrust` declining is a
*supported state*, not an error -- it is reported in the start response and has
its own `blocked` reason -- because auto-trusting a directory the owner did not
allocate is a security decision the deck must not take on his behalf.

**So the fault is that the seat is silent.** `create_agent` returns the row and
says nothing about a desk that will stall the moment it starts. The owner finds
out later, from a board that reads OFFLINE with no reason, and the four `/tmp`
desks are also where his pile of pending `Bash` approvals in `/tmp` came from.

What this file asserts is the good signal: **hiring into a directory the deck
cannot pre-trust says so, at hire, in the response.** The sweep is the CLASS --
every route that can put a `cwd` on the roster, not the one that bit.

**UPDATE -- `/tmp` is no longer reachable from here, and that is the point.**
Warning at hire turned out to be half the repair: a warning published beside a
desk that is already dead is a task for the owner, not a fix, and four cards
reading "Waiting for you" is exactly what he was looking at. `hire.hire` now
resolves the seat (`server/seat.py`): a scratch path -- `/tmp`, a `/var/folders`
directory, a bare home -- is *substituted* for a workspace the deck allocates,
so no desk is ever seated somewhere it cannot start. Substitution, not refusal,
because refusal is what turned those 41 tests red.

So the fixtures below are **real git repositories** now. That is not a
convenience: a repository is the one seat that is still honoured and still not
pre-trustable, so it is the only remaining case this warning is for -- and it
is the case that must keep working, because a desk hired onto
`~/Projects/acme-lead` has to sit in `~/Projects/acme-lead`. A `/tmp` fixture
here would now be asserting the warning fires for a seat that no longer exists.
The scratch half of the class is asserted in
`tests/test_a_desk_is_seated_where_work_can_happen.py`.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from server import api as api_mod
from server import pretrust


def _repo(at: Path) -> Path:
    """A real git repository, made the way one is really made.

    Stands in for `~/Projects/acme-lead`: honoured as a seat, and never
    pre-trusted, because accepting a security dialog for a folder the owner
    named is a decision the deck must not take for him.
    """
    at.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=at, check=True)
    return at


def test_the_rule_that_decides_this_is_the_one_start_uses():
    """Guard against the check drifting from the thing it is protecting.

    A hire-time check written against its own idea of "a good directory" would
    pass a desk that `pretrust` still refuses, which is the current bug wearing
    a test.
    """
    assert hasattr(pretrust, "is_deck_workspace"), (
        "pretrust.is_deck_workspace is gone; whatever replaced it is what the "
        "hire-time refusal must be written against")


def test_hiring_into_an_untrustable_directory_says_so_at_hire(tmp_path,
                                                              monkeypatch):
    """THE test. The row comes back carrying the warning.

    OWNER RULING 2026-09-30 (agents never dead-end): a real project folder is
    now vouched for at start, so the one seat left that cannot be pre-trusted
    is one whose trust would cascade -- the home directory or above. That is
    what this hires into (the project stands in for home)."""
    project = _repo(tmp_path / "acme-lead")
    monkeypatch.setattr(pretrust, "HOME", project)
    surface = api_mod.Surface(roster_path=tmp_path / "roster.json")

    row = surface.create_agent({"name": "doomed", "cwd": str(project),
                                "engine": "claude"})
    warning = surface.seat_warning(row["cwd"])

    assert row["cwd"] == str(project), (
        "a desk hired onto a real project was moved out of it; the whole "
        "point of warning rather than refusing is that this seat is honoured")
    assert warning.get("ok") is False, (
        "hiring a desk into a folder the deck cannot pre-trust answered "
        "without a word about it, so the owner learns it cannot start only "
        "when the board goes OFFLINE with no reason")
    assert warning["reason"] == "too_broad"
    assert str(project) in str(warning["detail"])


def test_a_desk_in_a_real_workspace_is_hired_with_no_warning(tmp_path):
    """The paired positive, first-class: the warning must not be 'warn always',
    which would pass the test above and cry wolf on every hire."""
    roster_path = tmp_path / "roster.json"
    workspace = pretrust.workspaces_root(roster_path) / "good-desk"
    workspace.mkdir(parents=True)

    surface = api_mod.Surface(roster_path=roster_path)
    row = surface.create_agent(
        {"name": "good-desk", "cwd": str(workspace), "engine": "claude"})

    assert row["name"] == "good-desk"
    assert surface.seat_warning(row["cwd"]).get("ok") is True, (
        "a desk in a directory the deck itself allocated is being warned "
        "about, so the warning means nothing")
    assert "good-desk" in [d.name for d in _roster(tmp_path)]
    assert pretrust.is_deck_workspace(workspace, roster_path), (
        "the fixture built a directory pretrust would itself refuse, so the "
        "assertion above proved nothing")


@pytest.mark.parametrize("shape", ["the repository root",
                                   "a directory inside the repository",
                                   "a worktree of the repository"])
def test_the_sweep_is_every_shape_of_untrusted_directory(tmp_path, shape):
    """Not just the repo root -- every shape of "a project the owner named"
    is honoured as a seat and warned about, because none of them can be
    pre-trusted. Each one is made for real, because `hire` refuses a path that
    does not exist and a fixture that leans on that would be testing the wrong
    refusal.

    The worktree case is not decoration: its `.git` is a *file* pointing at the
    real directory, not a directory, and every one of his agents works in one.
    A rule that tested `is_dir()` would call a worktree scratch and move a desk
    off the branch it was hired to work on.
    """
    project = _repo(tmp_path / "elsewhere" / "acme-lead")
    if shape == "a directory inside the repository":
        made = project / "server" / "sources"
        made.mkdir(parents=True)
    elif shape == "a worktree of the repository":
        subprocess.run(
            [
                "git", "-c", "user.name=Agent Deck Tests",
                "-c", "user.email=tests@agent-deck.invalid",
                "commit", "-q", "--allow-empty", "-m", "root",
            ],
            cwd=project,
            check=True,
        )
        made = tmp_path / "elsewhere" / "acme-lead-wt"
        subprocess.run(["git", "worktree", "add", "-q", str(made), "-b", "wt"],
                       cwd=project, check=True)
        assert (made / ".git").is_file(), "the fixture did not build a worktree"
    else:
        made = project

    surface = api_mod.Surface(roster_path=tmp_path / "roster.json")
    row = surface.create_agent({"name": "doomed", "cwd": str(made),
                                "engine": "claude"})

    assert row["cwd"] == str(made), f"the desk was moved off {shape}"
    # OWNER RULING 2026-09-30, agents never dead-end: every shape of a project
    # he named is honoured AND vouched for at start, so none is warned about.
    warning = surface.seat_warning(row["cwd"])
    assert warning.get("ok") is True, (shape, warning)


def test_every_route_that_writes_a_cwd_goes_through_the_same_refusal():
    """The class, not the instance.

    `create_agent` is one door. If a second one grows -- the interview hire, an
    import, a restore -- it must not be able to seat an unstartable desk
    because this test only knew about the first.
    """
    source = inspect.getsource(api_mod.Surface)
    writers = [name for name, _ in inspect.getmembers(
        api_mod.Surface, predicate=inspect.isfunction)
        if "cwd=" in inspect.getsource(getattr(api_mod.Surface, name))
        and "hire.hire" in inspect.getsource(getattr(api_mod.Surface, name))]

    assert writers, (
        "no method on Surface seats a desk any more; this check would pass "
        "over anything")
    for name in writers:
        body = inspect.getsource(getattr(api_mod.Surface, name))
        assert "pretrust" in body, (
            f"Surface.{name} seats a desk without ever asking whether the deck "
            "could pre-trust its directory, so it can put a row on the board "
            "that will sit on the trust dialog for ever and say nothing")


def _roster(tmp_path: Path):
    from server import roster
    path = tmp_path / "roster.json"
    return roster.load_roster(path) if path.exists() else []
