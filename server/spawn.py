"""Spawn: turn a desk into a live process.

Two paths: a Terminal window (the AppleScript channel `server/focus.py`
already uses) and a headless `claude --bg` agent (Claude Code's own
durable-agent primitive). `build_argv` and `build_applescript` are pure --
the functions that actually shell out are a one-line call around them.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import approval, deck_mcp, deskperms, office, owner, pretrust, rules
from .hire import brief
from .paths import CLAUDE_HOME
from .roster import Desk

# launchd's own name for a logged-in graphical session. Over ssh, or under a
# daemon, `launchctl managername` says "Background" or "StandardIO" instead --
# and that is the case a platform check cannot see.
GUI_MANAGER = "Aqua"

# Never emit one of these -- it would make the whole approval layer
# decorative. `build_argv` is swept for this in tests/test_spawn.py.
BANNED_FLAGS = {
    "--dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
}


class SpawnError(Exception):
    """Refusal to spawn. `reason` is a stable machine-readable slug."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def build_argv(desk: Desk, *, background: bool, seed: str = "",
               settings: str = "", boss_address: str = "") -> list[str]:
    """The command line for `desk`. PURE -- never touches the filesystem and
    never spawns anything.

    `settings` is what puts a hired desk under the approval hook, and it is why
    `server/approval.py` exists. The rule engine, the ask ledger, the always-
    allow rules and the approval cards were all real code that nothing in a real
    session ever called, because `hooks/cc-approve.js` was registered in no
    settings scope on this machine -- and it deliberately still is not.
    `--settings` is per-session, so the deck governs the agents it hired and the
    owner's own terminals stay his. Empty by default: a caller that knows
    nothing about approval still builds a working command line.

    `seed` is the session's opening prompt, and it is a *prompt*, not a system
    prompt: the interview door (`api.Surface.interview_agent`) hands over
    `onboard.interview_prompt`, which is a thing to do once -- ask the owner
    what the job is and then name yourself -- not an identity to re-send on
    every request. Empty by default, so an ordinary desk starts exactly as it
    always has.
    """
    if desk.engine == "claude":
        argv = ["claude"]
        if background:
            argv.append("--bg")
        # THE join. Every lookup on this deck is keyed on the session's name --
        # `roster.occupancy` seats a desk by it, `app._try_inject` finds the
        # socket by it, `harvest.poll` takes the actor from it, and the office
        # hook matches queued mail on it. Left alone, `claude` derives a name
        # from the cwd and appends a disambiguator: a desk the deck allocated
        # `.../workspaces/new-hire-511bba` for came back as the session
        # `new-hire-511bba-59`, and NOTHING could address it. The desk read
        # OFFLINE with its window up, a message to it answered
        # `delivered: false`, and its own YOS_DESK line was never applied.
        argv += ["--name", desk.name]
        if desk.model:
            argv += ["--model", desk.model]
        # OWNER RULING 2026-09-30 (server/deskperms.py). MEASURED: a `--bg`
        # session ignores `defaultMode: bypassPermissions` in settings and
        # stays in "default"; the flag is what takes effect. Not
        # `--dangerously-skip-permissions` (BANNED_FLAGS): same mode, but the
        # sweep stays as the guard against a process-wide switch.
        argv += ["--permission-mode", deskperms.PERMISSION_MODE]
        if settings:
            # Before the seed, which stays the last positional. `--settings`
            # takes a value: a flag appended after the seed would swallow the
            # opening prompt, and the desk would be asked nothing -- the same
            # class of fault as `opencode <seed>` reading the seed as a path.
            argv += ["--settings", settings]
        # The desk's own computer and the deck's own tools (say, ask,
        # message_desk), as ONE `--mcp-config`, with its name bound HERE rather
        # than in anything the model sends -- see server/computer_mcp.py and
        # server/deck_mcp.py. Before `--append-system-prompt`: `--mcp-config`
        # is variadic and would swallow a positional seed as a second config
        # file. A name no container can carry loses the computer only.
        try:
            # A FILE, not inline JSON: `--resume` re-reads a file, so an
            # installed connector can take effect (docs/connectors.md).
            argv += ["--mcp-config", deck_mcp.config_file(desk.name)]
        except ValueError:
            pass  # not even a name: the desk still starts
        # The FULL brief, not `desk.mission`. The mission is a one-liner that
        # names nobody; the brief is what says "your boss is Acme, not the
        # owner". A message can be compacted away -- a system prompt is re-sent
        # on every request and cannot be. Putting identity here is the fix for
        # a desk quietly forgetting whose it is. See server/compaction.py.
        argv += ["--append-system-prompt",
                 brief(desk, boss_address=boss_address)]
        if seed:
            argv.append(seed)
        return argv
    if desk.engine == "opencode":
        argv = ["opencode"]
        if desk.model:
            # `-m, --model`, in the format provider/model. A bare model name
            # with no provider prefix is not guaranteed to resolve -- the
            # roster stores whatever the client sent and checks nothing.
            argv += ["--model", desk.model]
        if seed:
            # NOT a bare positional. `opencode [project]` reads its positional
            # as "path to start opencode in", so appending the seed handed the
            # opening prompt over as a directory: opencode started somewhere
            # nonsensical and nobody was ever asked anything. The other two
            # engines really do take a positional prompt; this one does not.
            argv += ["--prompt", seed]
        return argv
    if desk.engine == "codex":
        return ["codex", seed] if seed else ["codex"]
    raise ValueError(f"unknown engine: {desk.engine!r}")


