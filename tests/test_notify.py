"""The sender. Never claims sent on a non-zero exit.

Every test here runs a stub script, never the real bridge. `node` is not
required: the interpreter is an env var too, so these run against /bin/sh.
No WhatsApp message is sent at any point.
"""

import os
import stat

import pytest

from server import notify


def stub(tmp_path, body):
    script = tmp_path / "wa-send-stub.sh"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def env_for(script, **extra):
    env = {notify.ENV_SCRIPT: str(script), notify.ENV_NODE: "/bin/sh"}
    env.update(extra)
    return env


def test_the_default_script_is_the_bridge_and_the_env_var_overrides_it(tmp_path):
    assert notify.script_path({}).name == "wa-send.js"
    assert "comunicate_with_me" in str(notify.script_path({}))
    assert notify.script_path({notify.ENV_SCRIPT: "/x/y.js"}) == \
        __import__("pathlib").Path("/x/y.js")


def test_a_clean_exit_sends_the_text_through_dash_dash_file(tmp_path):
    """The good signal: the exact bytes reach the script's --file argument."""
    seen = tmp_path / "seen.txt"
    script = stub(tmp_path, f'''
[ "$1" = "--file" ] || exit 9
cat "$2" > {seen}
exit 0
''')
    result = notify.send("acme-growth wants to run gh pr create",
                         env=env_for(script))

    assert result.ok is True
    assert result.code == 0
    assert seen.read_text() == "acme-growth wants to run gh pr create"


def test_a_non_zero_exit_is_never_reported_as_sent(tmp_path):
    """wa-send.js exits 3 when the daemon is down. Reporting that as sent is
    how a question silently never reaches the phone."""
    script = stub(tmp_path, 'echo "daemon is not answering" >&2\nexit 3\n')
    result = notify.send("anything", env=env_for(script))

    assert result.ok is False
    assert result.code == 3
    assert "daemon" in result.detail.lower()


def test_an_unlinked_bridge_is_not_sent_either(tmp_path):
    script = stub(tmp_path, "exit 4\n")
    result = notify.send("anything", env=env_for(script))
    assert result.ok is False
    assert result.code == 4


def test_a_missing_script_is_a_failure_not_an_exception(tmp_path):
    result = notify.send("anything",
                         env=env_for(tmp_path / "does-not-exist.sh"))
    assert result.ok is False
    assert result.code is None


def test_a_hung_script_does_not_hang_the_caller(tmp_path):
    script = stub(tmp_path, "sleep 30\n")
    result = notify.send("anything", env=env_for(script), timeout=0.5)
    assert result.ok is False
    assert "timed out" in result.detail.lower()


def test_empty_text_is_refused_without_running_anything(tmp_path):
    ran = tmp_path / "ran"
    script = stub(tmp_path, f"touch {ran}\nexit 0\n")
    result = notify.send("   ", env=env_for(script))
    assert result.ok is False
    assert not ran.exists()


def test_the_temp_file_does_not_outlive_the_send(tmp_path):
    """The message can name a folder and a command; it does not get to linger
    in /tmp afterwards."""
    kept = tmp_path / "path.txt"
    script = stub(tmp_path, f'printf "%s" "$2" > {kept}\nexit 0\n')
    assert notify.send("hello", env=env_for(script)).ok is True
    assert not os.path.exists(kept.read_text().strip())


def test_send_does_not_read_the_real_environment_when_given_one(tmp_path):
    """Hermetic by construction: an explicit env is the whole env consulted."""
    script = stub(tmp_path, "exit 0\n")
    assert notify.send("hi", env=env_for(script)).ok is True


# ── the return address ───────────────────────────────────────────────────────
#
# MEASURED by reading ~/Projects/comunicate_with_me/bin/wa-send.js
# (`buildSession`): the bridge records who to route a reply back to from
# `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_SESSION_ID` **in the env of
# the process that runs wa-send.js**. The deck daemon is not a Claude session,
# so those variables are absent from it, and `src/router.js` answers a reply to
# an unrecorded send with "that message came from a shell, not a session".
#
# Without this the deck can page the phone perfectly and no answer can ever
# come back -- and the failure is silent on both ends.


def test_a_reply_address_reaches_the_child_as_the_variables_the_bridge_reads(tmp_path):
    """The good signal: the exact variable names wa-send.js#buildSession reads."""
    seen = tmp_path / "env.txt"
    script = stub(tmp_path, f'''
printf "SOCK=%s\\n" "$CLAUDE_CODE_MESSAGING_SOCKET" > {seen}
printf "SID=%s\\n" "$CLAUDE_CODE_SESSION_ID" >> {seen}
exit 0
''')
    result = notify.send(
        "acme-growth is blocked",
        env=env_for(script),
        reply_to={"socket": "/tmp/cc-socks/agentdeck.sock",
                  "session_id": "agent-deck"},
    )

    assert result.ok is True
    assert seen.read_text() == ("SOCK=/tmp/cc-socks/agentdeck.sock\n"
                                "SID=agent-deck\n")


def test_without_a_reply_address_the_variables_are_not_invented(tmp_path):
    """A plain notification stays routable-as-unroutable, never a guess at
    some other session's socket."""
    seen = tmp_path / "env.txt"
    script = stub(tmp_path,
                  f'printf "SOCK=[%s]" "$CLAUDE_CODE_MESSAGING_SOCKET" > {seen}\nexit 0\n')
    assert notify.send("fyi", env=env_for(script)).ok is True
    assert seen.read_text() == "SOCK=[]"


def test_a_reply_address_never_leaks_into_this_process(tmp_path):
    """The CHILD is configured, not the daemon: a second send with no address
    must not inherit the first one's."""
    seen = tmp_path / "env.txt"
    script = stub(tmp_path,
                  f'printf "SOCK=[%s]" "$CLAUDE_CODE_MESSAGING_SOCKET" > {seen}\nexit 0\n')
    before = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    notify.send("one", env=env_for(script),
                reply_to={"socket": "/tmp/cc-socks/agentdeck.sock"})
    assert os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET") == before

    notify.send("two", env=env_for(script))
    assert seen.read_text() == "SOCK=[]"
