"""Where a desk is allowed to sit.

**The defect.** MEASURED on the box, 2026-09-07: four of six desks on the real
roster -- `listing-closer`, `acme-lead`, `acme-product`, `acme-growth` -- were
seated in `/tmp`. `pretrust` will not pre-accept Claude Code's trust dialog for
a directory the deck did not allocate, and that restriction is deliberate, so
each of those four opened on "Is this a project you created or one you trust?"
and never ran a turn. The board read *"Waiting for you: workspace trust was not
pre-accepted"* on four cards -- four desks asking the owner to approve a folder
he never chose.

`Surface.seat_warning` already reported this accurately. A warning published
beside a desk that is already dead is not a fix. The seat was the fault: `POST
/v1/agents` took whatever `cwd` it was handed, and the chief -- which had to put
*something* there -- invented `/tmp`.

**The rule.** A seat is honoured only where work can actually happen:

* `git_repo` -- the path is inside a git repository. A real project. His
  `~/Projects/acme-*` repos, a worktree, a subdirectory of either. A desk hired
  onto a real repo must keep sitting in that repo; refusing that was tried and
  turned 41 tests red, correctly.
* `deck_workspace` -- `pretrust.is_deck_workspace` already vouches for it.

Everything else is `scratch`, and gets a workspace of the deck's own instead.

**Substitution, not refusal.** Refusal was tried and was re-aimed at a warning
for good reasons. The caller's intent -- "give this agent somewhere to work" --
is satisfied by moving the seat; it is not satisfied by 409. What substitution
owes the caller is that it says so, which is why `Seat` carries `stated`
alongside `cwd` and every route publishes the pair.

**What is still refused, and why.** A stated path that does not exist stays
`hire.HireError("no_such_cwd")`. That is not a scratch seat, it is a typo: a
caller that meant `~/Projects/acme-lead` and wrote `acme-led` must be told, not
moved somewhere else while believing it got the repo. `hire()` checks that
first and this module never sees it.

**Two paths that own a `.git` and are still not projects.** The filesystem root
and the home directory. A dotfiles repo in `$HOME` would otherwise make every
bare-home seat read as a real project, and a desk seated at `~` is the same
"nowhere in particular" as one seated at `/tmp`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Not a project even when it holds a `.git`. See the module docstring.
NOT_PROJECTS = ("/",)


@dataclass(frozen=True)
class Seat:
    """Where the desk actually goes, and what the caller asked for.

    `kind` is a stable slug in the shape `hire.HireError` and `pretrust.Trust`
    already use, so a client reads it the same way everywhere.
    """

    cwd: str
    stated: str
    kind: str  # "git_repo" | "deck_workspace" | "scratch"
    substituted: bool

    def as_dict(self) -> dict:
        return {"cwd": self.cwd, "stated": self.stated,
                "kind": self.kind, "substituted": self.substituted}


def workspaces_root(roster_path: Path | str) -> Path:
    """The deck's own workspaces, derived from the roster the same way
    `pretrust.workspaces_root` and `hire.events_path` derive their files."""
    return Path(roster_path).parent / "workspaces"


def workspace_for(roster_path: Path | str, name: str) -> str:
    """Allocate this desk's own working directory. Raises OSError.

    0700, and made here rather than at spawn time: a spawn that fails on a
    directory the deck itself was supposed to make is the exact failure this
    removes. `exist_ok`, because reseating a desk into the workspace it already
    has must not lose what is in it.
    """
    path = workspaces_root(roster_path) / name
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return str(path)


def git_root(cwd: str | Path) -> Path | None:
    """The repository `cwd` is inside, or None.

    Walks up rather than shelling out to `git`: this runs on the hiring path,
    which holds the roster write lock, and a subprocess per hire is a cost with
    no answer this does not already have. `.git` is tested with `exists()`, not
    `is_dir()`, because a worktree's `.git` is a *file* pointing at the real
    directory -- and every one of his agents' worktrees is exactly that.
    """
    try:
        here = Path(cwd).resolve()
    except OSError:
        return None
    for folder in [here, *here.parents]:
        if str(folder) in NOT_PROJECTS or folder == Path.home():
            break
        if (folder / ".git").exists():
            return folder
    return None


def classify(cwd: str | Path, roster_path: Path | str) -> str:
    """`"deck_workspace"`, `"git_repo"` or `"scratch"`.

    Deck workspace first. Both of the first two are honoured, so the order only
    decides the label -- but a workspace under `~/.claude/agent-bus/` sitting
    below a home directory that happens to be a repo should read as what the
    deck made it, not as somebody's dotfiles.
    """
    # Deferred: `pretrust` imports `hire.events_path` and `hire` imports this
    # module, so a module-level import here closes a cycle. One deferred import
    # beats a second copy of the rule that decides whether a session starts.
    from .pretrust import is_deck_workspace

    if is_deck_workspace(cwd, roster_path):
        return "deck_workspace"
    if git_root(cwd) is not None:
        return "git_repo"
    return "scratch"


def resolve(roster_path: Path | str, name: str, stated: str) -> Seat:
    """Seat `name`. Honours `stated` where work can happen, substitutes where
    it cannot. Raises OSError only if the workspace cannot be created.

    Called from `hire.hire` rather than from each route on purpose: three
    callers can seat a desk -- the form door, the interview door, and an agent
    asking for a colleague through `YOS_HIRE` -- and a rule enforced at two of
    the three is a rule that ships the defect through the third.
    """
    stated = str(stated or "")
    if stated:
        kind = classify(stated, roster_path)
        if kind != "scratch":
            return Seat(cwd=stated, stated=stated, kind=kind, substituted=False)
    # No folder stated is not a substitution -- nothing was overridden. It is
    # the interview door's normal case and it must not read as a correction.
    return Seat(cwd=workspace_for(roster_path, name), stated=stated,
                kind="deck_workspace", substituted=bool(stated))


def describe(roster_path: Path | str, stated: str, landed: str) -> Seat:
    """The seat a hire ended up with, for the caller to be told about.

    Derived from the desk that was actually written to the roster rather than
    from a second `resolve()`, so a route cannot report one seat while the
    roster holds another -- which is the shape of the defect this whole module
    exists for.
    """
    stated = str(stated or "")
    return Seat(cwd=landed, stated=stated,
                kind=classify(landed, roster_path),
                substituted=bool(stated) and landed != stated)