def _as_applescript_string(value: str) -> str:
    """Escape a Python string for embedding as an AppleScript string literal.

    Mirrors the defensive quoting `server/focus.py` uses around the one
    variable it interpolates (the tty): never trust a path to be quote-free.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_applescript(desk: Desk, *, seed: str = "", settings: str = "",
                      boss_address: str = "") -> str:
    """AppleScript that opens a Terminal window at `desk.cwd` and runs the
    desk's command. PURE -- returns text, opens nothing."""
    argv = build_argv(desk, background=False, seed=seed, settings=settings,
                      boss_address=boss_address)
    shell_cmd = " ".join(shlex.quote(arg) for arg in argv)
    full_cmd = f"cd {shlex.quote(desk.cwd)} && {shell_cmd}"
    return (
        'tell application "Terminal"\n'
        f"  do script {_as_applescript_string(full_cmd)}\n"
        "  activate\n"
        "end tell\n"
    )


def _approval_settings(desk: Desk) -> str:
    """The `--settings` file for this hire, or "" if it could not be written.

    Installed here rather than inside `build_argv` so that stays pure, and on
    every spawn rather than once at import, so a node upgrade or a different
    checkout serving the daemon is picked up by the next hire rather than by a
    restart nobody performs.

    Fails open: an approver that could not be installed costs the desk its
    rules, not its existence, and `approval.install` writes the refusal to the
    ledger so an ungoverned hire is visible instead of silent.
    """
    if desk.engine != "claude":
        return ""  # `--settings` is Claude Code's; the others would reject it
    return approval.install().path


def _boss_address(desk: Desk) -> str:
    """How this desk's boss can be reached, or "" when the boss is dark.

    HERE for the same reason `_vouch` is here: there is more than one door that
    starts a session, and this must be right on all of them. It is the impure
    half of `hire.brief` -- the brief stays pure and takes the address as a
    string; finding it is a read of the board the collector publishes each tick.

    A boss with no live session yields "", and the brief then names the boss
    without offering an address. That is the honest answer: an address that
    resolves to nothing is worse than none, because the junior would use it.
    """
    return office.address_for(desk.reports_to or "")


