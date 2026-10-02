"""The phone loop: he is paged when he is needed, and his answer gets back.

Two halves, one file, because they only mean anything together.

**Outbound.** `server/notify.py` has existed, complete and correct, with ZERO
callers -- measured with `grep -rn notify server/`. `asking.compose` had zero
production callers too. So the deck recorded every question perfectly and the
phone never rang once.

**Inbound.** Measured by reading `~/Projects/comunicate_with_me`: the bridge has
a full inbound pipeline (`src/inbound.js` -> `src/router.js` -> `src/inject.js`)
that delivers a reply into a Claude Code session's UNIX socket under
`/tmp/cc-socks`. It has no HTTP webhook and cannot POST anywhere -- grepped for
`fetch(`/`webhook` across `src/`, `bin/` and `hooks/`; the only hit is the CLI
calling the daemon. So the ONE way into the deck is for the deck to own a socket
in that directory and be routed to like a session.

No test here sends a real message, binds a real Claude socket, or runs node.
"""

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from server import asking, phone


@pytest.fixture
def asks(tmp_path):
    return tmp_path / "asks.json"


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "paged.json"


@pytest.fixture
def sock_dir():
    """A SHORT directory. AF_UNIX paths are capped at 104 bytes on macOS and
    108 on Linux, and pytest's tmp_path is already ~110 -- bind() fails
    EADDRINUSE-adjacent with "path too long" and `Listener.start()` reports
    False. That is the listener behaving correctly; the fixture is what makes
    the test able to see it."""
    import shutil
    import tempfile
    path = Path(tempfile.mkdtemp(prefix="/tmp/dphone-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_bleed():
    """The paged mirror is process-global. Clearing it between tests keeps each
    one honest about what IT sent."""
    phone._PAGED.clear()
    yield
    phone._PAGED.clear()


#: How long ago the question was raised. Past `notify.quiet_seconds()` on
#: purpose: the app gets first refusal, so a question recorded a second ago is
#: deliberately still on the deck and not on his phone. These tests are about
#: what the page CONTAINS and where it is addressed; the window itself is
#: pinned in `tests/test_the_app_gets_first_refusal.py`.
UNANSWERED_FOR = 3600.0   # past the 30-minute default


def an_ask(asks, *, agent="acme-growth", tool="Bash",
           subject="gh pr create --fill", cwd="/Users/y/Projects/acme",
           ts=None):
    return asking.record(asks, agent=agent, tool=tool, subject=subject, cwd=cwd,
                         ts=time.time() - UNANSWERED_FOR if ts is None
                         else ts)


class Recorder:
    """Stands in for notify.send and remembers exactly what it was handed."""

    def __init__(self, ok=True, code=0, detail="sent"):
        self.calls = []
        self.result = phone.notify.Sent(ok, code, detail)

    def __call__(self, text, *, reply_to=None, **_):
        self.calls.append({"text": text, "reply_to": reply_to})
        return self.result


# ── outbound: he is paged for a question only he can answer ─────────────────


def test_a_recorded_ask_reaches_the_phone_with_its_id(asks, ledger):
    """The good signal: the exact text `asking.compose` produces, carrying the
    id he has to quote back."""
    ask = an_ask(asks)
    send = Recorder()

    result = phone.page_ask(ask, ledger=ledger, send=send,
                            reply_socket="/tmp/cc-socks/agentdeck.sock")

    assert result.ok is True
    assert len(send.calls) == 1
    # Marked urgent: an ask only reaches WhatsApp once it has gone unanswered
    # in the app past the threshold (owner ruling 2026-09-30).
    assert send.calls[0]["text"] == f"{phone.URGENT_MARK} {asking.compose(ask)}"
    assert f"(ask {ask.id})" in send.calls[0]["text"]


def test_the_page_is_phone_shaped(asks, ledger):
    """Under ~8 short lines with the one thing that matters in bold. He reads
    this on a lock screen; a wall of text is the same as no message."""
    ask = an_ask(asks)
    send = Recorder()
    phone.page_ask(ask, ledger=ledger, send=send,
                   reply_socket="/tmp/cc-socks/agentdeck.sock")

    lines = send.calls[0]["text"].splitlines()
    assert len(lines) <= 8
    assert lines[0].startswith(f"{phone.URGENT_MARK} **")
    assert "acme-growth" in lines[0]


def test_a_blocked_ask_is_addressed_to_the_deck_not_to_the_stuck_session(
        asks, ledger):
    """MEASURED and written down in `server/app.py` (the `/api/permission`
    comment): text injected into a session with a permission modal up lands
    UNDERNEATH the modal, unsubmitted. So the answer to a BLOCKED ask must come
    to the deck, which can write the rule and resume the desk -- never to the
    session that is stuck."""
    ask = an_ask(asks)
    send = Recorder()
    phone.page_ask(ask, ledger=ledger, send=send,
                   reply_socket="/tmp/cc-socks/agentdeck.sock")

    assert send.calls[0]["reply_to"] == {
        "socket": "/tmp/cc-socks/agentdeck.sock",
        "session_id": phone.DECK_SESSION_ID,
    }


def test_one_ask_is_paged_once_however_many_times_it_is_offered(asks, ledger):
    """`/api/permission` re-evaluates on every retry of the same tool call, so
    this is asked repeatedly by design. One question, one buzz."""
    ask = an_ask(asks)
    send = Recorder()
    for _ in range(4):
        phone.page_ask(ask, ledger=ledger, send=send,
                       reply_socket="/tmp/cc-socks/agentdeck.sock")
    assert len(send.calls) == 1


def test_the_once_only_guard_survives_a_restart(asks, ledger):
    """In-memory dedup re-pages everything the deck has ever asked the moment
    the daemon restarts -- and the daemon restarts on every deploy."""
    ask = an_ask(asks)
    first = Recorder()
    phone.page_ask(ask, ledger=ledger, send=first,
                   reply_socket="/tmp/cc-socks/agentdeck.sock")

    phone._PAGED.clear()  # a fresh process knows nothing but the file
    second = Recorder()
    phone.page_ask(ask, ledger=ledger, send=second,
                   reply_socket="/tmp/cc-socks/agentdeck.sock")

    assert len(second.calls) == 0


def test_a_send_that_failed_is_not_recorded_as_paged(asks, ledger):
    """The whole point of notify.py's exit-code handling, carried through to
    here. The bridge being down (exit 3) must leave the question OWED, so the
    next offer tries again -- otherwise one dead daemon silently swallows the
    one message he needed."""
    ask = an_ask(asks, ts=0.0)   # raised at the epoch: `now` below is the clock
    down = Recorder(ok=False, code=3, detail="daemon is not running")
    assert phone.page_ask(ask, ledger=ledger, send=down, now=4000.0,
                          reply_socket="/tmp/cc-socks/agentdeck.sock").ok is False

    # Past the retry backoff. The question stays OWED -- that is the property
    # this test defends and it is unchanged -- but it is no longer retried on
    # every 1 Hz tick. Measured on the Linux box, which has no bridge: five
    # pending questions produced 285 send attempts in 60 seconds, and with a
    # working bridge that is his phone once a second. See
    # tests/test_the_phone_does_not_flood.py.
    back_up = Recorder()
    phone.page_ask(ask, ledger=ledger, send=back_up,
                   now=4000.0 + phone.RETRY_AFTER_SECONDS + 1,
                   reply_socket="/tmp/cc-socks/agentdeck.sock")
    assert len(back_up.calls) == 1


def test_with_no_socket_directory_he_is_still_paged(asks, ledger):
    """Degrade the reply path, never the alarm. A machine with no
    /tmp/cc-socks can still tell him he is needed; he just has to answer on
    the board."""
    ask = an_ask(asks)
    send = Recorder()
    result = phone.page_ask(ask, ledger=ledger, send=send, reply_socket=None)

    assert result.ok is True
    assert send.calls[0]["reply_to"] is None


# ── outbound: a run he cares about finished ────────────────────────────────


def test_the_manager_finishing_is_paged_and_answerable_in_place(ledger):
    """The narrow trigger, and the reason it is narrow: per the working
    agreement he talks to ONE session and it runs the rest. That session going
    WORKING -> DONE is a run of his finishing. Every other desk reports to it,
    not to him.

    Its reply address is its OWN socket, unlike a blocked ask: a DONE session
    sits at its composer with no modal over it, which is precisely the case the
    bridge's inject path was built for."""
    send = Recorder()
    card = {"session_id": "S-manager", "name": "cos", "state": "DONE",
            "pid": 4242, "project": "other_app", "git_branch": "develop"}

    result = phone.page_done(card, crown={"session_id": "S-manager"},
                             ledger=ledger, send=send,
                             reply_socket="/tmp/cc-socks/4242.sock")

    assert result.ok is True
    assert send.calls[0]["reply_to"] == {"socket": "/tmp/cc-socks/4242.sock",
                                         "session_id": "S-manager"}
    assert send.calls[0]["text"].splitlines()[0].startswith("**cos**")


def test_a_worker_finishing_does_not_buzz_him(ledger):
    """Sweeping the class, not the instance: on this machine 40+ sessions run
    at once. Paging every DONE is how a phone becomes noise he mutes, and a
    muted phone is the same failure as no phone."""
    send = Recorder()
    for i in range(5):
        phone.page_done({"session_id": f"S-{i}", "name": f"worker-{i}",
                         "state": "DONE", "pid": 100 + i},
                        crown={"session_id": "S-manager"},
                        ledger=ledger, send=send, reply_socket=None)
    assert send.calls == []


def test_a_session_still_working_is_not_announced_as_finished(ledger):
    send = Recorder()
    phone.page_done({"session_id": "S-manager", "name": "cos",
                     "state": "WORKING", "pid": 1},
                    crown={"session_id": "S-manager"},
                    ledger=ledger, send=send, reply_socket=None)
    assert send.calls == []


def test_one_finish_is_one_message_not_one_per_tick(ledger):
    """The collector re-reads state at 1 Hz and a session stays DONE until it
    is spoken to, so an unguarded trigger sends a message every second."""
    send = Recorder()
    card = {"session_id": "S-manager", "name": "cos", "state": "DONE",
            "pid": 4242}
    for _ in range(30):
        phone.page_done(card, crown={"session_id": "S-manager"},
                        ledger=ledger, send=send, reply_socket=None)
    assert len(send.calls) == 1


def test_the_next_finish_after_working_again_is_a_new_message(ledger):
    """The guard is per TRANSITION, not per session, or he hears about a run
    once and never again."""
    send = Recorder()
    crown = {"session_id": "S-manager"}
    done = {"session_id": "S-manager", "name": "cos", "state": "DONE", "pid": 1}
    working = {**done, "state": "WORKING"}

    phone.page_done(done, crown=crown, ledger=ledger, send=send, reply_socket=None)
    phone.page_done(working, crown=crown, ledger=ledger, send=send, reply_socket=None)
    phone.page_done(done, crown=crown, ledger=ledger, send=send, reply_socket=None)

    assert len(send.calls) == 2


# ── inbound: parsing what the bridge writes into our socket ────────────────


def frame(text):
    """The exact bytes `src/inject.js` writes, prefix and all."""
    prefix = ("[via WhatsApp — your reply goes to Sam's phone, "
              "keep it under 10 short lines]\n")
    return json.dumps({"type": "user",
                       "message": {"role": "user", "content": prefix + text}}) + "\n"


def test_the_bridges_injection_frame_is_read_back_as_the_words_he_typed():
    assert phone.parse_frame(frame("xyz12 always")) == "xyz12 always"


def test_a_frame_without_the_prefix_still_parses():
    """The prefix is the bridge's, and the bridge may change it. Depending on
    it to strip is fine; depending on it to be PRESENT is not."""
    raw = json.dumps({"type": "user",
                      "message": {"role": "user", "content": "once"}}) + "\n"
    assert phone.parse_frame(raw) == "once"


def test_junk_on_the_socket_is_not_an_answer_and_not_a_crash():
    for junk in ("", "not json", "{}", '{"type":"assistant"}',
                 '{"type":"user","message":{}}'):
        assert phone.parse_frame(junk) is None


# ── inbound: the answer lands on the right question ────────────────────────


def test_an_id_prefixed_reply_answers_that_ask_and_not_the_newest(asks):
    """THE requirement. He is paged about `first`, something else blocks while
    his thumb is moving, and his "1" must still settle `first`."""
    first = an_ask(asks, agent="acme-growth")
    second = an_ask(asks, agent="orion", subject="rm -rf build")
    assert second.id != first.id

    got = phone.resolve(f"{first.id} 1", asking.pending(asks))

    assert got.refusal is None
    assert got.ask_id == first.id
    assert got.reply == "1"


def test_the_id_may_be_followed_by_the_word_as_well_as_the_number(asks):
    ask = an_ask(asks)
    assert phone.resolve(f"{ask.id} always", asking.pending(asks)).ask_id == ask.id
    assert phone.resolve(f"{ask.id}: never", asking.pending(asks)).reply == "never"


def test_a_bare_reply_settles_the_only_question_outstanding(asks):
    """The ordinary case: one buzz, one thumb, one word."""
    ask = an_ask(asks)
    got = phone.resolve("1", asking.pending(asks))
    assert got.ask_id == ask.id
    assert got.reply == "1"


def test_a_bare_reply_with_two_questions_open_refuses_and_names_them(asks):
    """Never guess. Guessing here approves the wrong action on the wrong
    machine, and he would have no way to tell that it had."""
    first = an_ask(asks, agent="acme-growth")
    second = an_ask(asks, agent="orion", subject="rm -rf build")

    got = phone.resolve("1", asking.pending(asks))

    assert got.ask_id is None
    assert first.id in got.refusal and second.id in got.refusal


def test_an_id_that_names_nothing_is_refused_by_name(asks):
    an_ask(asks)
    got = phone.resolve("zzzzz once", asking.pending(asks))
    assert got.ask_id is None
    assert "zzzzz" in got.refusal


def test_ordinary_chat_is_not_an_answer(asks):
    """He also just talks to sessions on this channel. `never mind, I'll do
    it` must not read as a standing deny -- `asking.parse_reply` is strict for
    this reason and this keeps that contract."""
    an_ask(asks)
    for chat in ("never mind, I'll do it", "how's it going", "yes please"):
        assert phone.resolve(chat, asking.pending(asks)).ask_id is None


def test_a_reply_with_nothing_outstanding_says_so(asks):
    got = phone.resolve("1", [])
    assert got.ask_id is None
    assert got.refusal


# ── inbound: the socket the bridge actually writes to ──────────────────────


def test_the_listener_hands_on_what_was_written_to_it(sock_dir):
    """End to end over a real UNIX socket, which is the only part of the
    transport this repo owns. Nothing here is a Claude session and no bridge
    runs; the frame is written by hand exactly as `src/inject.js` writes it."""
    heard = []
    sock_path = sock_dir / "agentdeck.sock"
    listener = phone.Listener(sock_path, on_text=heard.append)
    assert listener.start() is True
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock_path))
        client.sendall(frame("abcde always").encode())
        client.close()

        deadline = time.time() + 3
        while not heard and time.time() < deadline:
            time.sleep(0.02)
    finally:
        listener.stop()

    assert heard == ["abcde always"]


