"""What he types on his phone has to land on the desk he meant.

THE HALF THAT WAS MISSING. The desk pager pushes every desk's news to his
phone. The trip back was ASSUMED: each push carries that desk's socket as a
return address, and nothing on this machine had ever proved a reply travelled
down it. Without the return trip the push is a broadcast, not a conversation --
which is exactly the gap he named watching Grok, where he taps a card and the
agent carries on.

Two things are built here and the harness is the more important one.

1. A NAMED-DESK GRAMMAR. `Hamatsesa: do X` reaches Hamatsesa. He types this
   one-handed, on a phone, so the sloppy shapes are the real shapes: wrong
   case, stray spaces, a bare prefix of the name, a leading `@`. Every shape
   that CANNOT be routed answers him -- a silent drop is the worst outcome,
   because he will believe the desk got it.

2. A HARNESS THAT MAKES THE REPLY PATH MEASURABLE WITHOUT HIS PHONE. Until
   now there was none: proving a reply landed meant him tapping a message.
   `FakeSession` binds a UNIX socket where a real Claude session's would be,
   the bridge's exact inbound frame is written into the deck's own listener,
   and the assertion is that the words arrived at THAT desk's socket and at no
   other. Every future change to this path is measured against it instead of
   guessed at.

AND A LIVE HAZARD FOUND ON THE WAY, pinned here because it is what made the
harness unsafe to build. MEASURED on this Mac:

    notify.script_path() -> ~/Projects/comunicate_with_me/bin/wa-send.js
    exists -> True

`tests/conftest.py` redirects the bus, the roster and every ledger into a
temp home, and guards that it did. It does NOT redirect the phone. Any test
that reaches `notify.send` without stubbing it spawns the real bridge and
sends Sam a real WhatsApp -- and the inbound path under test here ends in
`_say_to_phone`, so building it without a guard would have made the suite page
him on every run.
"""

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from server import notify
from server import phone


# -- the harness -------------------------------------------------------------


class FakeSession:
    """A stand-in for a live Claude session's UNIX socket.

    Speaks the far half of `manager.inject`: accept, read until the peer
    half-closes, remember the bytes. Nothing is interpreted -- the point is to
    prove which socket the words arrived on, and interpreting them here would
    let a bug in the reader hide a bug in the router.
    """

    def __init__(self, path):
        self.path = path
        self.blobs: list[bytes] = []
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(4)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(1.0)
                chunks = []
                try:
                    while True:
                        data = conn.recv(65536)
                        if not data:
                            break
                        chunks.append(data)
                except OSError:
                    pass
            if chunks:
                self.blobs.append(b"".join(chunks))

    def heard(self) -> str:
        """Every user turn that arrived, as text.

        Reads the `type: "user"` frames and ignores an `auth` line, because
        `manager.inject` prepends one whenever the session published a token.
        """
        out = []
        for blob in self.blobs:
            for line in blob.decode("utf-8", "replace").splitlines():
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if payload.get("type") != "user":
                    continue
                content = (payload.get("message") or {}).get("content")
                if isinstance(content, str):
                    out.append(content)
        return "\n".join(out)

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


def bridge_frame(text: str) -> bytes:
    """Exactly what `src/inject.js` writes into a session socket."""
    payload = {"type": "user",
               "message": {"role": "user",
                           "content": f"[via WhatsApp from Sam]\n{text}"}}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def speak_to_deck(path, text: str) -> None:
    """Play the bridge: connect to the deck's socket, write, half-close."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2.0)
        client.connect(str(path))
        client.sendall(bridge_frame(text))
        client.shutdown(socket.SHUT_WR)


def wait_for(predicate, timeout: float = 3.0) -> bool:
    """The listener is a thread; polling beats sleeping a fixed guess."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- 0. no test may reach his phone ------------------------------------------


def test_no_test_can_reach_his_phone():
    """The guard that had to exist before the rest of this file could.

    MEASURED before it: `notify.script_path()` resolved to the real
    `wa-send.js` on his Mac and that file exists, so an unstubbed `send` in any
    test spawns the bridge and messages him. conftest redirects the bus and
    guards the redirect; the phone was never covered.
    """
    assert not notify.script_path().exists(), (
        "tests resolve to a REAL bridge script at "
        f"{notify.script_path()} — an unstubbed send would WhatsApp him")

    result = notify.send("this must never leave the machine")
    assert not result.ok, "a test just claimed a real send succeeded"


