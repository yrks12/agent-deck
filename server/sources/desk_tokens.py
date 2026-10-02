"""Claude tokens per desk, counted once, from the desks' own transcripts.

One meter for the whole deck: the Money Board charges each desk its Claude
work from it, and the token-diet audit reads the same numbers. Use
`DeskTokens(PROJECTS_DIR).measure(desks)`; never count a transcript twice.

* A transcript belongs to the desk whose cwd is the LONGEST prefix of its
  project dir (worktrees and sub-folders of a workspace count for it).
  Subagent transcripts (`<session>/subagents/*.jsonl`) count for the parent.
* Desks that share one cwd (yye-ops and yye-growth in one repo) are told apart
  by who is seated in that session (office.json, remembered once seen), else by
  the desk name the transcript says most often. ASSUMED, not measured: a
  session nobody was seen seated in and that names no desk goes to the first
  desk on the roster with that cwd.
* Streaming writes one message 2-3 times; usage is deduped by message.id
  (the same correction `sources/transcript.py` makes).
* Only new bytes are read; offsets and totals persist in `cache_path`, so a
  restarted deck does not re-read 900 MB of transcripts.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import atomic
from ..paths import BUS_DIR, PROJECTS_DIR, slug_for

#: $ per million tokens: input, output, cache read. A cache write (5 min) is
#: 1.25x input. First match on the model id wins; unknown models price as Opus.
PRICES: tuple[tuple[str, tuple[float, float, float]], ...] = (
    ("fable", (10.0, 50.0, 0.25)), ("mythos", (10.0, 50.0, 0.25)),
    ("opus-5-5", (4.0, 20.0, 0.20)), ("opus", (5.0, 25.0, 0.50)),
    ("sonnet-5", (2.0, 10.0, 0.20)), ("sonnet", (3.0, 15.0, 0.30)),
    ("haiku", (1.0, 5.0, 0.10)),
)
DEFAULT_PRICE = (4.0, 20.0, 0.20)
DEFAULT_CACHE = BUS_DIR / "money" / "desk-tokens.json"
_HEAD = 256 * 1024            # bytes of a shared-cwd transcript read for a name
_KEEP_IDS = 64                # recent message ids remembered per file


def price_of(model: str) -> tuple[float, float, float]:
    m = (model or "").lower()
    return next((p for key, p in PRICES if key in m), DEFAULT_PRICE)


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    by_model: dict[str, list[int]] = field(default_factory=dict)
    by_day: dict[str, int] = field(default_factory=dict)

    def add(self, model: str, day: str, *, inp: int, out: int, cr: int, cw: int) -> None:
        self.input += inp; self.output += out
        self.cache_read += cr; self.cache_write += cw
        row = self.by_model.setdefault(model or "?", [0, 0, 0, 0])
        for i, v in enumerate((inp, out, cr, cw)):
            row[i] += v
        self.by_day[day] = self.by_day.get(day, 0) + inp + out + cr + cw

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def api_usd(self) -> float:
        """What these tokens would cost at API prices (desks run on a plan)."""
        usd = 0.0
        for model, (i, o, cr, cw) in self.by_model.items():
            pi, po, pr = price_of(model)
            usd += (i * pi + o * po + cr * pr + cw * pi * 1.25) / 1e6
        return usd

    def as_dict(self) -> dict:
        return {"input": self.input, "output": self.output,
                "cache_read": self.cache_read, "cache_write": self.cache_write,
                "total": self.total, "api_usd": round(self.api_usd(), 2),
                "by_day": dict(sorted(self.by_day.items()))}


def _office_seats() -> dict[str, str]:
    try:
        board = json.loads((BUS_DIR / "office.json").read_text())
        return {sid: str(e.get("name")) for sid, e in (board.get("sessions") or {}).items()
                if isinstance(e, dict) and e.get("name")}
    except (OSError, ValueError, AttributeError):
        return {}


class DeskTokens:
    def __init__(self, projects_dir: Path = PROJECTS_DIR, *,
                 cache_path: Path = DEFAULT_CACHE,
                 seats: Callable[[], dict[str, str]] = _office_seats) -> None:
        self.root, self.cache_path, self._seats = Path(projects_dir), Path(cache_path), seats
        try:
            self.cache = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            self.cache = {}
        self.cache.setdefault("files", {})
        self.cache.setdefault("seats", {})

    # ── attribution ──────────────────────────────────────────────────────
    def _owners(self, dirname: str, desks) -> list[str]:
        best, names = -1, []
        for d in desks:
            s = slug_for(d.cwd.rstrip("/"))
            if dirname == s or dirname.startswith(s + "-"):
                if len(s) > best:
                    best, names = len(s), [d.name]
                elif len(s) == best:
                    names.append(d.name)
        return names

    def _pick(self, path: Path, session: str, names: list[str]) -> str:
        if len(names) == 1:
            return names[0]
        seated = self.cache["seats"].get(session)
        if seated in names:
            return seated
        try:
            with path.open("rb") as fh:
                head = fh.read(_HEAD).decode("utf-8", "replace")
        except OSError:
            head = ""
        counts = {n: head.count(n) for n in names}
        top = max(counts.values())
        return next(n for n in names if counts[n] == top) if top else names[0]

    # ── reading ──────────────────────────────────────────────────────────
    def _read(self, path: Path, entry: dict) -> None:
        size = path.stat().st_size
        if size < entry.get("offset", 0):          # rewritten: start over
            entry.update(offset=0, usage=[], ids=[])
        if size == entry.get("offset", 0):
            return
        ids = list(entry.get("ids") or [])
        seen = set(ids)
        with path.open("rb") as fh:
            fh.seek(entry.get("offset", 0))
            chunk = fh.read()
        end = chunk.rfind(b"\n") + 1               # a half-written line waits
        rows: dict[tuple[str, str], list[int]] = {
            (m, d): v for m, d, *v in entry.get("usage") or []}
        for raw in chunk[:end].splitlines():
            if b'"usage"' not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            msg = rec.get("message") if isinstance(rec, dict) else None
            usage = msg.get("usage") if isinstance(msg, dict) else None
            if not isinstance(usage, dict):
                continue
            mid = str(msg.get("id") or "")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid); ids.append(mid)
            key = (str(msg.get("model") or "?"), str(rec.get("timestamp") or "")[:10])
            row = rows.setdefault(key, [0, 0, 0, 0])
            for i, k in enumerate(("input_tokens", "output_tokens",
                                   "cache_read_input_tokens",
                                   "cache_creation_input_tokens")):
                row[i] += int(usage.get(k) or 0)
        entry["offset"] = entry.get("offset", 0) + end
        entry["ids"] = ids[-_KEEP_IDS:]
        entry["usage"] = [[m, d, *v] for (m, d), v in rows.items()]

    def measure(self, desks) -> dict[str, Usage]:
        """Every desk's tokens since its transcripts began. Desks with none: absent."""
        desks = [d for d in desks if getattr(d, "cwd", "")]
        self.cache["seats"].update(self._seats() or {})
        out: dict[str, Usage] = {}
        files = self.cache["files"]
        if self.root.is_dir():
            for proj in self.root.iterdir():
                names = self._owners(proj.name, desks) if proj.is_dir() else []
                if not names:
                    continue
                for path in proj.rglob("*.jsonl"):
                    rel = path.relative_to(self.root)
                    session = rel.parts[1].removesuffix(".jsonl")
                    entry = files.setdefault(str(rel), {})
                    try:
                        self._read(path, entry)
                    except OSError:
                        continue
                    if entry.get("desk") not in names:
                        entry["desk"] = self._pick(path, session, names)
                    usage = out.setdefault(entry["desk"], Usage())
                    for m, d, i, o, cr, cw in entry.get("usage") or []:
                        usage.add(m, d, inp=i, out=o, cr=cr, cw=cw)
        self.cache["measured_at"] = time.time()
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic.write_text(self.cache_path, json.dumps(self.cache))
        except OSError:
            pass
        return out
