"""The ask loop: turn one unanswered question into a rule that never asks again.

**This is not remote prompt-answering, and nothing here ever will be.** A
`PreToolUse` hook is synchronous and sits on the hot path of every tool call, so
it cannot wait for a human on a phone; and a permission prompt already drawn in
a running session cannot be answered from outside, because the session's socket
protocol has no frame for it (spec M1). Both facts are settled. Anyone who later
reads this module as "answer the prompt from WhatsApp" will build the one thing
that has been proven impossible twice.

What actually happens:

1. `autoreview.evaluate` returns `ask`. The session draws its normal prompt and
   waits, exactly as it does today. The hook has already returned.
2. *Separately, in the background*, the deck sends the owner one WhatsApp message
   naming the agent, the tool, the exact argument and the folder.
3. He replies one word. `always` writes a permanent `always_allow` rule,
   `never` writes a `deny`, `once` writes nothing.
4. The **next** time that class of thing happens, `evaluate` answers it silently
   and the agent never stops.

**The value is the second time, not the first.** The first question still costs
the owner an interruption and the agent a stall; what this buys is that it costs him
that once rather than forty times. Judge every design choice here against that:
a rule that is too narrow to ever match again makes the feature worthless, and a
rule that is too wide hands over the machine. `rule_from` is where that line is
drawn, and it is drawn conservatively -- exact tool, exact folder, and the
argument widened only to its leading verb.

Pure functions where it can be: `compose`, `parse_reply`, `rule_from` and
`redact` touch nothing. The rest is a small JSON file, capped, because
`suppressed` is consulted on the hot path.
"""

from __future__ import annotations

import glob
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path

from .autoreview import (
    ALWAYS_ALLOW,
    DENY,
    HANDOFF_PATTERNS,
    Rule,
    load_rules,
    save_rules,
    shell_segments,
)
from . import owner
from .expiry import DEFAULT_TTL, sweep
from .paths import BUS_DIR

DEFAULT_PATH: Path = BUS_DIR / "asks.json"

# `DEFAULT_TTL` is imported, not redefined: asks and handoffs must age out on
# the same clock. See server/expiry.py for why it is four hours, and why
# expiring is a removal rather than an approval.

# Newest kept. `suppressed()` reads this file inside a tool call, so it is
# capped rather than allowed to grow the way edits.jsonl did (spec, §3).
MAX_ASKS = 50

# No 0/O/1/l/I. This id is read off a phone screen and typed back by hand;
# a character that can be mistyped is a wrong answer to the wrong question.
ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
ID_LEN = 5

REPLIES = ("once", "always", "never")

# Shown in place of the `always` option when the secure-handoff floor owns this
# subject. He can still say "once" -- he just cannot make it standing policy.
HANDOFF_NOTE = "not available here (credential, payment or irreversible)"

# The pseudo-tool `POST /api/elicitation` files an MCP input dialog under. It is
# not a real tool and there is no rule engine behind it.
ELICITATION_TOOL = "Elicitation"
# `always` cannot mean anything for one. `hooks/cc-elicit.js` has a single word
# in its vocabulary and no way to invent the value a form asked for, so no rule
# could ever turn the next decline into an accept. Offering the option anyway
# would be offering something that cannot happen -- which is the same class of
# lie as `delivered: true` over an open modal.
ELICITATION_NOTE = "not available — the deck cannot fill a form in for you"


@dataclass(frozen=True)
class Ask:
    id: str            # short, unambiguous, safe to type on a phone
    ts: float
    agent: str         # the desk/session name that asked
    tool: str
    subject: str       # autoreview.tool_subject(...) output
    cwd: str
    answered: str | None = None   # "once" | "always" | "never" | None
    # When the question timed out. Set by `expire`, and NOT an answer: see
    # `status` below and the module docstring of server/expiry.py.
    expired_at: float | None = None
    # When he answered. The clock a `once` grant expires on -- it has to be
    # this and not `ts`, because the gap that matters is between his tap and
    # the desk's retry, not between the question and the retry.
    answered_at: float | None = None
    # When a `once` grant was spent. Set by `grant()` and never cleared: this
    # single field is what makes "once" mean once rather than "from now on".
    consumed_at: float | None = None
    # When the desk was told it may carry on. Set by `mark_resumed()` and never
    # cleared, and it is the loop guard: one ask is resumed exactly once, ever.
    # Durable rather than in-memory because two taps on Approve -- or a Baileys
    # replay of one WhatsApp reply -- are the ordinary case, not an exotic one.
    resumed_at: float | None = None

    @property
    def status(self) -> str:
        """"pending" | "answered" | "expired" -- derived, never stored.

        Derived on purpose. A stored status and a stored `answered` can drift,
        and the drift that matters is an expired record that also looks
        answered. `answered` wins here, so a reply that arrives late is still a
        real decision by a human rather than an expiry pretending to be one.
        """
        if self.answered is not None:
            return "answered"
        if self.expired_at is not None:
            return "expired"
        return "pending"