def test_the_guard_survives_an_http_bridge():
    """The box reaches the bridge over the tunnel with `DECK_WA_URL`, which
    skips the script path entirely. Neutering only the script would leave the
    suite able to page him from any machine that has the URL set."""
    import os
    assert not os.environ.get(notify.ENV_URL), \
        "DECK_WA_URL is set inside the test process — sends would go over HTTP"


# -- 1. the grammar ----------------------------------------------------------


DESKS = ["Hamatsesa", "Product", "Producer", "drift watch"]


def route(text, desks=DESKS, pending=()):
    return phone.resolve(text, list(pending), desks=desks)


def test_a_named_desk_routes_to_that_desk():
    got = route("Hamatsesa: run the bake")
    assert got.desk == "Hamatsesa", got
    assert got.reply == "run the bake"
    assert got.refusal is None


def test_the_name_is_case_insensitive():
    assert route("hamatsesa: run the bake").desk == "Hamatsesa"
    assert route("HAMATSESA: run the bake").desk == "Hamatsesa"


def test_whitespace_sloppiness_still_routes():
    """He types this one-handed, walking."""
    assert route("  Hamatsesa   :    run the bake  ").desk == "Hamatsesa"
    assert route("Hamatsesa :run the bake").desk == "Hamatsesa"


def test_a_name_with_a_space_in_it_routes():
    got = route("drift watch: what is blocking you")
    assert got.desk == "drift watch", got
    assert got.reply == "what is blocking you"


def test_a_leading_at_sign_routes():
    assert route("@Hamatsesa: run the bake").desk == "Hamatsesa"
    assert route("@Hamatsesa run the bake").desk == "Hamatsesa"


def test_a_unique_prefix_routes():
    got = route("ham: run the bake")
    assert got.desk == "Hamatsesa", got
    assert got.reply == "run the bake"


def test_an_ambiguous_prefix_is_refused_by_name():
    """`prod` is both Product and Producer. Guessing would put his instruction
    on the wrong desk, and he would never know which."""
    got = route("prod: ship it")
    assert got.desk is None, got
    assert got.refusal, "an ambiguous name must answer him, not vanish"
    assert "Product" in got.refusal and "Producer" in got.refusal, got.refusal


def test_an_exact_name_beats_a_prefix_collision():
    """`Product` is a prefix of nothing, but `Producer` starts with it. An
    exact name must never be refused as ambiguous."""
    got = route("Product: ship it")
    assert got.desk == "Product", got


def test_an_unknown_desk_answers_him_rather_than_vanishing():
    got = route("Nobody: ship it")
    assert got.desk is None and got.ask_id is None
    assert got.refusal, "a silent drop makes him believe the desk got it"
    assert "Nobody" in got.refusal, got.refusal
    assert "Hamatsesa" in got.refusal, "he should be told who he CAN reach"


def test_a_named_desk_with_nothing_to_say_is_refused():
    got = route("Hamatsesa:")
    assert got.desk is None, got
    assert got.refusal, got


def test_a_reply_with_no_name_is_unchanged():
    """The default is load-bearing: a bare `1` answering the one open question
    is how every approval has worked, and this must not touch it."""
    from server import asking

    ask = asking.Ask(id="kfmp", ts=0.0, agent="Hamatsesa", tool="Bash",
                     subject="run tests", cwd="/tmp")
    got = route("1", pending=[ask])
    assert got.ask_id == "kfmp", got
    assert got.desk is None


def test_ordinary_chat_is_still_silent():
    """Refusing every sentence would make the channel unusable for talking."""
    for chat in ("thanks", "ok cool", "what is happening"):
        got = route(chat)
        assert got.ask_id is None and got.desk is None and got.refusal is None, \
            (chat, got)


