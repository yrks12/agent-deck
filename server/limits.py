"""Has a desk just hit a usage limit? Read from its own transcript (S9c).

MEASURED in the box's binary (claude 2.1.287): the CLI recognises a limit
message by these prefixes (its `r8r` list) and composes the session/weekly one
as `You've hit your ${limit}${resets}`. MEASURED in real transcripts: an API
error is an assistant line with `isApiErrorMessage: true`, a top-level `error`
kind and the text. A login error (`authentication_failed`) is not a limit; a
plain 429 retry message is not one either -- only the CLI's limit wording or a
`billing_error` is.

Only the LAST assistant line counts: a desk that has answered since has been
served, and must not be moved for a limit that is over.
"""

from __future__ import annotations

import json
from pathlib import Path

#: Copied from the binary's own list (`r8r`), not paraphrased.
LIMIT_PREFIXES = (
    "You've hit your", "You've reached your", "You're out of usage credits",
    "Your org is out of usage", "Your seat type doesn't include usage",
    "Your usage allocation has been disabled", "Your group's usage limit is set to $0",
    "You're out of extra usage", "Your seat type doesn't include extra usage",
)
LIMIT_ERRORS = ("rate_limit", "billing_error")
TAIL_BYTES = 64 * 1024


def _text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return "".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def is_limit(entry: dict) -> bool:
    if entry.get("isApiErrorMessage") is not True:
        return False
    if entry.get("error") not in LIMIT_ERRORS:
        return False
    return _text(entry).lstrip().startswith(LIMIT_PREFIXES)


def limit_hit(transcript: Path) -> bool:
    """True when the transcript's last assistant line is a usage-limit error."""
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("type") == "assistant":
            return is_limit(entry)
    return False


def desk_limited(desk) -> bool:
    """The desk's last session hit a limit. Never raises."""
    from . import paths, wake
    try:
        job = wake.last_job(desk.name)
    except Exception:  # noqa: BLE001
        return False
    if job is None:
        return False
    return limit_hit(paths.transcript_path(job.cwd or desk.cwd, job.session_id))