# ── the secure-handoff floor, re-asked at subject level ────────────────────


def is_handoff_subject(subject: str) -> bool:
    """`autoreview.is_handoff` for a subject that has already been computed.

    Deliberately the same patterns rather than a second list: two floors that
    can drift apart is how one of them quietly stops covering something.
    """
    low = str(subject).lower()
    return any(fnmatchcase(low, p) for p in HANDOFF_PATTERNS)


# ── redaction ──────────────────────────────────────────────────────────────

# `https://user:pass@host` -- the password is the second field, and it is the
# one thing in a command line that is a secret by construction.
_URL_USERINFO = re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")

# Literals that are secrets whatever surrounds them.
_SECRET_LITERAL = re.compile(
    r"\b(?:sk|rk|pk)-[A-Za-z0-9_\-]{8,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{8,}"
    r"|\bxox[baprs]-[A-Za-z0-9\-]{8,}"
    r"|\bAKIA[0-9A-Z]{8,}"
    r"|\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]+"
)

# Anything long enough, and mixed enough, to be a key we do not have a prefix
# for. Path segments and English words do not reach 24 characters of
# letters-and-digits, so this fails closed without eating ordinary text.
_LONG_RUN = re.compile(
    r"\b(?=[A-Za-z0-9_\-]*[A-Za-z])(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{24,}\b"
)

# `--token=x`, `password: y`, `Authorization: Bearer z`.
_SECRET_KV = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[-_]?key|apikey|credential|"
    r"auth|authorization)\b(\s*[:=]\s*|\s+)(\S+)"
)

MASK = "[redacted]"

# The phone is the whole audience. A command longer than this is truncated
# rather than paged: he needs to recognise it, not audit it.
SUBJECT_MAX = 120


def redact(text: str) -> str:
    """Strip anything credential-shaped. PURE.

    This runs on every message before it leaves the machine. It fails closed:
    an over-eager mask costs the owner a "what was that?", an under-eager one puts a
    live token in a WhatsApp thread forever.
    """
    out = str(text)
    out = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{MASK}@", out)
    out = _SECRET_LITERAL.sub(MASK, out)
    out = _LONG_RUN.sub(MASK, out)
    out = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    return out


def _display(subject: str) -> str:
    safe = redact(subject).replace("\n", " ").strip()
    if len(safe) > SUBJECT_MAX:
        safe = safe[: SUBJECT_MAX - 1].rstrip() + "…"
    return safe


def short_cwd(path_str: str) -> str:
    """`~/Projects/acme` -- the folder as a human names it. PURE.

    Public because the phone message is no longer the only thing that shows an
    ask: the client API puts the same question on a card, and two renderers for
    "which folder" is how the card and the message end up naming different
    places for one decision.
    """
    home = str(Path.home())
    if path_str == home:
        return "~"
    if path_str.startswith(home + os.sep):
        return "~" + path_str[len(home):]
    return path_str


# ── rule_from: the dangerous one ───────────────────────────────────────────

_SUBCOMMAND = re.compile(r"[A-Za-z][A-Za-z0-9._\-]*\Z")

#: Every shell metacharacter that can begin a SECOND command. A subject holding
#: one of these is not a single action, so its leading verb says nothing about
#: what it does -- and widening on that verb grants whatever follows.
#:
#: Measured: `cd ~/Projects && for d in */; do ... git ... done` produced
#: `cd /Users/you/*`, which matches `cd /Users/you/Projects && rm -rf .` and
#: `cd /Users/you/.ssh && cat id_rsa`. Same class as
#: `rm build/out` -> `rm*`: a widening that swallows the actual action.
#:
#: `&` covers `&&`, `|` covers `||`, and `$(` is caught by its `$`. A `$` in an
#: ordinary argument only costs that subject its widening, which is the safe
#: direction to be wrong in.
_COMPOUND_CHARS = frozenset("&|;`$\n\r")


