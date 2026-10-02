"""Conversational hiring: you describe the role, the agent works out the rest.

The reference product's whole trick is that hiring an agent feels like hiring a
person rather than filling in a config form. You say what you want; it asks
what you actually want to use it for, offers a few shapes the job could take,
and then **names itself** -- picks its own name, its own title for the chip
beside it on the board, and writes its own charter. Changing it later is the
same conversation: you tell it in the chat.

The mechanism is one line on stdout. A session prints

    YOS_DESK {"name": "inbox-hand", "label": "Email", "charter": "..."}

and the deck folds that into the roster -- the same shape as the `YOS_EVENT`
convention in the engine this was modelled on, and `parse_lines` copies its
tolerance bar: this is the untrusted stdout of an LLM, so
every malformed, truncated, enormous or merely quoted line is skipped and
never raised.

**The privilege rule is the load-bearing part.** `apply_patch` takes the
`actor` -- the desk that emitted the line -- and a desk may patch only itself.
A `YOS_DESK` line naming any other desk is refused with `not_yours`. Without
that check a specialist can rewrite its manager's charter, and every level of
the org chart is advisory. Three things are therefore unreachable from this
channel by construction, not by validation:

* `reports_to` is not a field of `DeskPatch`, so no desk can promote itself,
  reassign its boss, or make itself a root.
* `cwd` and `engine` are not fields either, so no desk can move itself into
  another repo or swap the CLI it runs under.
* a hire request goes through `hire.hire()`, so `MAX_DEPTH`, `MAX_LIVE` and
  the duplicate-name check apply to a hire an agent asked for exactly as they
  apply to one the owner typed.

Everything here is either pure or a small read-modify-write of one JSON file.
Nothing spawns; putting a session in the new chair stays `server.spawn`'s job.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from . import atomic
from .hire import HIRING, HOW_YOU_DECIDE, HOW_YOU_SOUND, hire
from .roster import (DESCRIPTION_MAX, Desk, clean_description, load_roster,
                     save_roster)

DESK_PREFIX = "YOS_DESK"          # self-description / self-mutation
HIRE_PREFIX = "YOS_HIRE"          # "I want to hire someone for X"

# A line longer than this is junk, not a record. A charter is a paragraph; the
# 2 MB line this guards against is a session dumping a file to stdout, and
# json.loads on it costs real time for a result we would throw away anyway.
MAX_LINE = 64 * 1024

# What a desk's picture is allowed to be. No SVG: it is a script container.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

# Where a desk's generated picture has to live, found relative to the roster --
# the same trick `hire.events_path` uses, so production lands in
# ~/.claude/agent-bus/avatars/ while a test with a tmp roster gets a tmp one.
AVATAR_DIRNAME = "avatars"

# Where the old->new names of every rename are kept, found relative to the
# roster the same way `hire.events_path` finds the ledger. A desk's name is the
# key its threads, its read cursor and its unread count are stored under, so a
# rename that is not written down here does not move a conversation -- it ends
# one. See `server.api.Surface._apply_renames`, which is the reader.
ALIASES_FILENAME = "desk_aliases.json"

# A rename chain longer than this is a loop or a bug, and following it on every
# message would be an unbounded walk on the app-foreground path.
MAX_ALIAS_HOPS = 32


class OnboardError(Exception):
    """Refusal to apply. `reason` is a stable machine-readable slug.

    Same shape as `hire.HireError`, `spawn.SpawnError` and
    `manager.InjectError` so every caller on the deck reads a refusal the
    same way.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class DeskPatch:
    """What a desk is allowed to say about itself. Note what is absent:
    `reports_to`, `cwd` and `engine` cannot be expressed here at all."""

    name: str | None = None
    label: str | None = None
    charter: str | None = None
    avatar: str | None = None      # a path, validated in `apply_patch`
    description: str | None = None


