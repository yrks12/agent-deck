"""Who the deck works for, by name, in the words its desks read.

A desk's brief, a call's opening line and a grant receipt all mention the one
person the deck belongs to. That name is the owner's, not the product's, so it
is configuration: `DECK_OWNER_NAME` in the environment first (it reaches the
unit through `agentdeck.env`), then `[owner] name` in deck.toml, then the
neutral "the owner". Nothing here ever raises: a broken config must not take a
brief or a call down with it -- the doctor is what reports config trouble.
"""
from __future__ import annotations

import os
import re
from typing import Mapping

DEFAULT = "the owner"

#: The id his own messages carry on the wire (`from`, `author`, a thread's
#: participants). "owner" unless a deck already has history under another id:
#: changing it on a live deck would orphan that history and every installed app.
DEFAULT_HANDLE = "owner"
_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def name(env: Mapping[str, str] | None = None) -> str:
    """The owner as a desk should call him: "Sam", or "the owner"."""
    env = os.environ if env is None else env
    named = (env.get("DECK_OWNER_NAME") or "").strip()
    if named:
        return named
    try:
        from . import deckconfig
        named = deckconfig.load(env=env).owner.name.strip()
    except Exception:  # noqa: BLE001 - config trouble is reported by the doctor
        named = ""
    return named or DEFAULT


def title(env: Mapping[str, str] | None = None) -> str:
    """`name()` fit to open a sentence: "Sam", or "The owner"."""
    n = name(env)
    return n[:1].upper() + n[1:]


def handle(env: Mapping[str, str] | None = None) -> str:
    """The owner's wire id: `DECK_OWNER_HANDLE`, deck.toml `[owner] handle`,
    else "owner". A value that is not a short lowercase slug is ignored."""
    env = os.environ if env is None else env
    named = (env.get("DECK_OWNER_HANDLE") or "").strip()
    if not named:
        try:
            from . import deckconfig
            named = deckconfig.load(env=env).owner.handle.strip()
        except Exception:  # noqa: BLE001 - config trouble is reported by the doctor
            named = ""
    return named if _HANDLE.match(named) else DEFAULT_HANDLE