def _widen(subject: str) -> str:
    """The argument pattern a standing rule gets. PURE.

    Conservative on purpose. `git push origin main` becomes `git push*`, never
    `git *`: the branch and the remote are the parts that vary between two runs
    of the same intent, the verb is not. Keeping only the first token would
    make one answer about `git push` silently approve `git commit`, `git reset`
    and everything else that shares a binary -- which is exactly how an
    approval system ends up worse than no approval system.

    A second token is kept only when it reads as a subcommand (a bare word). A
    flag or a path is an argument, so `npm --version` widens to `npm*` and a
    file path widens to itself.

    A subject carrying a shell metacharacter is not widened at all -- see
    `_COMPOUND_CHARS`. It is pinned to itself, glob-escaped and with no
    trailing wildcard, so `always` still covers the exact repeat it was granted
    for and covers nothing else. Granting nothing would satisfy "does not
    over-grant" and make the answer useless; granting the prefix grants the
    command hiding after the `&&`.
    """
    text = str(subject)
    if any(ch in _COMPOUND_CHARS for ch in text):
        return glob.escape(text)

    tokens = str(subject).split()
    if not tokens:
        # An operation that takes no arguments -- `mcp__x__list_calendars` --
        # has no subject to narrow. This used to raise, which was correct while
        # every tool here had one; now it would turn the owner tapping "always"
        # into a 500, and an unanswerable question is the bug being fixed. The
        # rule this produces is still pinned to the exact tool and the exact
        # folder, so it means "this operation, here" and nothing wider.
        return "*"

    keep = [tokens[0]]
    if len(tokens) > 1 and _SUBCOMMAND.match(tokens[1]):
        keep.append(tokens[1])
    elif len(tokens) > 1 and "/" in tokens[1]:
        # A path, not a subcommand. Dropping it collapses the rule to the bare
        # verb, and `rm build/out` becomes `rm*` -- one tap of "always" then
        # permits `rm -rf .` in that folder forever. Keep the DIRECTORY: a
        # repeat of the same intent is the same verb on a sibling file, which
        # this still covers, while the rest of the tree does not.
        head = tokens[1].rsplit("/", 1)[0]
        keep.append(head + "/" if head else "/")

    # Escape before appending the wildcard: a `*` or `[` already in the command
    # would otherwise widen the pattern beyond what was asked about.
    return glob.escape(" ".join(keep)) + "*"


def rule_from(ask: Ask, kind: str) -> Rule:
    """The rule an answer creates. PURE.

    Three things stay pinned to what was actually asked about:

    * the **tool** is exact -- an answer about Bash says nothing about Write;
    * the **folder** is exact, and glob-escaped, so an answer given about one
      repo cannot leak into a sibling;
    * the **argument** is widened only by `_widen`.

    `once` is not a rule and never becomes one.
    """
    if kind == "always":
        rule_kind = ALWAYS_ALLOW
    elif kind == "never":
        rule_kind = DENY
    else:
        raise ValueError(f"{kind!r} creates no rule")

    return Rule(
        id=f"ask-{ask.id}",
        kind=rule_kind,
        tool=ask.tool,
        pattern=_widen(ask.subject),
        cwd=glob.escape(ask.cwd),
        note=f"from {ask.agent} on WhatsApp",
    )


# ── desk_rules: what "Always allow" means ─────────────────────────────────
#
# MEASURED on the box, 2026-09-30: 30 "always" answers had each written a rule,
# and the same class of call kept asking. 43 of the 66 always_allow rules were
# pinned to the exact command (`_widen` pins anything compound, and an agent's
# commands nearly always are), a Read was pinned to the one file, the folder was
# exact so a `cd shared` escaped it, and no rule named the desk. `rule_from`
# above is still what a bare `never`, and an `always` the deck cannot put a
# desk to, write. What a desk's "always" writes is this.

#: The folders under which "the file's own folder" is far too wide to grant: a
#: Read of ~/x must not become "read anything in your home directory".
_TOO_WIDE = frozenset({"", "/", str(Path.home())})


def _verb(words: list[str]) -> str:
    """One command's class: its verb, plus a subcommand or a folder. PURE.

    `git push origin main` -> `git push*`; `ls -la /x` -> `ls*`;
    `cat .env` -> `cat .env*`; `cat /repo/console/a.py` -> `cat /repo/console/*`. The same rule `_widen`
    uses for a single command, applied to each command of a compound line.
    """
    keep = [words[0]]
    if len(words) > 1 and "/" in words[1]:
        head = words[1].rsplit("/", 1)[0]
        keep.append(head + "/" if head else "/")
    elif len(words) > 1 and not words[1].startswith("-"):
        # A subcommand (`push`) or the object itself (`.env`): either way the
        # thing he read on the card, so `cat .env` does not become `cat*`.
        keep.append(words[1])
    return glob.escape(" ".join(keep)) + "*"