@dataclass(frozen=True)
class HireRequest:
    """"I want someone for X." Note the absence of `reports_to`: the new desk
    reports to whoever asked, which is not the asker's to choose."""

    name: str
    label: str
    charter: str
    cwd: str
    engine: str = "claude"
    model: str = ""
    # Required on the line (`parse_lines`); defaulted here only so a caller
    # building one by hand need not invent a sentence.
    description: str = ""


# ── reading the untrusted stdout of a session ──────────────────────────────


def _clean(value: object) -> str | None:
    """A usable string, or None. Not a coercion: a non-string is dropped.

    Lone surrogates are dropped too. `json.loads` happily produces one from
    `"\\ud800"`, and it survives until something tries to encode it -- writing
    a filename, a socket frame, an HTTP body -- where it raises far from here.
    """
    if not isinstance(value, str):
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    value = value.strip()
    return value or None


def _payload(line: str, prefix: str) -> dict | None:
    """The JSON object on `line` for `prefix`, or None for anything else."""
    if not line.startswith(prefix):
        return None
    rest = line[len(prefix):]
    # The prefix has to be the whole token: `YOS_DESKTOP {...}` is not ours.
    if not rest[:1].isspace():
        return None
    try:
        parsed = json.loads(rest)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_lines(text: str) -> tuple[list[DeskPatch], list[HireRequest]]:
    """Extract well-formed desk patches and hire requests from session output.

    Never raises. Skips a line whose prefix is not at the start (the prefix
    quoted inside ordinary prose is the common case), a lookalike prefix,
    malformed or truncated JSON, a payload that is not an object, an oversized
    line, and a record with nothing usable in it.

    This is deliberately *not* the privilege guard. It reports what a session
    said; `apply_patch` decides whether the session was entitled to say it.
    """
    patches: list[DeskPatch] = []
    hires: list[HireRequest] = []

    for raw in (text or "").splitlines():
        if len(raw) > MAX_LINE:
            continue
        line = raw.strip()

        payload = _payload(line, DESK_PREFIX)
        if payload is not None:
            patch = DeskPatch(
                name=_clean(payload.get("name")),
                label=_clean(payload.get("label")),
                charter=_clean(payload.get("charter")),
                avatar=_clean(payload.get("avatar")),
                description=_clean(payload.get("description")),
            )
            # A patch that says nothing is not a patch. This is also what
            # drops a line whose only keys were the unpatchable ones.
            if any((patch.name, patch.label, patch.charter, patch.avatar,
                    patch.description)):
                patches.append(patch)
            continue

        payload = _payload(line, HIRE_PREFIX)
        if payload is None:
            continue
        fields = {key: _clean(payload.get(key))
                  for key in ("name", "label", "charter", "cwd",
                              "description")}
        if not all(fields.values()):
            continue
        hires.append(
            HireRequest(
                **fields,
                engine=_clean(payload.get("engine")) or "claude",
                model=_clean(payload.get("model")) or "",
            )
        )

    return patches, hires


# ── applying it, to yourself and only to yourself ──────────────────────────


def _key(name: str) -> str:
    """The form two names collide in: case-folded, whitespace collapsed."""
    return " ".join(name.split()).casefold()


def rename_is_safe(desks: list[Desk], old: str, new: str) -> bool:
    """Can `old` become `new` without silently merging two desks?

    False for an empty name, a name carrying leading or trailing whitespace,
    and any name that collides with another desk's -- including one that
    differs only by case or by whitespace. `upsert` keys on the name, so a
    collision does not error: it overwrites, and one desk quietly disappears.
    Renaming a desk to the name it already has is safe and does nothing.
    """
    if not isinstance(new, str):
        return False
    if not new.strip() or new != new.strip():
        return False
    key = _key(new)
    return not any(_key(d.name) == key for d in desks if d.name != old)


# ── the rename ledger ──────────────────────────────────────────────────────
#
# `apply_patch` renaming a desk is the whole point of the interview door: a new
# hire arrives with a placeholder name and replaces it the moment it works out
# what it is. Everything else on this deck is keyed on that name -- the office
# thread the owner is already typing into, the read cursor, the unread count.
# Writing the old->new pair down is what lets those follow the desk instead of
# being orphaned under a name nothing points at any more.


