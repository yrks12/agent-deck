"""Decisions (K4): a desk asks the owner to pick, and he picks with one tap.

Before this a desk's question was prose. He typed an answer, the desk parsed
it, and nothing on the screen said which questions were settled. The only
buttons in the app were tool approvals.

A decision is two records, kept apart on purpose:

* **The card**, in `decisions.json` beside the roster: prompt, 2-4 options,
  `state` (`open` -> `answered` | `skipped`) and his `answer`. This is the part
  that CHANGES, so it lives in a small file rewritten atomically, not in the
  append-only message log.
* **The message**, appended to `messages.jsonl` by `office.send` from the desk
  to the owner's inbox with `kind: "decision"` -- so the card sits in the
  conversation at the moment it was asked, in the same order as everything
  else, and an old client that only knows `text` still reads the question.

Two processes write the card file: the deck (his answer, a skip) and a desk's
own `server.deck_mcp` subprocess (the `ask`). Every read-modify-write holds an
`flock` on a sibling lock file, and the write itself is `atomic.write_text`.
"""

from __future__ import annotations

import fcntl
import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import atomic, office
from .paths import BUS_DIR

DEFAULT_PATH = BUS_DIR / "decisions.json"

STATES = ("open", "answered", "skipped")
STYLES = ("primary", "default", "danger")
MIN_OPTIONS, MAX_OPTIONS = 2, 4
LABEL_MAX = 40
PROMPT_MAX = 600
HELP_MAX = 4000
VALUE_MAX = 2000
#: Settled cards kept on disk. Open ones are never dropped.
KEEP_SETTLED = 500


class DecisionError(Exception):
    """Refusal. `reason` is a stable slug; `status` is the HTTP answer."""

    def __init__(self, reason: str, detail: str = "", status: int = 400) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        self.status = status


def _path(path) -> Path:
    return Path(path or DEFAULT_PATH)


def load(path=None) -> dict[str, dict]:
    """`{id: card}`. Never raises: a missing or broken file is no decisions."""
    try:
        raw = json.loads(_path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    cards = raw.get("decisions") if isinstance(raw, dict) else None
    if not isinstance(cards, dict):
        return {}
    return {k: v for k, v in cards.items() if isinstance(v, dict)}


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _save(path: Path, cards: dict[str, dict]) -> None:
    settled = sorted((c for c in cards.values() if c.get("state") != "open"),
                     key=lambda c: c.get("ts") or 0.0)
    for old in settled[:max(0, len(settled) - KEEP_SETTLED)]:
        cards.pop(old["id"], None)
    atomic.write_text(path, json.dumps({"version": 1, "decisions": cards}))


def public(card: dict) -> dict:
    """The wire shape (K4). What a client draws; nothing about the desk."""
    return {
        "id": card["id"],
        "prompt": card.get("prompt", ""),
        "help": card.get("help", ""),
        "options": [dict(o) for o in card.get("options") or []],
        "allow_custom": bool(card.get("allow_custom", True)),
        "state": card.get("state", "open"),
        "answer": card.get("answer"),
    }


def _options(options) -> list[dict]:
    if not isinstance(options, list) or not (
            MIN_OPTIONS <= len(options) <= MAX_OPTIONS):
        raise DecisionError("bad_options",
                            f"give {MIN_OPTIONS} to {MAX_OPTIONS} options")
    out: list[dict] = []
    for raw in options:
        if isinstance(raw, str):
            raw = {"label": raw}
        if not isinstance(raw, dict):
            raise DecisionError("bad_options", "an option is a label or "
                                "{label, value?, style?}")
        label = str(raw.get("label") or "").strip()
        if not label:
            raise DecisionError("bad_options", "every option needs a label")
        if len(label) > LABEL_MAX:
            raise DecisionError("label_too_long", f"{label[:20]!r}... is over "
                                f"{LABEL_MAX} characters; put detail in help")
        value = str(raw.get("value") or "").strip() or label
        style = str(raw.get("style") or "default")
        if style not in STYLES:
            raise DecisionError("bad_style", f"style is one of {STYLES}")
        out.append({"label": label, "value": value[:VALUE_MAX], "style": style})
    return out


def text_for(card: dict) -> str:
    """The message text: the question and its options, for a reader that does
    not draw cards (an old client, the replay a restarted desk is handed)."""
    lines = [card["prompt"]]
    lines += [f"- {o['label']}" for o in card["options"]]
    return "\n".join(lines)


def create(desk: str, prompt: str, options, help: str = "",
           allow_custom: bool = True, *, path=None) -> dict:
    """File a new open card for `desk` and post it into its owner thread."""
    prompt = str(prompt or "").strip()
    if not prompt:
        raise DecisionError("empty_prompt", "a decision needs a question")
    if len(prompt) > PROMPT_MAX:
        raise DecisionError("prompt_too_long", f"keep the question under "
                            f"{PROMPT_MAX} characters; put detail in help")
    card = {
        "id": f"dec_{uuid.uuid4().hex[:12]}",
        "desk": desk,
        "prompt": prompt,
        "help": str(help or "")[:HELP_MAX],
        "options": _options(options),
        "allow_custom": bool(allow_custom),
        "state": "open",
        "answer": None,
        "ts": time.time(),
    }
    target = _path(path)
    with _locked(target):
        cards = load(target)
        cards[card["id"]] = card
        _save(target, cards)
    sent = office.send(office.OWNER_INBOX, text_for(card), sender=desk,
                       extra={"kind": "decision", "decision": public(card)})
    if not sent.get("ok"):
        raise DecisionError("queue_failed", str(sent.get("detail") or ""), 500)
    return card


def find(decision_id: str, *, path=None) -> dict | None:
    return load(path).get(str(decision_id or ""))


def answer(decision_id: str, value, *, path=None) -> dict:
    """Mark the card answered with `value`. Returns the settled card.

    A `skipped` card can still be answered: he scrolled up and tapped it, and
    that tap is the newest thing he said about it.
    """
    value = str(value if value is not None else "").strip()
    if not value:
        raise DecisionError("empty_value", "an answer needs a value")
    target = _path(path)
    with _locked(target):
        cards = load(target)
        card = cards.get(str(decision_id or ""))
        if card is None:
            raise DecisionError("unknown_decision",
                                f"no decision {decision_id!r}", 404)
        if card.get("state") == "answered":
            raise DecisionError("already_answered",
                                f"answered: {card.get('answer')!r}", 409)
        values = {o["value"] for o in card.get("options") or []}
        if value not in values and not card.get("allow_custom", True):
            raise DecisionError("not_an_option",
                                "this decision takes one of its options only")
        card.update(state="answered", answer=value[:VALUE_MAX],
                    answered_at=time.time())
        _save(target, cards)
    return card


def skip_open(desk: str, *, path=None) -> list[dict]:
    """Every open card of `desk`, marked `skipped`. He moved on (the reference's
    `widgetSkipped`): the card stops asking, and still reads what it asked."""
    target = _path(path)
    if not target.exists():
        return []
    with _locked(target):
        cards = load(target)
        skipped = [c for c in cards.values()
                   if c.get("desk") == desk and c.get("state") == "open"]
        for card in skipped:
            card["state"] = "skipped"
        if skipped:
            _save(target, cards)
    return skipped