def _vouch(desk: Desk, roster_path: Path | str) -> pretrust.Trust:
    """Mark the workspace trusted, immediately before the window opens.

    HERE, and not in a caller, for the reason `--settings` and `--name` are
    here: there is more than one door that starts a session, and a gate that
    lives in one of them is a gate the next door forgets. It already happened.
    `Surface.interview_agent` vouched; `Surface.start_agent` and
    `app.roster_start` did not -- so an agent that hired a colleague and asked
    the deck to start it got a window stalled on "Is this a project you created
    or one you trust?" behind an API row reading `state: OFFLINE, blocked:
    null`. Measured: `POST /v1/agents/branch-scout/start` answered
    `{"ok": true, "detail": ""}` and `~/.claude.json` had no entry for the
    workspace the deck had allocated seconds earlier.

    Immediately before, never at hire time: the CLI's own re-basing path can
    revert this key from a session that loaded the config first (GH #3117), so
    the window between the write and the spawn has to stay as small as it can.

    Fails open, like `_approval_settings`. A workspace that could not be
    vouched for costs the desk a dialog, not its existence -- but the verdict
    comes back in the return value so the caller can put it on the row, because
    an OFFLINE that is stuck and an OFFLINE that never started look identical.
    """
    if desk.engine == "claude":
        # Desks run in bypassPermissions (server/deskperms.py); a `--bg` start
        # is refused until the disclaimer is accepted. Fails open, like the rest.
        pretrust.accept_bypass()
    return pretrust.pretrust(desk.cwd, roster_path=roster_path,
                             engine=desk.engine)


@dataclass(frozen=True)
class Channel:
    """Which door a desk can be started through, and why that one.

    `reason` is a stable slug and `detail` is the sentence a human acts on. Both
    ride into the refusal when neither door can serve the desk, because
    "background_unsupported" on its own sends someone hunting for a flag that
    was never the problem.
    """

    name: str
    reason: str
    detail: str


