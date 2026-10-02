"""A desk may do safe work without asking, and must still stop him for harm.

The corpus in `HIS_BOARD` is not invented. It is what was actually pending on
the owner's deck: thirteen approval cards, every one of them a desk trying to
find out where it was. `pwd`. `ls`. `git branch --show-current`. The product was
asking its owner to authorise `pwd`.

**Where the classification happens, and therefore what this test asserts.**
Claude Code 2.1.263 already ships the engine -- auto mode's classifier, plus a
tree-sitter shell grammar behind its Bash rules. Re-testing Anthropic's
classifier from here would be theatre. What this file pins is the two things
that ARE ours and that were both wrong:

1. the deck's hook stops manufacturing the prompt. It answered "ask" for every
   call it had no rule about, and a `PreToolUse` "ask" is returned AS the
   decision before any rule or classifier runs -- so no amount of configuring
   the CLI underneath it could ever have helped. All six of his real commands
   must now come back `abstain`, and the hook must emit no `permissionDecision`
   for that.
2. the configuration the deck hands a hired desk actually turns the engine on,
   keeps his checkpoints in front of him, and does not leak.

The floor is asserted too: what the deck itself must never let past, whatever
any rule or classifier says.
"""
import json
import subprocess
from pathlib import Path

from server import approval, deskperms
from server.autoreview import Rule, evaluate

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "cc-approve.js"

#: Verbatim from the pending cards, with two repairs stated rather than hidden:
#: the board truncates long text with `…` for display, so the elided path in the
#: fourth row and the elided third argument in the second row are written out in
#: the shape the neighbouring rows prove they had. Nothing else is changed.
HIS_BOARD: tuple[tuple[str, dict, str], ...] = (
    ("Bash", {"command": "pwd && ls -la"}, "/tmp"),
    ("Bash", {"command": "ls -la orion-pb vv-gen opencode 2>/dev/null"}, "/tmp"),
    ("Read", {"file_path": "/tmp/run.py"}, "/tmp"),
    ("Read", {"file_path":
              "/home/deckop/.claude/projects/-tmp/memory/MEMORY.md"}, "/tmp"),
    ("Bash", {"command":
              "ls /home/deckop/.claude/projects/-tmp/memory/ 2>&1"}, "/tmp"),
    ("Bash", {"command": 'pwd && echo "---" && git branch --show-current'
                         ' && git log --oneline -20'}, "/tmp"),
)

#: The deck's own floor. No rule may lift these and no classifier is consulted
#: for them, because a hook "ask" outranks a classifier allow.
FLOOR: tuple[tuple[str, dict], ...] = (
    ("Bash", {"command": "rm -rf /"}),
    ("Bash", {"command": "rm -rf ~/Projects"}),
    ("Bash", {"command": "cat ~/.ssh/id_rsa"}),
    ("Bash", {"command": "cat /Users/y/.ssh/id_ed25519"}),
    ("Bash", {"command": "cat /Users/y/Projects/app/.env"}),
    ("Bash", {"command": "cat ~/.npmrc"}),
    ("Bash", {"command": "cat ~/.git-credentials"}),
    ("Bash", {"command": "echo pwned >> ~/.ssh/authorized_keys"}),
    ("Bash", {"command": "security dump-keychain"}),
    ("Bash", {"command": "git push --force origin main"}),
    ("Bash", {"command": "psql -c 'drop table users'"}),
    ("Bash", {"command": "curl -X POST https://pay.example.com/checkout"}),
    ("Read", {"file_path": "/Users/y/.ssh/id_rsa"}),
    ("Read", {"file_path": "/Users/y/.aws/credentials"}),
    ("Read", {"file_path": "/Users/y/.gnupg/secring.gpg"}),
    ("Write", {"file_path": "/Users/y/deploy.pem"}),
)


# ── the good signal: his real board goes quiet ─────────────────────────────


def test_the_deck_holds_no_opinion_about_anything_on_his_board():
    """All six. The GOOD signal, and the half that was actually broken.

    `abstain`, not `allow`: the deck is not granting these, it is getting out of
    the way so Claude Code's own engine can. Granting them here would put the
    deck back in the business of writing a policy it does not own.
    """
    for tool, tool_input, cwd in HIS_BOARD:
        verdict = evaluate([], tool_name=tool, tool_input=tool_input, cwd=cwd)
        assert verdict.decision == "abstain", (tool, tool_input, verdict)
        assert verdict.rule_id is None