def test_an_open_ask_id_beats_a_desk_of_the_same_name():
    """A question he was paged about is the thing in front of him. Routing it
    to a desk would silently leave an agent parked while he believed he had
    freed it.

    The id here is ask-shaped on purpose. `asking.parse_reply` only accepts a
    short id of its own vocabulary, so a first draft of this test using the id
    "Product" failed inside the ASK branch rather than proving precedence --
    the branch was already winning, the fixture was just unreachable. Ask ids
    also exclude 0/1/i/l/o (ID_ALPHABET), the characters a thumb confuses, so
    a second draft using "ab12" was unreachable for the same reason.
    """
    from server import asking

    ask = asking.Ask(id="kfmp", ts=0.0, agent="Hamatsesa", tool="Bash",
                     subject="run tests", cwd="/tmp")
    got = phone.resolve("kfmp: once", [ask], desks=["kfmp", "Hamatsesa"])
    assert got.ask_id == "kfmp", got
    assert got.reply == "once", got
    assert got.desk is None, "his approval was routed to a desk as chat"


def test_no_roster_means_the_old_behaviour_exactly():
    """`resolve` is called with two arguments in the existing code and tests.
    Adding a third must change nothing when it is not supplied."""
    got = phone.resolve("zzzzz once", [])
    assert got.ask_id is None and got.desk is None
    assert got.refusal, got


# -- 2. the trip back, end to end, over real sockets -------------------------


@pytest.fixture
def wired(monkeypatch):
    """A deck socket, two desks with live sessions, and nothing that can page.

    NOT under `tmp_path`. A UNIX socket path is capped at 104 bytes on macOS
    and pytest's per-test directory spends most of that on the test's own name
    -- the first run of this file failed with `AF_UNIX path too long` before it
    could assert anything. A short directory under /tmp is what the real socket
    directory is anyway.

    `notify.send` is replaced rather than trusted: this exercises
    `_phone_answer`, whose refusal and receipt branches both end at the phone.
    """
    import shutil
    import tempfile

    from server import app as app_mod
    from server import manager as manager_mod

    base = Path(tempfile.mkdtemp(prefix="dks-", dir="/tmp"))
    sock_dir = base / "s"
    sock_dir.mkdir()
    monkeypatch.setattr(manager_mod, "sock_dir", lambda: sock_dir)

    # **The roster, and this is the thing the first draft of this harness
    # left out.** `_phone_answer` asks `_desk_names()` which desks he may
    # address, and that reads the ROSTER -- not the live session list. A desk
    # with a running session and no roster row is deliberately NOT addressable
    # (see `test_no_roster_means_the_old_behaviour_exactly`), so without this
    # every test below sent his words into the ask branch and asserted against
    # a desk that was never a candidate. Red as:
    #   AssertionError: Hamatsesa never heard it; got ''
    # Into THIS test's directory, with the module constant pointed at it, or
    # `tests/conftest.py`'s bus guard refuses the run -- correctly: the real
    # roster.json is the file every agent on his machine is governed by.
    from server import roster as roster_mod

    roster_path = base / "roster.json"
    monkeypatch.setattr(app_mod, "ROSTER_PATH", roster_path)
    roster_mod.save_roster(
        roster_path,
        [roster_mod.Desk(name=name, cwd=str(base), engine="claude",
                         mission=f"{name} desk",
                         reports_to=None if i == 0 else "Hamatsesa")
         for i, name in enumerate(("Hamatsesa", "Product"))])

    sessions = {"Hamatsesa": 90001, "Product": 90002}
    fakes = {name: FakeSession(sock_dir / f"{pid}.sock")
             for name, pid in sessions.items()}
    monkeypatch.setattr(app_mod, "_state", {
        "sessions": [{"name": name, "pid": pid, "session_id": f"sid-{name}",
                      "state": "WORKING"}
                     for name, pid in sessions.items()]})

    said_to_phone: list = []
    monkeypatch.setattr(notify, "send",
                        lambda text, reply_to=None: (
                            said_to_phone.append(text)
                            or notify.Sent(True, 0, "stubbed")))

    # **The BOARD, and this is the second thing the harness left out.**
    # `_send_to_desk` deliberately goes down `Surface.send`, and that method
    # refuses any name that is not on `Surface._agents` -- the board the
    # surface folds forward from the roster plus the collector snapshot. The
    # fold happens in `Surface.refresh()`, which the daemon runs on a timer
    # (`api.REFRESH_SECONDS`) and which a test process never runs at all. So
    # the board was EMPTY, `send` 404'd every name, and his words came back to
    # him as a refusal instead of reaching the desk. Red as:
    #   phone == ["Not sent to *Hamatsesa*: no agent named 'Hamatsesa'"]
    #   AssertionError: Hamatsesa never heard it; got ''
    #
    # Pointed at the SAME roster `_desk_names()` reads, because on the daemon
    # they are one file -- `app.ROSTER_PATH is roster.DEFAULT_PATH`. A harness
    # that let the two diverge would stop measuring the machine: a desk could
    # be addressable from his phone and absent from the board, which is the
    # exact failure this fixture is here to catch.
    #
    # `office.MESSAGES_FILE` with it, because `Surface.send` QUEUES the record
    # before it tries the socket, and that queue is product state in the bus
    # that `tests/conftest.py` rightly refuses to let a test write.
    from server import office as office_mod

    surface = app_mod._client_surface
    monkeypatch.setattr(office_mod, "MESSAGES_FILE", base / "messages.jsonl")
    monkeypatch.setattr(surface, "_roster_path", roster_path)

    # The surface is a process-wide singleton and `refresh` REBINDS a dozen of
    # its attributes, so folding this fixture's two desks into it would leave
    # them on the board for every test that runs after this file -- the same
    # class of leak as the receipt ceiling in `tests/conftest.py`, which was
    # measured at 10 passed alone and 7 failed in the full suite. Snapshot the
    # instance and put it back, rather than trying to name each field.
    folded = dict(surface.__dict__)
    surface.refresh()

    deck_sock = sock_dir / phone.DECK_SOCKET_NAME
    listener = phone.Listener(deck_sock, on_text=app_mod._phone_answer)
    assert listener.start(), "the deck could not bind its own socket"
    try:
        yield {"deck": deck_sock, "fakes": fakes, "phone": said_to_phone,
               "app": app_mod, "surface": surface}
    finally:
        listener.stop()
        for fake in fakes.values():
            fake.close()
        surface.__dict__.clear()
        surface.__dict__.update(folded)
        shutil.rmtree(base, ignore_errors=True)