def test_the_listener_replaces_a_stale_socket_file(sock_dir):
    """A crash leaves the inode behind, and bind() on an existing path fails
    with EADDRINUSE. The deck would then come up healthy with no reply path --
    the exact silent failure this whole slice exists to remove."""
    sock_path = sock_dir / "agentdeck.sock"
    sock_path.write_text("")  # a dead file where the socket should be

    listener = phone.Listener(sock_path, on_text=lambda _: None)
    assert listener.start() is True
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.connect(str(sock_path))  # raises if nothing is accepting
        probe.close()
    finally:
        listener.stop()


def test_a_socket_that_cannot_be_bound_is_reported_not_swallowed(tmp_path):
    """`start()` returning False is how the daemon learns it has an alarm and
    no answer path. Swallowed, the deck comes up green and his replies go
    nowhere -- which is exactly today's failure with extra steps."""
    too_long = tmp_path / ("x" * 120) / "agentdeck.sock"
    listener = phone.Listener(too_long, on_text=lambda _: None)
    assert listener.start() is False


def test_the_listener_stops_cleanly_and_takes_its_socket_with_it(sock_dir):
    sock_path = sock_dir / "agentdeck.sock"
    listener = phone.Listener(sock_path, on_text=lambda _: None)
    assert listener.start() is True
    listener.stop()
    assert not sock_path.exists()