def aliases_path(roster_path: Path | str) -> Path:
    """Where renames are recorded, derived from the roster."""
    return Path(roster_path).parent / ALIASES_FILENAME


def load_aliases(path: Path | str) -> dict[str, str]:
    """`{old_name: new_name}`, one hop each. Missing or corrupt file -> {}.

    Same rule as `roster.load_roster`: an unreadable file is an empty map and a
    board that still paints, never an exception thrown at a request.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    rows = raw.get("renames") if isinstance(raw, dict) else None
    if not isinstance(rows, dict):
        return {}
    return {old: new for old, new in rows.items()
            if isinstance(old, str) and isinstance(new, str) and old and new}


def resolve(aliases: dict[str, str], name: str) -> str:
    """The name a desk goes by now, following a chain of renames. PURE.

    Bounded and cycle-safe: a desk renamed back to a name it once had would
    otherwise loop forever on a path that runs on every message.
    """
    seen = {name}
    current = name
    for _ in range(MAX_ALIAS_HOPS):
        nxt = aliases.get(current)
        if nxt is None or nxt in seen:
            return current
        seen.add(nxt)
        current = nxt
    return current


def reseat(cards: list[dict], aliases: dict[str, str]) -> None:
    """Rewrite each card's `name` to the name its desk goes by NOW. In place.

    The other half of `server.spawn`'s `--name`. A session is named once, on
    the command line that started it, and `claude` has no way to rename a live
    one -- so the moment a new hire does the one thing the interview asked it
    to do and names itself, the roster says `pr-watch` and the process sitting
    at that desk is still called `new-hire-511bba`. Every name-keyed join then
    misses again: the desk falls back to OFFLINE, a message to it answers
    `delivered: false`, and the harvester stops hearing it.

    So the rename ledger is read on the way in, on the collector's tick,
    before the card reaches anything. One rewrite fixes all of them at once --
    the desk join, the inject target, the office hook's mailbox and the
    harvester's actor all read `card["name"]`.

    A no-op for an empty ledger and for every session that never renamed:
    `resolve` returns the name unchanged when nothing points at it.
    """
    if not aliases:
        return
    for card in cards:
        name = card.get("name")
        if isinstance(name, str) and name:
            card["name"] = resolve(aliases, name)


def _remember_rename(roster_path: Path, old: str, new: str) -> None:
    """Record one rename. Never raises: a desk that renamed itself must not
    fail because the alias file could not be written -- the roster is already
    the truth, and the worst case is a thread that has to be reopened."""
    path = aliases_path(roster_path)
    aliases = load_aliases(path)
    aliases[old] = new
    # A desk that took this name before and left has nothing to forward now.
    aliases.pop(new, None)
    try:
        atomic.write_text(path, json.dumps({"version": 1, "renames": aliases}))
    except OSError:
        pass


def _validated_avatar(raw: str, directory: Path) -> str:
    """The picture path, or OnboardError('bad_avatar').

    An agent generates its own avatar and applies it, so this path arrives
    from the same untrusted channel as everything else. It must be a real
    file, with an image extension, inside the avatar directory -- resolved
    first, so both `../` and a symlink pointing out of the directory are
    caught. The unresolved path is what gets stored, so the roster keeps the
    name the board will serve.
    """
    candidate = Path(raw)
    if candidate.suffix.lower() not in IMAGE_SUFFIXES:
        raise OnboardError("bad_avatar", f"{raw!r} is not an image")
    try:
        resolved = candidate.resolve(strict=True)
        root = directory.resolve(strict=True)
    except (OSError, RuntimeError):
        raise OnboardError("bad_avatar", f"{raw!r} is not a file that exists")
    try:
        resolved.relative_to(root)
    except ValueError:
        raise OnboardError("bad_avatar", f"{raw!r} is outside {root}")
    if not resolved.is_file():
        raise OnboardError("bad_avatar", f"{raw!r} is not a file")
    return str(candidate)


def apply_patch(
    roster_path: Path | str,
    actor: str,
    patch: DeskPatch,
    *,
    avatar_dir: Path | str | None = None,
) -> Desk:
    """Apply `patch` to `actor`'s own desk and return the updated desk.

    `actor` is the desk that emitted the line, and it is the only desk this
    call can touch. Raises OnboardError:

      no_such_desk  `actor` is not on the roster
      not_yours     the patch names a different desk -- the privilege rule
      unsafe_name   the new name collides with another desk's
      has_reports   renaming would dangle the reports pointing at the old name
      bad_avatar    the picture is not a real image inside the avatar directory

    Every check runs before anything is written, so a refused patch leaves the
    roster exactly as it was -- including the fields that were fine.
    """
    path = Path(roster_path)
    desks = load_roster(path)
    current = next((d for d in desks if d.name == actor), None)
    if current is None:
        raise OnboardError("no_such_desk", f"no desk named {actor!r}")

    new_name = current.name
    if patch.name is not None and patch.name != current.name:
        # THE rule. Naming a desk that exists and is not you is an attempt to
        # rewrite someone else -- most damagingly, your own boss.
        if any(d.name == patch.name for d in desks):
            raise OnboardError(
                "not_yours",
                f"{actor} may not patch {patch.name!r}: a desk may patch only itself",
            )
        if not rename_is_safe(desks, current.name, patch.name):
            raise OnboardError("unsafe_name", f"{patch.name!r} collides with a desk")
        reports = [d.name for d in desks if d.reports_to == current.name]
        if reports:
            raise OnboardError(
                "has_reports",
                f"{actor} still has reports: {', '.join(reports)}",
            )
        new_name = patch.name

    avatar = current.avatar
    if patch.avatar is not None:
        directory = Path(avatar_dir) if avatar_dir else path.parent / AVATAR_DIRNAME
        avatar = _validated_avatar(patch.avatar, directory)

    charter = patch.charter if patch.charter is not None else current.charter
    updated = replace(
        current,
        name=new_name,
        label=patch.label if patch.label is not None else current.label,
        charter=charter,
        # The charter IS the mission -- it is what `spawn.build_argv` hands the
        # process as its system prompt. Letting the two drift is how a desk
        # ends up briefed as one thing and running as another.
        mission=charter,
        avatar=avatar,
        description=(clean_description(patch.description)[:DESCRIPTION_MAX]
                     if patch.description is not None
                     else current.description),
    )

    # Replace in place rather than remove-then-append: a rename must not move
    # the desk to the bottom of the board, and must not leave the old name.
    save_roster(path, [updated if d.name == current.name else d for d in desks])
    if new_name != current.name:
        _remember_rename(path, current.name, new_name)
    return updated


def apply_hire(
    roster_path: Path | str,
    actor: str,
    request: HireRequest,
    *,
    live_count: int = 0,
) -> Desk:
    """Hire the desk `actor` asked for, reporting to `actor`.

    A thin call into `hire.hire()` on purpose: an agent asking for a colleague
    must meet the same MAX_DEPTH, MAX_LIVE, duplicate-name and real-cwd checks
    as the owner asking for one, and it raises `hire.HireError` with the cap's
    own reason slug. `reports_to` is `actor` and is not in `HireRequest`, so
    nothing an agent prints can hire a peer for its boss or a second root.
    """
    return hire(
        Path(roster_path),
        name=request.name,
        label=request.label,
        charter=request.charter,
        description=request.description,
        cwd=request.cwd,
        engine=request.engine,
        reports_to=actor,
        model=request.model,
        live_count=live_count,
    )


# ── the interview a brand-new session is handed ────────────────────────────


def interview_prompt(role_hint: str, mandate: str) -> str:
    """The opening prompt for a session with no desk yet. PURE.

    Replaces the config form. It tells the new session to ask what it is
    actually for, offer a few concrete shapes plus a free-text option, and
    then name *itself* -- name, title, charter -- and write that down as one
    `YOS_DESK` line. The mandate is embedded verbatim so the new hire knows
    the company it just joined before it decides what it is.

    The example line is real, not illustrative: `parse_lines` reads it. If the
    two ever drift, a new hire's self-description is silently dropped and the
    session sits there with no desk.

    `hire.HOW_YOU_SOUND` and `hire.HOW_YOU_DECIDE` are embedded verbatim
    rather than paraphrased. A session at this door writes to the owner
    *before* it has a desk to be briefed with, so this is the only place the
    voice can reach it -- and a second wording of the same rules would be a
    second thing to keep in step.
    """
    hint = (role_hint or "").strip()
    said = f'"{hint}"' if hint else "(nothing yet -- ask them)"

    example = json.dumps(
        {
            "name": "inbox-hand",
            "label": "Email",
            "charter": (
                "You own the owner's inbox: you triage it every morning, draft "
                "replies to anything routine, and escalate anything that commits "
                "money or a date. You never send without a yes."
            ),
            "description": (
                "Triages the owner's inbox every morning and drafts the "
                "routine replies; never sends without a yes."
            ),
        }
    )

    return "\n".join(
        [
            "You have just been hired, and nobody has configured you.",
            "You have no name, no title and no charter. You are going to work",
            "them out in conversation and then write them down yourself.",
            "",
            "The company you have joined",
            mandate,
            "",
            "What the owner said they wanted",
            said,
            "",
            "That is a hint, not a specification. Your first message is a",
            "question: what do you actually want to use me for? Put a short",
            "list of concrete shapes the job could take underneath it, so they",
            "can answer off the list instead of writing a brief:",
            "",
            "  1. Do the whole job end to end, and show them only the result.",
            "  2. Draft everything, and wait for a yes before anything leaves.",
            "  3. Watch and report -- flag what matters, change nothing.",
            "  4. Something else -- say it in your own words.",
            "",
            "Ask follow-ups until you could do the job without asking again.",
            "Two or three questions is usually enough. Do not interview them.",
            "",
            "Ask in plain prose, written out in this conversation, and nothing",
            "else. Nobody is sitting at this window. The owner reads you on a",
            "board on his phone and his answer arrives here as an ordinary",
            "message, so anything that needs a keypress at this keyboard --",
            "an interactive question, a menu, a checklist, options you arrow",
            "through and select -- is a question he can never answer, and you",
            "will sit on it forever. Write the question and the numbered list",
            "as text. He replies with a number or a sentence.",
            "",
            HOW_YOU_SOUND,
            "",
            HOW_YOU_DECIDE,
            "",
            "The numbered list above is the one exception to two or three",
            "sentences: it is what lets him answer with a digit instead of a",
            "brief. Ask it as text, not with `ask` -- this is a conversation",
            "about the job, not a decision. Everything after the interview obeys",
            "the rules as written.",
            "",
            "Then name yourself. Do not ask them what to call you -- choose",
            "your own name, a title of one or two words for the chip beside it",
            "on the board (Negotiator, Researcher, Designer, Email, Travel",
            "Scout), a charter of one paragraph saying what you own and",
            "what you never touch, and a description: one or two plain",
            "sentences for the owner, shown under your name in his apps.",
            "",
            "When you have all four, emit exactly one line, at the start of a",
            "line, with nothing else on it:",
            "",
            f"{DESK_PREFIX} {example}",
            "",
            "Those four values are the shape, not your answer -- replace every",
            "one of them. Emitting that line is what puts you on the board;",
            "until you do, you are a session without a desk.",
            "",
            "After that, the owner changes you by telling you in the chat. When",
            "they do, print another such line carrying only the fields that",
            'changed -- a "label" on its own, or an "avatar" holding the path to',
            "a picture you generated for yourself. Two things you cannot change",
            "that way: who you report to, and anybody else. A line naming a desk",
            "that is not you is refused and logged.",
            "",
            # Last, because it is the thing this session does after it has a
            # desk rather than during the interview -- but here, because the
            # brief that would otherwise carry it is built from the desk this
            # session does not have yet, and a manager hired at this door was
            # measured claiming two hires it had no way to make.
            HIRING,
        ]
    )
