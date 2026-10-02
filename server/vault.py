"""The vault: a live credential a desk needs, kept out of everything else.

The reference product (spec §"Measured findings") shows a field
that takes a live `sk_live_...` key with the words **"Stored securely, never
shown to your Bot."** It can say that because its agents never touch a shell:
they call tools through a broker, and the broker holds the key.

**We cannot say that, and this module must never claim it.** Our desks are real
Claude Code / OpenCode / Codex processes with a real shell in a real working
directory. A secret has to reach the tool the agent runs, and the only place to
put it is that process's environment — at which point the agent can read it,
because reading its own environment is one `env` away and no design here can
stop that. Any docstring, UI string or commit message that says "never shown to
the agent" is a lie, and a lie about a credential is worse than no vault.

So here is the promise this module actually makes, and it is a narrower one:

1. **The value is never in a repo, a prompt, a charter or a transcript.** It
   lives in one file, mode 0600, that no agent is told about. It is not in
   `roster.json`, not in a desk's `mission` or `charter`, and never passed on a
   command line, where it would sit in `ps` output for every process on the Mac.
2. **It reaches only the desks explicitly granted it**, and only inside the
   environment of the process spawned for that desk. A desk with no grant gets
   an empty dict — not a blank value, nothing at all.
3. **It is redacted from everything a human or another agent ever reads** —
   harvested output, WhatsApp messages, handoff evidence, the board. That is
   what `redactor()` is for, and it is the only defence that actually holds
   once the value is out in the world.

What that does defend against: a key in a screenshot, in a phone message, in a
log the deck files, in a transcript another agent later reads, in a git diff, in
`ps aux`, or readable by another user on the machine. What it does **not**
defend against: the granted desk itself. That desk is trusted with the key by
definition of having been granted it — the grant list is the whole security
model, so keep it short.

**`env_for` is the only door values come out of.** Nothing else in this module
returns one, `SecretMeta` cannot carry one, and `repr()` on it cannot print one.
If a second door is ever added, every guarantee above collapses to whatever that
door does — so do not add one; add a caller of `env_for` instead.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .paths import BUS_DIR

DEFAULT_PATH: Path = BUS_DIR / "vault.json"

# Owner-only. The vault sits in the same directory the deck already writes to,
# which is not itself private, so the mode is set on the file and re-asserted
# after every write rather than inherited from the umask.
FILE_MODE = 0o600

# A name becomes an environment variable, so it has to be a legal one. Refusing
# early is deliberate: silently mangling "stripe key" into "stripe_key" would
# hand a desk a variable it never asked for and leave the one it expects unset,
# which shows up as a confusing runtime failure rather than a clear one here.
_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# Short values are not redactable: replacing every occurrence of a 3-character
# secret would shred ordinary prose, and the shredded text is what a human then
# has to read. A credential shorter than this is not one worth storing.
MIN_REDACTABLE = 8


def MARKER_FOR(name: str) -> str:  # noqa: N802 - reads as a constant at call sites
    """What replaces a value in text. Names the secret, never shows it.

    Naming it is the point: "[redacted STRIPE_LIVE_KEY]" tells the owner which key
    was in that log line, which is exactly what he needs to know and carries no
    part of the value.
    """
    return f"[redacted {name}]"


@dataclass(frozen=True)
class SecretMeta:
    """Everything about a secret except the secret.

    There is no `value` field and there must never be one. This is the object
    that goes to the board, to `/api/...` responses and into log lines, so a
    value on it would leak through every one of them at once.
    """

    name: str                # "STRIPE_LIVE_KEY" -- also the env var name
    description: str         # what it is for, in the owner's words
    grants: tuple[str, ...]  # desk names allowed to receive it
    created_at: float
    last_used: float | None = None

    def __repr__(self) -> str:
        """Explicit, so a field added later cannot start printing itself.

        The generated dataclass repr prints every field. That is safe today
        only because no field holds a value; spelling the repr out means it
        stays safe on the day someone adds one by mistake.
        """
        return (f"SecretMeta(name={self.name!r}, "
                f"grants={self.grants!r}, "
                f"description={len(self.description)} chars, "
                f"last_used={self.last_used!r})")


# ── the store ──────────────────────────────────────────────────────────────


def _read(path: Path) -> list[dict]:
    """Raw rows, values included. Private on purpose — callers get meta().

    A missing or corrupt file is an empty vault, not an exception: the deck
    polls, and a daemon that dies on a half-written file is worse than one that
    reports no secrets.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = raw.get("secrets") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        value = row.get("value")
        if not name or not isinstance(value, str) or not value:
            continue
        grants = row.get("grants")
        out.append({
            "name": name,
            "value": value,
            "description": str(row.get("description") or ""),
            "grants": tuple(str(g) for g in grants) if isinstance(grants, (list, tuple)) else (),
            "created_at": float(row.get("created_at") or 0.0),
            "last_used": (None if row.get("last_used") is None
                          else float(row["last_used"])),
        })
    return out