def _desk_class(ask: Ask) -> list[str]:
    """The patterns one "always" grants -- exactly what the card shows. PURE.

    * Bash: one verb per command in the line, in order, de-duplicated. A line
      that cannot be split honestly (a `$(...)`, a here-doc, bad quoting) is
      pinned to itself.
    * Read / Write / Edit / NotebookEdit: the file's folder and below, unless
      that folder is `/` or the home directory, where it stays the one path.
    * WebFetch: the site.
    * Anything else (an MCP operation): the tool itself.
    """
    subject = str(ask.subject)
    if ask.tool == "Bash":
        segments = shell_segments(subject)
        if segments is None:
            return [glob.escape(subject)]
        out: list[str] = []
        for words in segments:
            verb = _verb(words)
            if verb not in out:
                out.append(verb)
        return out
    if ask.tool in ("Read", "Write", "Edit", "NotebookEdit"):
        folder = os.path.dirname(subject.rstrip("/"))
        if folder in _TOO_WIDE or not subject.startswith("/"):
            return [glob.escape(subject) + "*"]
        return [glob.escape(folder) + "/*"]
    if ask.tool == "WebFetch":
        match = re.match(r"\A([a-z][a-z0-9+.\-]*://[^/?#]+)", subject, re.I)
        return [glob.escape(match.group(1)) + "/*"] if match else [
            glob.escape(subject)]
    return ["*"]


def desk_class(ask: Ask) -> list[str]:
    """The patterns one "always" grants -- exactly what the card shows. PURE.

    See `_desk_class` for the shape per tool; redacted runs are wildcards.
    """
    out: list[str] = []
    for pattern in _desk_class(ask):
        pattern = _unmask(pattern)
        if pattern not in out:
            out.append(pattern)
    return out


def _unmask(pattern: str) -> str:
    """A redacted run becomes a wildcard. PURE.

    `record` stores the subject redacted, and a long mixed run in a PATH is
    redacted too (`/var/folders/6p/j24bh4m57_5g2fdngw4yvjdr0000gn/T/...`).
    Left as the literal `[redacted]` the rule could never match the real
    path again -- measured: ask `myvdp` on the box is exactly that. The
    run is one opaque token, so `*` in its place is the honest pattern.
    """
    return pattern.replace(glob.escape(MASK), "*")


def desk_rules(ask: Ask, desk: str) -> list[Rule]:
    """The standing rules "Always allow" writes for `desk`. PURE.

    Keyed by the desk's NAME, never a session id: a wake gives the desk a new
    session and the rule must still be its own. Scoped to the folder the
    question was asked in and everything below it. One rule per pattern so
    each can be listed and removed on its own.
    """
    patterns = desk_class(ask)
    return [
        Rule(
            id=f"ask-{ask.id}" + (f".{n}" if n else ""),
            kind=ALWAYS_ALLOW,
            tool=ask.tool,
            pattern=pattern,
            cwd=glob.escape(ask.cwd),
            note=f"{desk} said always to ask {ask.id}",
            desk=desk,
        )
        for n, pattern in enumerate(patterns)
    ]


def describe_class(ask: Ask, desk: str = "") -> str:
    """The card's words for "always". PURE, and built from `desk_class` so
    the promise and the stored rule cannot differ."""
    shown = ", ".join(f"`{p}`" for p in desk_class(ask))
    who = f"{desk} may" if desk else "allow"
    return f"{who} {shown} in {short_cwd(ask.cwd)} and below, never ask again"


# ── the message ────────────────────────────────────────────────────────────

_VERBS = {
    "Bash": "run",
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "NotebookEdit": "edit",
    "WebFetch": "fetch",
    ELICITATION_TOOL: "reply to",
}

# `mcp__<server>__<operation>`. The server segment may itself contain single
# underscores, so the split is on the DOUBLE underscore, non-greedy from the
# left -- measured wire names include `mcp__claude_ai_Google_Calendar__
# list_calendars` and `mcp__docker-mcp__container_logs`.
_MCP_TOOL = re.compile(r"\Amcp__(.+?)__(.+)\Z")
# Claude Code's own prefix for a connector it hosts. It is plumbing, not a
# thing the owner named, so it does not belong on his phone.
_MCP_VENDOR_PREFIX = "claude_ai_"


