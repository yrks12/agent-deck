"""Two writers must never destroy the file they are both saving.

The failure, found by a concurrency test on `POST /v1/agents` and then swept
across the package: every atomic write here derived its temp path from the
*target* name -- `roster.json` -> `roster.json.tmp`. That name is shared. Two
writers racing produce this:

    A: write roster.json.tmp
    B: write roster.json.tmp        (same file, clobbers A's bytes)
    A: os.replace(tmp, roster.json) (moves it -- tmp no longer exists)
    B: os.replace(tmp, roster.json) -> FileNotFoundError

and the loser's write is gone. Worse than a lost write, the window between the
two replaces is one where the target can be missing entirely, so a reader can
see no roster at all -- which reads as "all my agents disappeared".

`os.replace` is atomic. Deriving the source path from the destination is what
was not. The fix is a unique temp file per write, in the same directory so the
replace stays on one filesystem.

Every test here asserts the file ends up PRESENT and PARSEABLE. "No exception"
would also be what a silently-lost write looks like.
"""

import json
import threading
from pathlib import Path

import pytest

from server import atomic


def test_a_write_lands(tmp_path):
    """The good signal first: it actually writes."""
    p = tmp_path / "thing.json"
    atomic.write_text(p, '{"a": 1}')
    assert json.loads(p.read_text()) == {"a": 1}


def test_the_temp_file_is_not_left_behind(tmp_path):
    p = tmp_path / "thing.json"
    atomic.write_text(p, "x")
    assert [q.name for q in tmp_path.iterdir()] == ["thing.json"]


def test_two_writers_never_lose_the_file(tmp_path):
    """The one that matters. Under the old shared-name scheme this raised
    FileNotFoundError; the file must exist and parse after every round."""
    p = tmp_path / "roster.json"
    errors: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            for _ in range(40):
                atomic.write_text(p, json.dumps({"writer": n}))
        except BaseException as exc:      # noqa: BLE001 - the point is to catch it
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"a concurrent write raised: {errors[0]!r}"
    assert p.exists(), "the target was destroyed by a concurrent write"
    assert json.loads(p.read_text())["writer"] in range(6)


def test_a_reader_never_sees_a_partial_file(tmp_path):
    """A reader racing a writer sees the old bytes or the new bytes, never
    half of each. That is the whole reason for the temp-then-replace dance."""
    p = tmp_path / "big.json"
    atomic.write_text(p, json.dumps({"n": 0, "pad": "x" * 50_000}))
    seen: list[int] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                seen.append(json.loads(p.read_text())["n"])
            except FileNotFoundError:
                seen.append(-1)          # the failure this test exists to catch
            except json.JSONDecodeError:
                seen.append(-2)          # a torn read

    t = threading.Thread(target=reader)
    t.start()
    for n in range(1, 60):
        atomic.write_text(p, json.dumps({"n": n, "pad": "x" * 50_000}))
    stop.set()
    t.join()

    assert seen, "the reader never ran"
    assert -1 not in seen, "a reader saw the file missing"
    assert -2 not in seen, "a reader saw a torn file"


def test_the_mode_is_honoured(tmp_path):
    """The vault writes secrets at 0600. mkstemp creates at 0600 already, but
    a caller that asks for a mode must get it -- and must not get 0644 by
    inheriting the process umask through a plain write."""
    p = tmp_path / "vault.json"
    atomic.write_text(p, "{}", mode=0o600)
    assert (p.stat().st_mode & 0o777) == 0o600


def test_a_failed_write_leaves_the_previous_content(tmp_path):
    """If serialisation blows up mid-write, the old file must still be there.
    Losing yesterday's roster to today's bug is the compounding failure."""
    p = tmp_path / "thing.json"
    atomic.write_text(p, '{"good": true}')

    class Boom(str):
        def encode(self, *a, **k):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        atomic.write_text(p, Boom("nope"))
    assert json.loads(p.read_text()) == {"good": True}
    assert [q.name for q in tmp_path.iterdir()] == ["thing.json"]