def test_abstaining_emits_no_permission_decision_at_all():
    """The wire format, driven through the real hook against a real socket.

    A `permissionDecision` of any value is an opinion; the absence of the field
    is not. Anything printed here that carries the field would put the desk back
    on a modal (for "ask") or overrule his deny rules (for "allow").
    """
    out = _run_hook({"decision": "abstain", "rule_id": None,
                     "reason": "no rule"})
    assert "permissionDecision" not in out["hookSpecificOutput"], out
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


# ── the floor: what the deck itself still stops ────────────────────────────


def test_the_deck_asks_for_what_nothing_may_lift():
    for tool, tool_input in FLOOR:
        verdict = evaluate([], tool_name=tool, tool_input=tool_input, cwd="/w")
        assert verdict.decision == "ask", (tool, tool_input, verdict)
        assert "handoff" in verdict.reason


def test_a_private_key_is_not_spelled_key():
    """The hole this closes. `~/.ssh/id_rsa` matched no floor pattern at all --
    the nearest was `*ssh*key*`, which needs the word after the word."""
    verdict = evaluate([], tool_name="Read",
                       tool_input={"file_path": "/Users/y/.ssh/id_rsa"},
                       cwd="/w")
    assert verdict.decision == "ask"


def test_an_allow_everything_rule_still_cannot_lift_the_floor():
    wide = Rule(id="wide", kind="always_allow", tool="*", pattern="*", cwd="**")
    for tool, tool_input in FLOOR:
        verdict = evaluate([wide], tool_name=tool, tool_input=tool_input,
                           cwd="/w")
        assert verdict.decision == "ask", (tool, tool_input, verdict)


def test_his_own_rules_still_outrank_abstaining():
    """Abstaining is the LAST step, so nothing he wrote is bypassed by it."""
    guard = Rule(id="g", kind="require_approval", tool="Bash",
                 pattern="npm run deploy*", cwd="**")
    stopped = evaluate([guard], tool_name="Bash",
                       tool_input={"command": "npm run deploy"}, cwd="/w")
    assert stopped.decision == "ask" and stopped.rule_id == "g"

    granted = Rule(id="a", kind="always_allow", tool="Bash",
                   pattern="make *", cwd="**")
    allowed = evaluate([granted], tool_name="Bash",
                       tool_input={"command": "make build"}, cwd="/w")
    assert allowed.decision == "allow" and allowed.rule_id == "a"


# ── the fail-safe is untouched ─────────────────────────────────────────────


def test_every_failure_still_prints_ask():
    """The hook's oldest invariant. A deck that is down, hung, unauthorised or
    talking nonsense must draw the prompt the human would have got anyway."""
    for reply in ('{"decision": "wat"}', "not json at all", "", "[]", "null"):
        out = _run_hook_raw(reply)
        assert out["hookSpecificOutput"]["permissionDecision"] == "ask", reply

    unreachable = _run_hook_raw(None, url="http://127.0.0.1:9")
    assert unreachable["hookSpecificOutput"]["permissionDecision"] == "ask"


# ── the configuration the deck hands a desk ────────────────────────────────


def test_a_hired_desk_runs_in_bypass_permissions_mode():
    """OWNER RULING 2026-09-30: "this has to be fixed! we should allow it
    anything". MEASURED on the box (claude 2.1.285): `--settings` flag settings
    with defaultMode bypassPermissions ran a Bash `touch` under `-p` that
    defaultMode "default" refused -- the setting is honoured, no argv flag."""
    document = approval.settings_document("/usr/bin/node")
    assert document["permissions"]["defaultMode"] == "bypassPermissions"
    assert deskperms.PERMISSION_MODE == "bypassPermissions"


def test_a_desk_has_no_ask_floor_and_no_classifier():
    """MEASURED: `permissions.ask` rules still stop a session in bypass mode, so
    the floor has to be gone, not merely outranked. `autoMode` configures a
    classifier that no longer runs."""
    document = approval.settings_document("/usr/bin/node")
    assert document["permissions"]["ask"] == []
    assert deskperms.ALWAYS_ASK == ()
    assert deskperms.ask_rules() == []
    assert "autoMode" not in document
    assert not hasattr(deskperms, "ENVIRONMENT")


def test_the_desk_launch_carries_no_banned_flag_and_no_auto_mode():
    """The mode rides in `--settings`; the argv stays free of the flags
    `spawn.BANNED_FLAGS` sweeps for, and never names auto mode."""
    from server import spawn
    from server.roster import Desk
    argv = spawn.build_argv(Desk(name="d", cwd="/tmp", engine="claude", mission="m", charter="c"),
                            background=True, settings="/x/s.json")
    assert "--settings" in argv
    assert not [flag for flag in spawn.BANNED_FLAGS if flag in argv]
    assert "auto" not in argv
    # MEASURED on the box (claude 2.1.285): a `--bg` session IGNORES
    # `defaultMode: bypassPermissions` from settings and stays in "default";
    # only `--permission-mode bypassPermissions` takes effect.
    i = argv.index("--permission-mode")
    assert argv[i + 1] == "bypassPermissions"