def describe_tool(tool: str) -> str:
    """`mcp__claude_ai_Google_Calendar__list_calendars` ->
    `the Google Calendar tool list_calendars`. PURE.

    The wire name is not a question a human can answer. Widening the
    prompt-time door to every tool that can draw a modal is only an improvement
    if what arrives is still legible -- otherwise the owner has swapped a stall
    for a riddle, and the counter-argument against widening was right.

    A non-MCP tool is returned unchanged: `AskUserQuestion` reads fine as
    itself, and inventing prose for a tool nobody has seen yet would be worse
    than the plain name.
    """
    match = _MCP_TOOL.match(str(tool))
    if match is None:
        return str(tool)
    server = match.group(1)
    if server.startswith(_MCP_VENDOR_PREFIX):
        server = server[len(_MCP_VENDOR_PREFIX):]
    server = server.replace("_", " ").replace("-", " ").strip()
    return f"the {server} tool {match.group(2)}" if server else match.group(2)


def compose(ask: Ask) -> str:
    """The WhatsApp message text. PURE.

    First line is the whole question: which agent, and what it wants to do.
    Nothing credential-shaped survives `redact`. Eight lines, because it is read
    on a phone by someone who will not scroll.
    """
    subject = _display(ask.subject)
    verb = _VERBS.get(ask.tool) or f"use {describe_tool(ask.tool)}"
    if _VERBS.get(ask.tool) is None and subject:
        verb += " with"

    if ask.tool == ELICITATION_TOOL:
        always = f"2 always — {ELICITATION_NOTE}"
    elif subject:
        always = f"2 always — {describe_class(ask)} (this desk only)"
    else:
        # No arguments, so there is no pattern worth showing him -- quoting the
        # bare `*` the rule carries reads like a blank cheque when it is not.
        always = (f"2 always — allow {describe_tool(ask.tool)} there, "
                  "never ask again")

    return "\n".join([
        f"**{ask.agent}** wants to {verb} {subject}".rstrip(),
        f"in {short_cwd(ask.cwd)}",
        "",
        "1 once — just this time",
        always,
        "3 never — refuse it from now on",
        "",
        f"Reply with a number or the word. (ask {ask.id})",
    ])


def refusal(ask: Ask) -> str:
    """What the owner is told when `always` could not be honoured. PURE."""
    return "\n".join([
        "Allowed once — but *always* is not available for this one.",
        f"{_display(ask.subject)}",
        "",
        "It is a credential, a payment or something irreversible, so it asks "
        "every time by design.",
    ])


def resume_text(ask: Ask) -> str:
    """What the DESK is told once its question has been answered yes. PURE.

    The counterpart to `app._blocked_message`, and it has to be, because that
    message is why this one is needed. A denied desk is told "do not retry it in
    a loop -- say you are waiting on ask <id> and carry on with something else",
    which is right: a hook cannot wait on a human. But it means the desk is now
    parked by instruction, and writing the rule does not un-park it. Measured
    three times in one run: he tapped Approve, the rule landed, and nothing
    happened until a human typed into the session.

    Three things have to be in here or it does not work:

    * **Which action.** A desk can be blocked on several at once, so the ask id,
      the tool and the exact subject are all named. "You may proceed" would make
      the agent guess.
    * **Do it now.** The desk is at its prompt with no reason to take a turn;
      the message has to be an instruction, not a notification.
    * **What to do if it is refused again.** Otherwise this becomes the loop the
      original denial was written to prevent. A second refusal is a NEW ask with
      a new id, and the owner decides that one too.

    Redacted like everything else that leaves this module: the subject is the
    agent's own argument string and can carry a token.
    """
    return "\n".join([
        f"Approved: ask {ask.id}. {owner.title()} answered {ask.answered!r}.",
        "",
        f"You may now run {ask.tool} with {_display(ask.subject)} "
        f"in {short_cwd(ask.cwd)}.",
        "",
        "Do that now -- it is the action you stopped on. If it is refused "
        f"again, do not retry: that is a new question for {owner.name()}, and you should "
        "say which one you are waiting on and carry on with something else.",
    ])


def mark_resumed(path: Path, ask_id: str, *,
                 now: float | None = None) -> bool:
    """Stamp `resumed_at`. True the FIRST time only -- this is the loop guard.

    Read-modify-write under the same lock `grant()` uses, for the same reason:
    `_save` is atomic but deciding what to save from what was just read is not,
    and two taps on Approve arriving together is the ordinary case. Whoever
    wins the lock sends; the loser is told no and sends nothing.
    """
    with _GRANT_LOCK:
        asks = _load(path)
        index = next((i for i, a in enumerate(asks) if a.id == ask_id), None)
        if index is None or asks[index].resumed_at is not None:
            return False
        asks[index] = replace(asks[index],
                              resumed_at=time.time() if now is None else now)
        _save(path, asks)
        return True