def test_his_reply_lands_on_the_desk_he_named(wired):
    """The whole trip: his thumb, the bridge's frame, the deck's socket, the
    router, that desk's session."""
    speak_to_deck(wired["deck"], "Hamatsesa: run the bake now")

    hama = wired["fakes"]["Hamatsesa"]
    assert wait_for(lambda: "run the bake now" in hama.heard()), \
        f"Hamatsesa never heard it; got {hama.heard()!r}"


def test_it_does_not_land_on_any_other_desk(wired):
    """The half that matters. One desk hearing it is not enough -- the defect
    this guards is his instruction arriving at every desk, or at the wrong one."""
    speak_to_deck(wired["deck"], "Hamatsesa: run the bake now")

    hama = wired["fakes"]["Hamatsesa"]
    assert wait_for(lambda: "run the bake now" in hama.heard())
    assert "run the bake now" not in wired["fakes"]["Product"].heard(), \
        "Product heard an instruction addressed to Hamatsesa"


def test_the_other_desk_gets_its_own_reply(wired):
    """Sweep both directions, so a router hard-wired to one desk fails."""
    speak_to_deck(wired["deck"], "Product: freeze live")

    product = wired["fakes"]["Product"]
    assert wait_for(lambda: "freeze live" in product.heard()), \
        f"Product never heard it; got {product.heard()!r}"
    assert "freeze live" not in wired["fakes"]["Hamatsesa"].heard()


# -- 3. the receipt path has a ceiling too -----------------------------------


def test_the_receipt_path_is_bounded():
    """`_say_to_phone` is the one path to his phone with NO limiter on it.

    Every desk push goes through `deskpage`'s gaps. Refusals and receipts do
    not: they call `notify.send` directly. That is bounded by his own thumb
    today and unbounded the moment anything else calls it -- which is precisely
    how the 285-sends-in-60-seconds defect happened on the other path.
    """
    from server import deskpage

    ceiling = deskpage.Ceiling(burst=3, refill=60.0)
    allowed = [ceiling.allow(now=float(i)) for i in range(10)]

    assert allowed[:3] == [True, True, True], allowed
    assert not any(allowed[3:]), allowed