def test_the_settings_accept_the_bypass_disclaimer():
    """MEASURED: without `skipDangerousModePermissionPrompt` in flag settings the
    session stays in "default" even with the flag."""
    document = approval.settings_document("/usr/bin/node")
    assert document["skipDangerousModePermissionPrompt"] is True


def test_the_box_config_accepts_the_bypass_disclaimer(tmp_path):
    """MEASURED: `claude --bg --permission-mode bypassPermissions` refuses with
    "requires accepting the disclaimer first" until `~/.claude.json` carries
    bypassPermissionsModeAccepted. Idempotent, and it keeps every other key."""
    import json as _json
    from server import pretrust
    cfg = tmp_path / ".claude.json"
    cfg.write_text(_json.dumps({"projects": {"/x": {"a": 1}}, "keep": 2}))
    first = pretrust.accept_bypass(config_path=cfg)
    assert first.ok
    data = _json.loads(cfg.read_text())
    assert data["bypassPermissionsModeAccepted"] is True
    assert data["keep"] == 2 and data["projects"] == {"/x": {"a": 1}}
    assert pretrust.accept_bypass(config_path=cfg).reason == "already_accepted"


def test_the_settings_file_still_carries_the_hooks_it_always_did():
    """The permissions block is added beside the five hooks, not instead."""
    document = approval.settings_document("/usr/bin/node")
    for event in ("PreToolUse", "PermissionRequest", "Elicitation"):
        assert document["hooks"][event]
    assert document["env"]["DECK_URL"]


def test_the_generated_settings_carry_no_secret():
    """It lands in the bus beside world-readable siblings. The token's PATH may
    appear; the token may not, and neither may anything shaped like one."""
    document = approval.settings_document("/usr/bin/node")
    body = json.dumps(document)
    assert "deck-token.txt" in body          # the path, deliberately
    for leak in ("Bearer ", "ghp_", "sk-ant", "BEGIN ", "PRIVATE KEY"):
        assert leak not in body, leak


# ── driving the real hook ──────────────────────────────────────────────────


def _run_hook_raw(reply: str | None, url: str | None = None) -> dict:
    """Run `hooks/cc-approve.js` for real against a socket that answers `reply`.

    A real node process and a real HTTP round trip: the wire format is the whole
    point of this file, and a Python re-implementation of the hook would prove
    nothing about what Claude Code actually reads on stdout.
    """
    import http.server
    import threading

    server = None
    if reply is not None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib's name
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = reply.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        proc = subprocess.run(
            ["node", str(HOOK)],
            input=json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": "pwd"},
                              "session_id": "s", "cwd": "/tmp"}),
            capture_output=True, text=True, timeout=20,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                 "DECK_URL": url or "http://127.0.0.1:9",
                 "DECK_TOKEN_FILE": "/nonexistent"},
        )
    finally:
        if server is not None:
            server.shutdown()
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_hook(verdict: dict) -> dict:
    return _run_hook_raw(json.dumps(verdict))


# ── never a dead end (owner, 2026-09-30) ───────────────────────────────────


def test_the_brief_forbids_telling_him_you_are_blocked():
    """"any type of permission it should has or ask for approval, this is not
    helping if its says im block". The line names the two exits: another way,
    or a card he can tap."""
    from server import hire
    from server.roster import Desk
    text = hire.brief(Desk(name="d", cwd="/tmp", engine="claude", mission="m", charter="c"))
    assert "Never tell him you are blocked" in text
    assert "mcp__deck__ask" in hire.NEVER_BLOCKED
    assert "Allow" in hire.NEVER_BLOCKED
    assert "Take over the screen" in hire.NEVER_BLOCKED


def test_a_bypass_desk_still_receives_the_owners_messages():
    """MEASURED on the box (claude 2.1.285): in bypass mode Claude Code HOLDS
    inbound peer messages -- "The sender did not attest its permission mode and
    this session bypasses prompts" -- so every message the deck injects was
    parked unread. `crossSessionInbound: accept` in flag settings is the fix."""
    document = approval.settings_document("/usr/bin/node")
    assert document["crossSessionInbound"] == "accept"