def parse_reply(text: str | None) -> str | None:
    """`"always"` / `"once"` / `"never"`, or None when it is not an answer. PURE.

    Strict on purpose. An ordinary sentence that happens to contain "never"
    ("never mind, I'll do it") must not be read as a standing deny, so the reply
    has to be the word or the number and nothing else. An ask id may lead it,
    because that is how a reply gets attributed when several are outstanding.
    """
    if not isinstance(text, str):
        return None
    body = text.strip().lower().rstrip(".!,")
    if not body:
        return None

    parts = body.split()
    if len(parts) == 2 and re.fullmatch(rf"[{ID_ALPHABET}]{{3,8}}:?", parts[0]):
        body = parts[1]
    elif len(parts) != 1:
        return None

    body = body.strip().rstrip(".!,")
    if body in REPLIES:
        return body
    if body in ("1", "2", "3"):
        return REPLIES[int(body) - 1]
    return None


# ── persistence ────────────────────────────────────────────────────────────


def _ask_from(raw: object) -> Ask | None:
    if not isinstance(raw, dict):
        return None
    answered = raw.get("answered")
    if answered not in REPLIES:
        answered = None
    try:
        ts = float(raw.get("ts", 0.0))
    except (TypeError, ValueError):
        ts = 0.0
    ident = str(raw.get("id", ""))
    if not ident:
        return None
    def stamp(key: str) -> float | None:
        try:
            value = raw.get(key)
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    return Ask(
        id=ident,
        ts=ts,
        agent=str(raw.get("agent", "")),
        tool=str(raw.get("tool", "")),
        subject=str(raw.get("subject", "")),
        cwd=str(raw.get("cwd", "")),
        answered=answered,
        expired_at=stamp("expired_at"),
        answered_at=stamp("answered_at"),
        # An ask written before this field existed reads back as unspent. That
        # is the right default: those rows are all long past the grant TTL, so
        # `grant()` refuses them on the clock instead.
        consumed_at=stamp("consumed_at"),
        # An ask written before this field existed reads back as un-resumed.
        # Harmless: every such row is long settled, and `mark_resumed` is only
        # ever reached from the answer that is being made right now.
        resumed_at=stamp("resumed_at"),
    )