def test_the_ceiling_refills_so_it_never_goes_permanently_silent():
    """A limiter that latches shut is worse than none: he would answer a desk
    and never be told again whether anything landed."""
    from server import deskpage

    ceiling = deskpage.Ceiling(burst=2, refill=60.0)
    assert ceiling.allow(now=0.0)
    assert ceiling.allow(now=1.0)
    assert not ceiling.allow(now=2.0)
    assert ceiling.allow(now=61.0), "the allowance never came back"


def test_his_own_replies_are_never_throttled_in_practice():
    """The burst has to be wide enough that a human conversation never hits
    it, or the limiter is a bug he experiences as the product ignoring him."""
    from server import deskpage

    ceiling = deskpage.Ceiling()
    # Five replies over four minutes -- a fast exchange for a person.
    assert all(ceiling.allow(now=float(t))
               for t in (0, 45, 100, 180, 240)), "a human pace was throttled"


def test_a_runaway_loop_is_capped(wired):
    """The good signal: the receipt still goes out, and a loop is bounded."""
    app_mod = wired["app"]
    for _ in range(200):
        app_mod._say_to_phone("something happened")

    sent = len(wired["phone"])
    assert sent >= 1, "the ceiling silenced the path entirely"
    assert sent <= 20, f"{sent} messages left for 200 calls"


def test_an_unroutable_name_answers_his_phone(wired):
    """He must learn the name was wrong. Silence here is the failure mode --
    he would go on believing the desk was told."""
    speak_to_deck(wired["deck"], "Nobodyy: ship it")

    assert wait_for(lambda: bool(wired["phone"])), \
        "an unroutable name produced no answer at all"
    assert "Nobodyy" in "\n".join(wired["phone"]), wired["phone"]
    for fake in wired["fakes"].values():
        assert "ship it" not in fake.heard()


# -- the receipt says exactly what the thread says ---------------------------
#
# The phone half of the hours he lost to five unanswered messages.
# `_send_to_desk` used to write its own receipt, and it had ONE sentence for
# every failure it could have: "*X* is not at its desk — queued for its next
# turn." A desk that was merely busy and a desk whose session had died read
# identically, which is the app's defect arriving by a second route. It now
# repeats `delivery.what` off the message the send returns -- one computation
# (`api.delivery_of`), both surfaces, no second wording to drift.


def _receipt(monkeypatch, outcome):
    """One trip through `_send_to_desk`, with the surface's answer stubbed.

    `_say_to_phone` is intercepted rather than `notify.send`, so nothing here
    can reach the bridge even if the module's own guard were ever removed.
    """
    from server import app as app_mod

    said: list = []
    monkeypatch.setattr(app_mod, "_say_to_phone", said.append)
    monkeypatch.setattr(app_mod._client_surface, "send",
                        lambda thread_id, text: outcome)
    app_mod._send_to_desk("hemingway", "hi?")
    return said


def _outcome(state, what, *, delivered=False):
    return {"ok": True, "delivered": delivered,
            "message": {"id": "m1", "delivery": {"state": state,
                                                 "reason": "", "what": what}}}


def test_a_dead_desk_is_not_reported_to_his_phone_as_merely_queued(monkeypatch):
    """The failure state, on the phone. The old receipt promised him it would
    be picked up on the next turn -- of a session that no longer existed."""
    sentence = "Nobody is at hemingway's desk, so nothing has taken this."
    (said,) = _receipt(monkeypatch, _outcome("undelivered", sentence))
    assert sentence in said, said
    assert "next turn" not in said, (
        f"a desk with no session was promised a next turn: {said!r}")


def test_a_live_desk_still_reports_the_good_news(monkeypatch):
    """The good signal. A receipt that only ever hedged would be useless to a
    man deciding whether to wait."""
    (said,) = _receipt(monkeypatch,
                       _outcome("delivered", "hemingway's session took this.",
                                delivered=True))
    assert "took this" in said, said


def test_the_phone_never_invents_a_state_the_thread_did_not_give(monkeypatch):
    """A group id, or a send that queued but folded to no message. It says the
    little it knows rather than guessing at a desk's condition."""
    (said,) = _receipt(monkeypatch, {"ok": True, "delivered": False,
                                     "message": None})
    assert "hemingway" in said
    assert "queue" in said.lower(), said
