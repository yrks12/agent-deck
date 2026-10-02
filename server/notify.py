"""Send one message to the owner's phone, and be honest about whether it went.

A thin shell around the WhatsApp bridge at `~/Projects/comunicate_with_me`,
which owns the socket, the credentials and the recipient. Nothing here knows a
phone number or a token, and nothing here is imported by that project: the
contract is one subprocess, `node bin/wa-send.js --file <path>`.

`--file` rather than an argument, because the text carries shell commands and
folder paths and quoting them through a command line is a bug waiting to
happen. The file is written, used, and unlinked in a finally.

The single rule this module exists to enforce: **never report sent on a
non-zero exit.** wa-send.js documents its codes -- 0 sent, 1 send error, 3 the
daemon is not running, 4 not linked to WhatsApp -- and the two that matter are
3 and 4, because both mean the message is nowhere while everything upstream
looks healthy. An ask that silently never arrives reads, from the deck's side,
exactly like an ask the owner chose to ignore.

Both the script and the interpreter come from env vars so a test can point them
at a stub. No test in this repo runs the real bridge or sends a real message.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ENV_SCRIPT = "DECK_WA_SEND"
ENV_NODE = "DECK_WA_NODE"

#: The bridge as an HTTP endpoint, for a machine that cannot host one.
#: MEASURED on the Linux box: there is no wa-send.js there, so the subprocess
#: path returns "no such script" forever and the box tells him nothing. It
#: cannot get its own bridge -- pairing is interactive, and a second Baileys
#: client on the same number is the configuration most likely to get that
#: number banned. Set these and the deck talks to the Mac's bridge across the
#: tunnel instead. Unset on the Mac, where the local script is the proven path.
ENV_URL = "DECK_WA_URL"
ENV_TOKEN = "DECK_WA_TOKEN"

#: How long a thing that NEEDS HIM sits on the deck before it is allowed to
#: buzz his phone.
ENV_QUIET = "DECK_QUIET_SECONDS"

DEFAULT_SCRIPT = "~/Projects/comunicate_with_me/bin/wa-send.js"
DEFAULT_NODE = "node"

#: Thirty minutes. OWNER RULING 2026-09-30: *"we don't need the WhatsApp
#: bridge on the deck anymore, we have clear communication in the app -- only
#: in urgent cases should he send me; otherwise Atlas."*
#:
#: The app is where every question is answered. A blocking question he has
#: left there unanswered for half an hour has stopped being ordinary traffic:
#: a desk has been stalled that long, and that is the one kind of ask that is
#: urgent by itself. It was three minutes when the phone was the second
#: surface; it is now the line between "in the app" and "urgent".
#:
#: Only DEFERRABLE traffic waits here: a thing he must ANSWER. Things he is
#: TOLD -- a finished run, a desk's report -- do not reach the phone at all.
DEFAULT_QUIET_SECONDS = 1800.0

#: One urgent WhatsApp per this many seconds, whatever raised it.
DEFAULT_URGENT_GAP_SECONDS = 600.0


def _deck_toml_minutes(key: str, env) -> float | None:
    """`[notify] <key>` from deck.toml, in seconds, or None. Never raises.

    A missing file, a missing key or an unreadable file all mean "the
    default": a broken config must not silence his phone, and it must not
    take the collector tick down with it.
    """
    try:
        from . import deckconfig
        cfg = deckconfig.load(env=env)
        return float(getattr(cfg.notify, key)) * 60.0
    except Exception:
        return None


def quiet_seconds(env: dict | None = None) -> float:
    """How long a blocking ask sits in the app before it is urgent. Seconds.

    `DECK_QUIET_SECONDS` first (so he can widen or close it without touching
    a file), then deck.toml `[notify] ask_urgent_after_minutes`, then thirty
    minutes. Read per call rather than at import so a change lands on the next
    tick rather than on the next restart.

    An unreadable or negative value falls back. A garbled variable must not
    silence his phone forever -- that failure is invisible from the phone.
    Zero is honoured: it is the legitimate answer "buzz me at once".
    """
    source = os.environ if env is None else env
    raw = str(source.get(ENV_QUIET) or "").strip()
    if raw:
        try:
            seconds = float(raw)
        except ValueError:
            seconds = -1.0
        if seconds >= 0:
            return seconds
    configured = _deck_toml_minutes("ask_urgent_after_minutes", source)
    return DEFAULT_QUIET_SECONDS if configured is None else configured


def urgent_gap_seconds(env: dict | None = None) -> float:
    """The minimum gap between two urgent WhatsApps. deck.toml, else 10 min."""
    source = os.environ if env is None else env
    configured = _deck_toml_minutes("urgent_gap_minutes", source)
    return DEFAULT_URGENT_GAP_SECONDS if configured is None else configured

# The bridge is loopback HTTP to a local daemon. If it has not answered in this
# long it is wedged, and the caller is on a background path that must not hang.
DEFAULT_TIMEOUT = 20.0

# What wa-send.js means by its exit codes, so a failure reads as a thing to fix.
_CODE_HINTS = {
    1: "the bridge refused it",
    3: "the WhatsApp bridge daemon is not running",
    4: "the bridge is not linked to WhatsApp — re-pair the bot phone",
}


@dataclass(frozen=True)
class Sent:
    ok: bool
    code: int | None      # the script's exit status, or None if it never ran
    detail: str


def script_path(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get(ENV_SCRIPT) or DEFAULT_SCRIPT).expanduser()


def node_bin(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    return env.get(ENV_NODE) or DEFAULT_NODE


def child_env(reply_to: dict | None = None,
              base: dict | None = None) -> dict:
    """The environment wa-send.js is run with. PURE apart from reading os.environ.

    The child inherits this process's real environment -- it needs PATH and
    node resolution to work at all -- with the two variables the bridge reads
    as a return address set, or REMOVED, never left as inherited.

    Removal is the load-bearing half. MEASURED: the deck is normally launched
    from a terminal that is itself a Claude Code session, so
    `CLAUDE_CODE_MESSAGING_SOCKET` is already in its environment
    (`/tmp/cc-socks/16439.sock` on this machine). Inherited, it makes the
    bridge record every deck notification as having come from THAT session, and
    a reply typed on the phone is injected into a session that never asked
    anything. Unroutable is a correct answer; misrouted is not.
    """
    out = dict(os.environ if base is None else base)
    address = reply_to or {}
    for var, field in (("CLAUDE_CODE_MESSAGING_SOCKET", "socket"),
                       ("CLAUDE_CODE_SESSION_ID", "session_id")):
        value = address.get(field)
        if isinstance(value, str) and value.strip():
            out[var] = value.strip()
        else:
            out.pop(var, None)
    return out


def _send_http(body: str, url: str, token: str, *, reply_to: dict | None,
               timeout: float) -> Sent:
    """POST the bridge's own `/send` route. Never raises.

    Failures are mapped onto the SAME codes wa-send.js uses, because the
    runbook, the installer's checks and every log line downstream key on those:
    3 is "no daemon there", 4 is "the bridge is not linked to WhatsApp". A
    transport that invented its own vocabulary would make the box's failures
    unreadable next to the Mac's.

    The return address travels in the JSON body rather than the environment --
    `src/server.js#normalizeSession` reads it there, where the CLI reads it
    from env. Same routing, different door.
    """
    payload = {"text": body}
    if reply_to:
        payload["session"] = {
            "socket": reply_to.get("socket") or None,
            "session_id": reply_to.get("session_id") or None,
            "name": reply_to.get("name") or None,
        }

    request = urllib.request.Request(
        url.rstrip("/") + "/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answered = response.read().decode("utf-8", "replace")[:200]
        return Sent(True, 0, answered.strip() or "sent")
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            detail = err.read().decode("utf-8", "replace")[:200].strip()
        except Exception:
            pass
        # 503 is the bridge's own "unlinked" answer; everything else is a
        # refusal we must not dress up as delivery.
        code = 4 if err.code == 503 else 1
        return Sent(False, code,
                    f"bridge answered {err.code}: {detail or err.reason}")
    except urllib.error.URLError as err:
        # Nothing listening, no route, DNS. This is what the box gets today
        # pointed at the Mac, whose bridge binds loopback only.
        return Sent(False, 3, f"no bridge at {url}: {err.reason}")
    except Exception as err:  # a timeout is not a delivery either
        return Sent(False, None, f"bridge at {url} failed: {err}")


def send(text: str, *, env: dict | None = None,
         timeout: float = DEFAULT_TIMEOUT,
         reply_to: dict | None = None) -> Sent:
    """Send `text` to the configured recipient. Never raises.

    `env` is the *configuration* source only -- the child still inherits this
    process's real environment, because the bridge needs its own PATH and node
    resolution to work at all.

    `reply_to` is the *return address*: `{"socket": ..., "session_id": ...}`.
    The bridge records it against the WhatsApp message ids it sends, and routes
    a reply that quotes one of them back to that socket. Omit it and the send
    is recorded as unroutable, which is the honest answer for a notification
    nobody is expected to answer.
    """
    body = text if isinstance(text, str) else ""
    if body.strip() == "":
        return Sent(False, None, "nothing to send")

    source = os.environ if env is None else env
    url = (source.get(ENV_URL) or "").strip()
    if url:
        return _send_http(body, url, (source.get(ENV_TOKEN) or "").strip(),
                          reply_to=reply_to, timeout=timeout)

    script = script_path(env)
    if not script.exists():
        return Sent(False, None, f"no such script: {script}")

    handle, tmp = tempfile.mkstemp(prefix="deck-ask-", suffix=".md")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(body)

        argv = [node_bin(env), str(script), "--file", tmp]
        try:
            # Its own process group, so a timeout kills the whole tree. Killing
            # only the parent leaves a grandchild holding the stdout pipe, and
            # the "timeout" then blocks for as long as that child lives.
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                env=child_env(reply_to),
            )
        except OSError as err:
            return Sent(False, None, f"could not run {argv[0]}: {err}")

        try:
            out, errs = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return Sent(False, None, f"wa-send timed out after {timeout}s")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    code = proc.returncode
    if code == 0:
        return Sent(True, 0, (out or "sent").strip().splitlines()[-1]
                    if (out or "").strip() else "sent")

    said = (errs or out or "").strip().splitlines()
    detail = said[0] if said else ""
    hint = _CODE_HINTS.get(code)
    if hint:
        detail = f"{detail} ({hint})" if detail else hint
    return Sent(False, code, detail or f"wa-send exited {code}")