def _load(path: Path) -> list[Ask]:
    """Every ask on disk. A missing or corrupt file is no asks, not a crash."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    entries = raw.get("asks") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    return [a for a in (_ask_from(e) for e in entries) if a is not None]


def _save(path: Path, asks: list[Ask]) -> None:
    """tmp file then os.replace, like autoreview.save_rules."""
    path = Path(path)
    body = {"version": 1, "asks": [asdict(a) for a in asks[-MAX_ASKS:]]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2))
    os.replace(tmp, path)


def _new_id(taken: set[str]) -> str:
    for _ in range(64):
        ident = "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LEN))
        if ident not in taken:
            return ident
    # 30^5 is 24 million; 64 collisions means the alphabet is broken, and a
    # duplicate id would attribute an answer to the wrong question.
    raise RuntimeError("could not mint a unique ask id")


def record(path: Path, *, agent: str, tool: str, subject: str, cwd: str,
           ts: float | None = None) -> Ask:
    """Write down one question. Returns the Ask, with its id.

    **The subject is redacted before it is stored, not on the way out.**
    Redacting in `compose()` alone protected exactly one consumer: the file on
    disk still held `Bearer sk-ant-...` verbatim, and every other reader -- the
    client API, the board, a backup, any panel written later -- got the raw
    value. Redacting here means the secret never lands in the first place.

    A redacted subject stays usable: `rule_from` generalises to the leading
    tokens plus a wildcard, so `curl -H "Authorization: [redacted]"` still
    yields a working `curl -H*` rule -- and a rule that cannot contain a
    credential is the better rule anyway. It also means two calls that differ
    only in a rotated token collapse to one question instead of two.
    """
    asks = _load(path)
    ask = Ask(
        id=_new_id({a.id for a in asks}),
        ts=time.time() if ts is None else float(ts),
        agent=str(agent),
        tool=str(tool),
        subject=redact(str(subject)),
        cwd=str(cwd),
    )
    _save(path, asks + [ask])
    return ask


def pending(path: Path) -> list[Ask]:
    """Asks that are still live questions, oldest first.

    An expired ask is not pending. It is not answered either -- nothing was
    granted -- it has simply stopped being something the owner can usefully act on,
    so it leaves the board. `expire` is what puts it in that state; this
    function only stops showing it.
    """
    return [a for a in _load(path) if a.status == "pending"]


def find(path: Path, ask_id: str) -> Ask | None:
    """One ask by id, whatever its status. None when there is no such ask.

    `pending()` hides answered and expired rows on purpose, but a caller
    replying to one has to tell those two apart -- "you already said never to
    this" and "this timed out and granted nothing" are different sentences to a
    human, and answering both with "unknown" is how a client shows neither.
    """
    return next((a for a in _load(path) if a.id == ask_id), None)


def expire(path: Path, *, now: float | None = None,
           ttl: float = DEFAULT_TTL) -> list[Ask]:
    """Time out unanswered asks. Returns the ones that changed.

    **This grants nothing.** No rule is written, `autoreview.evaluate` still
    answers `ask` for the same call, and the agent's own permission prompt is
    still on its screen waiting for a human -- an expired ask denies by
    omission. The only effect is that the row leaves `pending()`.

    An already-answered ask is never touched, so the provenance of a standing
    rule cannot be rewritten by a sweep.
    """
    at = time.time() if now is None else float(now)
    asks = _load(path)
    after, changed = sweep(
        asks,
        now=at,
        ttl=ttl,
        ts_of=lambda a: a.ts,
        is_open=lambda a: a.status == "pending",
        mark=lambda a: replace(a, expired_at=at),
    )
    if changed:
        _save(path, after)
    return changed


def answer(path: Path, ask_id: str, reply: str,
           rules_path: Path | None = None,
           desk: str = "") -> tuple[Ask, Rule | None]:
    """Apply one reply. Returns the answered Ask and the rule it created.

    The rule is written to `rules_path` when one is given -- that write is the
    entire point of the feature, and leaving it to a caller is how it ends up
    never happening.

    Raises KeyError for an unknown id and ValueError for a reply that is not an
    answer; both are things the caller must tell the owner about rather than guess at.
    """
    asks = _load(path)
    index = next((i for i, a in enumerate(asks) if a.id == ask_id), None)
    if index is None:
        raise KeyError(ask_id)

    ask = asks[index]
    if ask.answered is not None:
        # Already settled. A duplicate reply -- Baileys replays on reconnect --
        # must not append a second copy of the same rule.
        return ask, None

    kind = parse_reply(reply)
    if kind is None:
        raise ValueError(f"not an answer: {reply!r}")

    # The floor. `always` on a credential or an irreversible action is
    # downgraded to `once` when the deck cannot say WHICH desk it is for --
    # never honoured globally and never silently dropped: `refusal(ask)` is
    # what goes back to the phone. With a desk, it is honoured for that desk
    # (see `desk_rules`), which is what the card promised him.
    if kind == "always" and not desk and is_handoff_subject(ask.subject):
        kind = "once"
    # Same downgrade, different reason: an elicitation has no rule engine behind
    # it, so an `always_allow` row for it would sit in the file matching nothing
    # forever. Writing a rule that can never fire is how a permission system
    # ends up looking like it decided something when it did not.
    if kind == "always" and ask.tool == ELICITATION_TOOL:
        kind = "once"

    settled = replace(ask, answered=kind, answered_at=time.time())
    asks[index] = settled
    _save(path, asks)

    if kind == "once":
        # Still no rule -- `once` must never become standing policy. What it
        # leaves behind instead is `answered_at` with no `consumed_at`, which
        # `grant()` reads as one unspent permission. See that function for why
        # the difference matters.
        return settled, None

    if kind == "always" and desk:
        made = desk_rules(settled, desk)
        if rules_path is not None:
            existing = load_rules(rules_path)
            held = {(r.desk, r.kind, r.tool, r.pattern, r.cwd)
                    for r in existing}
            fresh = [r for r in made
                     if (r.desk, r.kind, r.tool, r.pattern, r.cwd) not in held]
            if fresh:
                save_rules(rules_path, existing + fresh)
        return settled, made[0]

    rule = rule_from(settled, kind)
    if rules_path is not None:
        existing = load_rules(rules_path)
        same = (rule.kind, rule.tool, rule.pattern, rule.cwd)
        if not any((r.kind, r.tool, r.pattern, r.cwd) == same and not r.desk
                   for r in existing):
            save_rules(rules_path, existing + [rule])
    return settled, rule


#: Serialises the read-modify-write inside `grant`. `_save` is atomic, but
#: deciding WHAT to save from what was just read is not, and "the agent retried
#: twice quickly" is the ordinary case here rather than an exotic one. Two
#: threads reading the same unspent grant and both allowing is precisely the
#: replay this field exists to prevent. One process, so a lock is the whole
#: control; a second daemon on the same file would need more, and there is
#: never a second daemon on the same `CLAUDE_CONFIG_DIR`.
_GRANT_LOCK = threading.Lock()


def grant(path: Path, *, tool: str, subject: str, cwd: str,
          now: float | None = None, ttl: float = DEFAULT_TTL) -> Ask | None:
    """Spend one `once` answer. Returns the Ask it came from, or None.

    **This is what makes "once" mean anything.** `answer()` writes a rule for
    `always` and for `never` and deliberately nothing for `once`. That was fine
    while the only consumer was `PreToolUse` -- the agent was sitting at its own
    prompt and the human answering "once" answered it at the keyboard. Once
    `/api/permission` began denying at prompt time it became a dead end: the
    owner tapped approve, the desk retried, `evaluate` found no rule, and the
    desk was denied again. He tapped approve and nothing happened.

    Both edges are load-bearing and they pull in opposite directions:

    * **A grant that can be replayed is a permission leak.** `consumed_at` is
      stamped and saved BEFORE this returns, under `_GRANT_LOCK`, so a second
      caller -- another retry, a racing thread, a restarted daemon re-reading
      the file -- finds it spent. It is never cleared.
    * **A grant that expires before the retry is the bug being fixed.** The
      clock runs from `answered_at`, not from when the question was asked: the
      gap that matters is between his tap on a phone and the desk's next
      attempt. It ages out on `DEFAULT_TTL`, the same clock the rest of the ask
      loop uses, because a grant left lying around indefinitely is a permission
      nobody remembers giving.

    Matching is exact on tool, subject and folder, and the subject is redacted
    first because that is how `record` stored it -- comparing a raw subject
    would never match for exactly the commands that carry a credential, which
    is the same trap `suppressed` documents.

    Never raises: a missing or unreadable file is "no grant", not a crash. This
    runs with a permission prompt half-drawn.
    """
    at = time.time() if now is None else float(now)
    subject = redact(str(subject))

    with _GRANT_LOCK:
        try:
            asks = _load(path)
        except Exception:  # pragma: no cover -- _load is already total
            return None
        index = next(
            (i for i, a in enumerate(asks)
             if a.answered == "once" and a.consumed_at is None
             and a.answered_at is not None and at - a.answered_at <= ttl
             and a.tool == tool and a.subject == subject and a.cwd == cwd),
            None)
        if index is None:
            return None
        spent = replace(asks[index], consumed_at=at)
        asks[index] = spent
        try:
            _save(path, asks)
        except OSError:
            # The grant could not be marked spent, so it must not be handed
            # out: an unwritable file would otherwise turn "once" into
            # "forever" for as long as the disk stays full.
            return None
        return spent


def suppressed(path: Path, *, tool: str, subject: str, cwd: str,
               window: float = 900, now: float | None = None) -> bool:
    """Has this exact question already gone out recently?

    The flood guard. An agent in a retry loop asks the same thing forty times in
    a minute; the owner gets one message. Matching is exact on all three fields --
    a *different* question is never suppressed, because a swallowed question is
    an agent stuck forever with nobody knowing why.

    The incoming subject is redacted before comparing, because `record` stores
    it redacted. Comparing a raw subject against a stored one would never match
    for exactly the commands that carry a credential -- so the guard would fail
    open on retry loops around auth, which is the loop most likely to happen.
    It also means a rotated token is still the same question.

    **A SPENT ROW SUPPRESSES NOTHING.** A row that is both answered and consumed
    is an answer that has already been handed out and cannot be handed out
    again, so it is not a question anybody is currently sitting on. Letting it
    suppress produced a dead end nobody could see: the owner tapped "once", the
    desk spent the grant, needed the same thing a second time, and for the rest
    of the fifteen minutes was denied "ask (unrecorded)" while he was never
    re-asked. Safe -- it denies -- and invisible, which is worse.

    The boundary is deliberately narrow, because the flood guard is still right:

    * PENDING inside the window still suppresses. That is the retry loop this
      function exists for, and it is the case that actually happens.
    * ANSWERED but NOT yet consumed still suppresses: the grant is live and the
      retry it was written for is about to be allowed by `grant()`. Asking again
      now would be asking twice for one decision.
    * Only ANSWERED **and** CONSUMED steps aside. `consumed_at` is set by
      `grant()` alone, and only ever on an `answered == "once"` row, so this
      cannot widen to any other reply.
    """
    at = time.time() if now is None else float(now)
    subject = redact(str(subject))
    for ask in _load(path):
        if ask.answered is not None and ask.consumed_at is not None:
            continue  # spent: its answer is gone, so it stands in for nothing
        if ask.tool == tool and ask.subject == subject and ask.cwd == cwd:
            if at - ask.ts <= window:
                return True
    return False