def _launchd_manager() -> str:
    """`launchctl managername` -- launchd's own name for this bootstrap domain.

    "Aqua" in a logged-in graphical session; "Background" or "StandardIO" over
    ssh or under a daemon; "" where there is no launchctl at all, which is every
    non-Darwin box. Read, never assumed: this is the only cheap thing on macOS
    that tells a process whether it has a session to open a window in.
    """
    try:
        out = subprocess.run(["launchctl", "managername"], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def choose_channel(*, which=shutil.which, manager=_launchd_manager) -> Channel:
    """Terminal where a GUI terminal genuinely exists, headless where it does
    not. A CAPABILITY read, deliberately not `sys.platform == "darwin"`.

    The platform check is the shortcut that rebuilds the bug one machine over.
    The Linux box has no `osascript` -- that much a platform check would catch --
    but a Mac reached over ssh, or a deck running under a LaunchAgent, has
    `osascript` right there on PATH and still no session to open a window in.
    Both are the same failure, and only one of them is a different platform.

    `which` and `manager` are injected so a test can drive BOTH branches on a
    logged-in Mac without opening a single window.
    """
    if which("osascript") is None:
        return Channel("background", "no_osascript",
                       "osascript is not on PATH: there is no Terminal.app to "
                       "open a window in here")
    seen = manager()
    if seen != GUI_MANAGER:
        return Channel("background", "no_gui_session",
                       f"launchd reports this session as {seen or 'unknown'!r}, "
                       f"not {GUI_MANAGER!r}: no window can be opened from here")
    return Channel("terminal", "gui_session",
                   f"osascript is on PATH and launchd reports a {GUI_MANAGER} session")


# `claude --bg` prints a banner, not a bare id. MEASURED on this Mac against
# 2.1.258: five lines, of which the first is `backgrounded · <id> · <name>` and
# the rest are the `attach`/`logs`/`stop` hints. `stdout.strip()` is therefore
# the whole banner -- an "agent id" that addresses nothing, which is what
# `spawn_background` returned for as long as nothing called it.
_AGENT_ID = re.compile(r"^backgrounded\s+\W\s+(\S+)", re.MULTILINE)
_ATTACH_ID = re.compile(r"^\s*claude attach\s+(\S+)", re.MULTILINE)


def _agent_id(stdout: str) -> str:
    """The id `claude attach`, `logs`, `stop` and `rm` take, or "" if the CLI
    printed something this cannot read. Two anchors, because a banner is a
    human-facing surface and it will be reworded."""
    for pattern in (_AGENT_ID, _ATTACH_ID):
        found = pattern.search(stdout)
        if found:
            return found.group(1)
    return ""


def spawn_terminal(desk: Desk, *, roster_path: Path | str,
                   seed: str = "") -> dict:
    """Open a Terminal window running `desk`.
    {"ok": bool, "detail": str, "pretrust": {...}}. Raises SpawnError on
    refusal. `seed` is the session's opening prompt.

    `roster_path` has no default on purpose. It is what `pretrust` derives the
    deck's own `workspaces/` and its ledger from, and a fourth caller added
    later either supplies it or does not run -- which is the whole point of
    moving the gate in here.
    """
    if shutil.which("osascript") is None:
        # Not `osascript_failed`. That slug says the window manager refused,
        # and it would be a lie on a box where there is no window manager --
        # sending whoever reads it to look at AppleScript instead of at the
        # channel. `start` never lands here; a direct caller can.
        raise SpawnError("osascript_missing",
                         "osascript is not on PATH: this machine cannot open a "
                         "Terminal window. Use spawn.start, which picks the "
                         "headless channel here.")
    vouched = _vouch(desk, roster_path)
    script = build_applescript(desk, seed=seed,
                               settings=_approval_settings(desk),
                               boss_address=_boss_address(desk))
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SpawnError("osascript_failed", str(exc)) from exc
    if out.returncode != 0:
        raise SpawnError("osascript_failed", (out.stderr or "osascript failed").strip())
    rules.mark_seen(desk.name)  # started on today's brief: nothing to catch up on
    return {"ok": True, "detail": out.stdout.strip(), "pretrust": _verdict(vouched)}


def _verdict(vouched: pretrust.Trust) -> dict:
    """The trust outcome, in the shape the interview door already published.

    `muted` is always present, even empty: a client made to tell "denied
    nothing" apart from "field absent" gets it wrong exactly once.
    """
    return {"ok": vouched.ok, "reason": vouched.reason,
            "detail": vouched.detail, "muted": list(vouched.muted)}


def spawn_background(desk: Desk, *, roster_path: Path | str,
                     seed: str = "") -> dict:
    """Start `desk` as a headless `claude --bg` agent.
    {"ok": bool, "agent_id": str, "pretrust": {...}}. Raises SpawnError on
    refusal.

    Gated exactly as `spawn_terminal` is. A background agent has no window to
    show a dialog in, which makes an untrusted or ungoverned workspace *harder*
    to notice here, not easier.

    `seed` is the session's opening prompt and it goes through, which is the
    whole point of this path on a box with no Terminal: the seed IS the job --
    the hire brief and the interview question. This function used not to accept
    one, so a headless desk started knowing nothing. MEASURED on this Mac
    against claude 2.1.258: `claude --bg <seed>` reads the trailing positional
    as the opening prompt exactly as an interactive session does, and a `--bg`
    with no prompt announces itself as `(idle - send a prompt to start)`.
    """
    if desk.engine != "claude":
        raise SpawnError("background_unsupported", f"{desk.engine} has no --bg")
    vouched = _vouch(desk, roster_path)
    argv = build_argv(desk, background=True, seed=seed,
                      settings=_approval_settings(desk),
                      boss_address=_boss_address(desk))
    try:
        out = subprocess.run(
            argv,
            cwd=desk.cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SpawnError("spawn_failed", str(exc)) from exc
    if out.returncode != 0:
        raise SpawnError("spawn_failed", (out.stderr or "claude --bg failed").strip())
    agent_id = _agent_id(out.stdout)
    if not agent_id:
        raise SpawnError(
            "no_agent_id",
            "claude --bg printed no id this could read: "
            f"{out.stdout.strip()[:200]!r}")
    rules.mark_seen(desk.name)  # started on today's brief: nothing to catch up on
    return {"ok": True, "agent_id": agent_id, "pretrust": _verdict(vouched)}


#: What a CLI that cannot log in says, in any of the wordings measured or
#: plausible. A wake that fails on this is REFUSED, not restarted: a new
#: session cannot log in either, and a restart would only add a replay and a
#: "this desk was restarted" line to a desk that is still dead.
_LOGIN_REFUSED = re.compile(
    r"/login|not logged in|oauth|authenticat|invalid api key|\b401\b", re.I)


def resume_background(session_id: str, *, cwd: str, seed: str) -> str:
    """Wake a stopped or idle-retired background session AS ITSELF.
    Returns the job id the CLI printed. Raises SpawnError on refusal.

    NO FLAGS BUT THE SEED, and that is the measurement, not a simplification.
    MEASURED on the box, claude 2.1.285, throwaway desk `wake-probe`:

        claude --bg --resume <sid> --name ... --settings ... <note>
        -> "background session de8b1457 keeps its own saved options, so the
            flags you passed started a copy as b3f54a1c"
        claude --bg --resume <sid> <note>
        -> "woke session de8b1457 with its saved options (--name, --settings,
            --mcp-config, --append-system-prompt, --model, --permission-mode)"

    The job already carries every option `build_argv` would add; passing them
    again is exactly what turns a wake into a fork with a new session id.

    `oauth_expired` and `resume_failed` are the two refusals: the first means
    no session can start here at all, the second means THIS one cannot be
    resumed and a fresh start may still work -- `server/wake.py` decides.
    """
    # Vouch here too: a woken desk opens a session like any start does, and
    # this door never wrote a trust key (the acme-lead dead-end, 2026-09-30).
    # Fails open, like `_vouch`.
    # A cwd that is not there has nothing to trust and cannot be resumed in.
    if Path(cwd).is_dir():
        from .roster import DEFAULT_PATH as roster_path
        pretrust.pretrust(cwd, roster_path=roster_path)
    argv = ["claude", "--bg", "--resume", session_id, seed]
    try:
        out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                             timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SpawnError("resume_failed", str(exc)) from exc
    said = f"{out.stdout}\n{out.stderr}".strip()
    if out.returncode != 0:
        reason = "oauth_expired" if _LOGIN_REFUSED.search(said) else "resume_failed"
        raise SpawnError(reason, said[:300] or "claude --bg --resume failed")
    agent_id = _agent_id(out.stdout)
    if not agent_id:
        raise SpawnError("resume_failed",
                         f"claude --bg --resume printed no id: {said[:200]!r}")
    return agent_id


#: Where the CLI keeps one directory per BACKGROUND JOB, `<job>/state.json`.
#: Not the same thing as `~/.claude/sessions/<pid>.json`, and the difference is
#: the whole reason a desk could not be retired: the session file describes a
#: PROCESS, the job file describes something that OUTLIVES the process.
JOBS_DIR = CLAUDE_HOME / "jobs"

#: How long to wait for the CLI to admit the session is gone.
RETIRE_TIMEOUT_SECONDS = 15.0


def jobs_for(name: str) -> list[str]:
    """The CLI job ids behind the live sessions at desk `name`. Newest last.

    THE INDIRECTION THAT MAKES RETIREMENT POSSIBLE. The deck knows a desk by
    name and a session by its uuid; `claude stop` accepts neither. It takes the
    job's `daemonShort`. MEASURED on the box:

        claude stop 2326c35c-6953-4875-8f5a-e87b0c1500f4
        -> No job matching '2326c35c-6953-4875-8f5a-e87b0c1500f4'.
        claude stop 2326c35c
        -> stopped 2326c35c

    So the id is READ out of the registry rather than sliced off the session
    uuid. They happen to share a prefix today; `daemonShort` is the field that
    is actually promised, and a retirement that passes the wrong id reports
    success and retires nothing -- the defect wearing a fix's clothes.

    Never raises. A registry that cannot be read costs the retirement, not the
    start: a desk with no session at all is the common case, and it must not be
    made unstartable by a missing directory.
    """
    seated = office.live_session_ids(name)
    if not seated:
        return []
    try:
        entries = sorted(JOBS_DIR.iterdir())
    except OSError:
        return []
    found: list[str] = []
    for entry in entries:
        try:
            state = json.loads((entry / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        # Matched on the SESSION, not on the desk name and not on the job's own
        # `state` field. MEASURED on the box: `state.json` carries no pid, and
        # its `state` is the AGENT's disposition -- `blocked`, `done`,
        # `failed` -- which says nothing about whether a process is sitting
        # there. `~/.claude/jobs/` holds every job this machine has ever run,
        # so matching on the name alone would stop eleven dead jobs to retire
        # one live session. The board knows which sessions are actually up;
        # this only has to turn those into the ids `claude stop` accepts.
        if str(state.get("sessionId") or "") not in seated:
            continue
        job = str(state.get("daemonShort") or entry.name)
        if job:
            found.append(job)
    return found


def jobs_of_desk(name: str) -> list[str]:
    """Every CLI job that could still be running at desk `name`, for firing it.

    `jobs_for` finds live sessions only. A fired desk may also hold a job the
    board no longer shows (asleep, or between ticks), and that job would be
    respawned by the daemon or resumed later. So: the live ones, plus every job
    in the registry filed under this exact desk name that is not already
    `stopped`. Exact name match only -- never another desk's job. Never raises.
    """
    found = list(jobs_for(name))
    try:
        entries = sorted(JOBS_DIR.iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            state = json.loads((entry / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict) or str(state.get("name") or "") != name:
            continue
        if str(state.get("state") or "") == "stopped":
            continue
        job = str(state.get("daemonShort") or entry.name)
        if job and job not in found:
            found.append(job)
    return found


def stop_job(job_id: str) -> bool:
    """Retire ONE background job, for good. The single impure call.

    `claude stop`, not `kill`. This is the measurement that decided the design.
    `~/.claude/jobs/<id>/state.json` carries `respawnFlags` -- including the
    frozen `--append-system-prompt` -- and the CLI's own daemon respawns the
    job from it when the process dies. MEASURED on the box:

        kill -TERM 2568438
        -> process, socket and session file gone within 4 seconds
        -> and the daemon had it back under pid 2665177, same session id,
           same name, same stale brief, before the next tick

    So killing the process is not a retirement; it is a restart with a new pid.
    `claude stop <job>` sets the job's state to `stopped`, which is what makes
    the daemon leave it alone -- MEASURED to stay gone.

    Reversible on purpose. `stop` keeps the conversation and `claude attach
    <id>` resumes it; `claude rm` would delete the transcript, which is the
    desk's whole memory and not ours to destroy to win an argument about
    duplicates.

    A seam, so a test can drive the retirement without stopping the owner's
    real agents -- the suite's guard refuses to let a test run `claude`, and it
    is right to.
    """
    try:
        out = subprocess.run(["claude", "stop", job_id], capture_output=True,
                             text=True, timeout=RETIRE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def retire(name: str) -> list[str]:
    """Empty desk `name` of whatever is sitting at it. Returns the jobs stopped.

    HERE, beside `_vouch` and `_approval_settings` and for the same reason:
    four doors can seat a session -- `POST /v1/agents/{name}/start`,
    `POST /v1/agents/interview`, `POST /api/roster/{name}/start` and the
    harvester's `seat` callback -- and a rule written in one of them is a rule
    the next one forgets. That has already happened twice on this path.

    Fails open, like every other gate in this module. A retirement that could
    not run costs the desk a duplicate, not its existence -- and the caller
    gets the list back, so "nothing was retired" is visible rather than
    assumed.
    """
    stopped = []
    for job in jobs_for(name):
        if stop_job(job):
            stopped.append(job)
    return stopped


#: Opens the replayed conversation, and the token a test can pin. The words
#: "already been told" are the load-bearing half: without them the block reads
#: as a stack of live requests and the desk answers all of it again, which is
#: the same wound one layer along -- he says something once and hears it back
#: twice.
ALREADY_SAID = (
    "--- WHAT THIS DESK HAS ALREADY BEEN TOLD (a record, not a request) ---")

END_OF_SAID = "--- end of what this desk has already been told ---"

#: What the new session is told about its own arrival. It is handed the
#: conversation as history and given ONE live instruction: finish his last one
#: if nothing answered it. A desk that treats a replay as a fresh queue starts
#: the work over; a desk that treats it as unreadable archive leaves his last
#: question hanging, which is what a restart looked like from his side.
RESEAT_LEAD = (
    f"{office.MARK} THIS DESK WAS RESTARTED AND YOU ARE THE NEW SESSION AT IT. "
    "The desk keeps its name, its thread and its owner across a restart; the "
    "session does not. Below is the conversation this desk has already had "
    f"with {owner.name()}, oldest first, replayed by Agent Deck from its own records -- "
    "it is not a message anyone just sent you. Take every word of it as ALREADY "
    "SAID: do not greet him again, do not ask him anything he has already "
    "answered there, and do not redo work the record shows was done. If the "
    "last thing in it is his and nothing answered it, that is your live "
    "instruction -- carry it out. Otherwise say nothing and wait for him. What "
    "you do NOT have is the previous session's own working memory: what it had "
    "open and what it was part-way through did not survive, so if the record "
    "says work was in flight, check the state of it before claiming anything "
    "about it. The desk's own replies in it are an older, longer register: "
    "take their facts, not their length -- answer the way \"How you sound\" "
    "says.")

#: The line his own thread carries. `RESTART_MARK` is the stable token -- one
#: string, so the test and the message cannot drift apart.
RESTART_MARK = f"{office.MARK} This desk was restarted."

RESTART_REPLAYED = (
    "The session you were talking to is gone and a new one is sitting here. It "
    "has been given this conversation, so you do not have to say any of it "
    "again. What it does not have is the previous session's own working "
    "memory -- what it had open, what it was part-way through.")

#: The other case, and it must not claim the replay. A desk can be re-seated
#: with nothing in its thread -- started, never spoken to, restarted -- and
#: telling him it was handed a conversation that does not exist is a claim he
#: can check and find false, on the one message whose whole job is to be
#: believed.
RESTART_BARE = (
    "The session you were talking to is gone and a new one is sitting here. "
    "There was nothing in this conversation to give it, so it starts from its "
    "brief alone.")


def memory_seed(history: str, seed: str = "") -> str:
    """The opening prompt for a REPLACEMENT session. PURE.

    `history` is `office.conversation(desk.name)`; `seed` is whatever the door
    was going to ask for anyway. Empty history returns the seed untouched,
    which is what makes a first hire indistinguishable from before this
    existed.

    The door's own seed stays LAST. It is the job -- the interview question,
    the hire brief -- and a recap that displaced it would trade one silence for
    another. The record goes in front of it so the desk reads the history
    before the instruction, in the order a person would.
    """
    if not history:
        return seed
    block = f"{RESEAT_LEAD}\n\n{ALREADY_SAID}\n\n{history}\n\n{END_OF_SAID}"
    return f"{block}\n\n{seed}" if seed else block


def announce_restart(name: str, *, replayed: bool) -> None:
    """One line in this desk's own conversation saying the brain was replaced.

    HERE for the reason `retire` and `_vouch` are here: four doors seat a desk,
    and a notice written in one of them is a notice the next three do not send.

    IN THE CONVERSATION, not in a log and not in a field. A respawn is same
    desk, same name, same thread, new brain, and until now it left no mark
    anywhere the owner could see -- so from his side he was repeating himself
    to something that kept forgetting, while the board read healthy. That is
    the part that made him feel he was going mad, and a fix that restores the
    memory silently would leave it exactly there.

    Posted `from` the desk to `office.OWNER_INBOX`, the same way `harvest._say`
    puts a desk's prose in his thread, so it lands in that desk's conversation
    and nowhere else. It opens with `office.MARK` because it is the DECK
    talking about the desk rather than the desk talking: the mark is the only
    thing on the queue a sender cannot write for itself.

    Never raises. A restart he was not told about is bad; a start that fails
    because the telling failed is worse.
    """
    body = f"{RESTART_MARK} {RESTART_REPLAYED if replayed else RESTART_BARE}"
    try:
        office.send(office.OWNER_INBOX, body, sender=name,
                    extra={"restarted": True})
    except OSError:
        pass


def start(desk: Desk, *, roster_path: Path | str, seed: str = "",
          channel: Channel | None = None) -> dict:
    """THE way to put a process at a desk. One entry point, two channels,
    one result shape: {"ok", "channel", "detail", "agent_id", "pretrust"}.

    Every key is present on both channels, empty where it does not apply, for
    the reason `_verdict` always publishes `muted`: a client made to tell
    "nothing to report" apart from "field absent" gets it wrong exactly once --
    and here it would get it wrong on exactly one of the two installs, which is
    the hardest kind of bug to see.

    Why this exists. `server/api.py::Surface._start` and
    `server/app.py::roster_start` are the only two doors that start a session,
    and BOTH called `spawn_terminal`, which runs `osascript`. The second install
    of this deck -- Linux, systemd, authenticated, answering 200 -- has
    therefore hired nobody, ever: every hire on it reaches the last step and
    dies there. Choosing the channel inside a caller is what put the choice in
    two places; it lives here so a third door added next month cannot get it
    wrong, exactly as `_vouch` and `_approval_settings` already do.
    """
    # ONE DESK, ONE LIVE SESSION -- before anything is seated, and before the
    # channel is even chosen, because a desk that already has somebody at it is
    # occupied on both channels.
    #
    # RETIRE RATHER THAN REFUSE, and the reason is `respawnFlags`. A session's
    # brief is frozen at spawn, and the CLI's daemon respawns the job from that
    # frozen brief forever. If `start` refused while a session was seated, a
    # desk whose brief is wrong could never be given the right one through the
    # deck at all: the incumbent is immortal, and the only remaining fix is the
    # manual kill that the daemon undoes. Retire-then-seat is the only order in
    # which "we fixed the brief" is something the owner can actually receive.
    # Refusing is the safer-sounding option and it is the one that would have
    # left him exactly where he was tonight.
    retired = retire(desk.name)

    # AND THE REPLACEMENT ARRIVES KNOWING THE CONVERSATION. Retiring the
    # incumbent was only half of it: MEASURED on the box, desk
    # `new-hire-82d9ab` was seated with four sessions in a row as fixes were
    # deployed, and each one started blank. The owner had typed his mandate
    # into that desk repeatedly -- the thread has every word of it -- and got
    # back an agent that had read none, with nothing anywhere saying why.
    #
    # Two facts, and either one alone makes this a re-seat. `retired` is a
    # session we just stopped; `history` is a conversation a previous brain
    # held, which outlives it -- and it is the one that catches the case the
    # owner actually hit, because a deploy restarts the daemon and the
    # incumbent is already dead by the time this runs. A desk with neither is
    # genuinely new: it gets no replay and he is told of no restart, because
    # announcing one on every first hire would make the notice worth nothing.
    history = office.conversation(desk.name)
    opening = memory_seed(history, seed)

    picked = channel or choose_channel()
    if picked.name == "terminal":
        opened = spawn_terminal(desk, roster_path=roster_path, seed=opening)
        result = {"ok": True, "channel": "terminal", "detail": opened["detail"],
                  "agent_id": "", "pretrust": opened["pretrust"]}
    elif desk.engine != "claude":
        # Both halves of why, because either alone misleads. On the box this
        # reads: opencode has no --bg, AND there is no window to run it in.
        raise SpawnError(
            "no_channel",
            f"{desk.engine} has no --bg, and {picked.detail}")
    else:
        started = spawn_background(desk, roster_path=roster_path, seed=opening)
        result = {"ok": True, "channel": "background",
                  "detail": f"started headless ({picked.reason})",
                  "agent_id": started["agent_id"],
                  "pretrust": started["pretrust"]}

    # AFTER the seat, never before. A `spawn_*` that raises leaves the desk
    # empty, and "this desk was restarted" in his thread with nobody sitting
    # there is a second false claim on top of the first.
    #
    # NOT reported in the return value, on purpose. The result shape is a
    # published contract -- `tests/test_spawn_channel.py` pins that both
    # channels answer with exactly the same five keys -- and the person who has
    # to learn about the restart is the OWNER, in his thread, not a caller in
    # a JSON field no client reads. A `replaced: true` on the response would
    # have been the cheap half of this fix and the useless one.
    if retired or history:
        announce_restart(desk.name, replayed=bool(history))
    return result