def _write(path: Path, rows: list[dict]) -> None:
    """Atomic write that is 0600 from the moment it exists.

    The temp file is opened with the mode rather than chmod'ed afterwards:
    between `open` and `chmod` there is a window in which a world-readable file
    holding a live key exists on disk, and this is the one file where that
    window is not acceptable. `os.replace` carries the temp file's mode across,
    and the final chmod re-asserts it for a vault that predates this code.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = json.dumps({"version": 1, "secrets": rows}, indent=2)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    os.chmod(path, FILE_MODE)


def _meta_of(row: dict) -> SecretMeta:
    return SecretMeta(
        name=row["name"],
        description=row["description"],
        grants=tuple(row["grants"]),
        created_at=row["created_at"],
        last_used=row["last_used"],
    )


def _find(rows: list[dict], name: str) -> int:
    for index, row in enumerate(rows):
        if row["name"] == name:
            return index
    raise KeyError(name)


# ── writing ────────────────────────────────────────────────────────────────


def put(path: Path, *, name: str, value: str, description: str,
        grants: tuple[str, ...] | list[str] = (),
        now: float | None = None) -> SecretMeta:
    """Store or rotate one secret. Returns its meta — never the value.

    Rotation replaces in place and the old value is gone from the file, because
    a vault that keeps history is a vault that keeps a revoked key alive.

    Nothing here logs, prints or raises with the value in it. A `ValueError`
    from this function names the *field* that was wrong and never quotes what
    was passed, so a bad-input traceback in a log cannot become the leak.
    """
    name = str(name or "")
    if not _NAME_RE.match(name):
        raise ValueError("name must be a valid environment variable name "
                         "(letters, digits, underscore; not starting a digit)")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: value is required and must not be empty")

    at = time.time() if now is None else float(now)
    rows = _read(path)
    row = {
        "name": name,
        "value": value,
        "description": str(description or ""),
        "grants": tuple(dict.fromkeys(str(g) for g in grants)),
        "created_at": at,
        "last_used": None,
    }
    try:
        index = _find(rows, name)
    except KeyError:
        rows.append(row)
    else:
        # A rotation keeps the desks that were already trusted with it, unless
        # the caller passed a grant list; re-granting by hand after every
        # rotation is the kind of chore that ends in "grant everyone".
        if not row["grants"]:
            row["grants"] = tuple(rows[index]["grants"])
        row["created_at"] = at
        rows[index] = row
    _write(path, rows)
    return _meta_of(row)


def grant(path: Path, name: str, desk: str) -> SecretMeta:
    """Allow `desk` to receive `name`. Idempotent. KeyError if unknown.

    KeyError rather than a silent no-op: a typo'd secret name that quietly did
    nothing would leave a desk running without the key and nobody knowing why.
    """
    rows = _read(path)
    index = _find(rows, str(name))
    grants = tuple(dict.fromkeys((*rows[index]["grants"], str(desk))))
    rows[index] = {**rows[index], "grants": grants}
    _write(path, rows)
    return _meta_of(rows[index])


def revoke(path: Path, name: str, desk: str) -> SecretMeta:
    """Stop `desk` receiving `name`. KeyError if unknown.

    Takes effect at the next spawn, not immediately: a desk already running has
    the value in its environment and nothing here can reach in and remove it.
    Revoking a live key means rotating it as well — say so wherever this is
    surfaced.
    """
    rows = _read(path)
    index = _find(rows, str(name))
    grants = tuple(g for g in rows[index]["grants"] if g != str(desk))
    rows[index] = {**rows[index], "grants": grants}
    _write(path, rows)
    return _meta_of(rows[index])


def forget(path: Path, name: str) -> None:
    """Remove a secret entirely. A name that is not there is a no-op.

    No-op rather than KeyError, because this is the function someone reaches
    for in a hurry after a key leaks, and it must not fail on the second run.
    """
    rows = _read(path)
    kept = [r for r in rows if r["name"] != str(name)]
    if len(kept) != len(rows):
        _write(path, kept)


# ── reading ────────────────────────────────────────────────────────────────


def meta(path: Path) -> list[SecretMeta]:
    """What is in the vault, with no values. Safe to hand to anything."""
    return [_meta_of(row) for row in _read(path)]


def env_for(path: Path, desk: str, *, now: float | None = None) -> dict[str, str]:
    """**The only function that returns a secret value.**

    Returns `{NAME: value}` for every secret `desk` was granted, to be merged
    into the environment of the process spawned for that desk — never onto a
    command line, where `ps` would show it to every process on the machine.

    A desk with no grants gets `{}`. That is the default and it is the safe
    one: a new desk is trusted with nothing until someone says otherwise.

    Records `last_used` so the board can show a key nobody has taken in months,
    which is the one that should be revoked. That write is the only side effect.
    """
    wanted = str(desk)
    rows = _read(path)
    out: dict[str, str] = {}
    touched = False
    at = time.time() if now is None else float(now)
    for index, row in enumerate(rows):
        if wanted in row["grants"]:
            out[row["name"]] = row["value"]
            rows[index] = {**row, "last_used": at}
            touched = True
    if touched:
        _write(path, rows)
    return out


def redactor(path: Path) -> Callable[[str], str]:
    """A function that removes every stored value from a piece of text.

    Run it over anything a human or another agent will read: harvested output,
    a WhatsApp message, handoff evidence, a board field. It is exact-substring
    replacement, not a shape rule, which is what makes it safe on prose — the
    word "sk_live_" and the variable name "STRIPE_LIVE_KEY" survive untouched
    because neither is the value. (`asking.redact` and `handoff.redact` are the
    shape rules; this is the complement to them, catching a key whose shape
    nobody anticipated.)

    Longest first, so a secret that contains another one cannot leave a tail of
    the longer value behind.

    The vault is read once, when the redactor is built. A secret stored after
    that is not covered until a fresh redactor is made — so build it per
    message, not once at start-up.
    """
    pairs = sorted(
        ((row["value"], MARKER_FOR(row["name"])) for row in _read(path)
         if len(row["value"]) >= MIN_REDACTABLE),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )

    def scrub(text: str) -> str:
        out = str(text or "")
        for value, marker in pairs:
            if value in out:
                out = out.replace(value, marker)
        return out

    return scrub
