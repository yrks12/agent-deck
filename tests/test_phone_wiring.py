"""The wiring: the daemon actually calls the phone loop.

`server/phone.py` can be perfect and still never run. These tests are the join,
and they exist because the exact defect they guard already happened once in this
repo: `server/notify.py` was complete, tested and imported by nobody, so the
board filled with questions and the phone stayed silent.

Both directions are pinned here:

  * `_page_the_owner` is on the collector tick, so it sweeps EVERY door that can
    record an ask -- `/api/approve`, `/api/permission`, `/api/elicitation` and
    the handoff floor -- rather than three instrumented call sites and whatever
    is added next week.
  * `_phone_answer` turns his typed words into `Surface.answer_ask`, which is
    the same call the board makes. Not a parallel implementation of answering:
    a second one would drift, and the half that drifted would be the one nobody
    watches.

No message is sent and no socket is bound by this module.
"""

import json
from pathlib import Path

import pytest

from server import app as app_mod
from server import asking, notify, phone


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Point every path at tmp. Nothing here may touch ~/.claude.

    The quiet window is closed with the SAME knob production uses, and that is
    deliberate on two counts. These tests ask whether the tick is WIRED to the
    phone at all -- the defect they exist for is `notify.py` having zero
    callers -- and a three-minute hold would make every one of them pass
    vacuously, which is the failure they were written to prevent. It also
    means `DECK_QUIET_SECONDS` is exercised end to end through the daemon's
    real call path rather than only in a unit test of the parser. When the
    hold itself is the subject, see `tests/test_the_app_gets_first_refusal.py`.
    """
    asks = tmp_path / "asks.json"
    monkeypatch.setattr(app_mod, "ASKS_PATH", asks)
    monkeypatch.setattr(app_mod, "PAGED_PATH", tmp_path / "paged.json")
    monkeypatch.setenv(notify.ENV_QUIET, "0")
    phone._PAGED.clear()
    yield asks
    phone._PAGED.clear()


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_send(text, *, reply_to=None, **_):
        calls.append({"text": text, "reply_to": reply_to})
        return phone.notify.Sent(True, 0, "sent")

    monkeypatch.setattr(phone.notify, "send", fake_send)
    return calls


# ── outbound is on the tick ─────────────────────────────────────────────────


def test_a_pending_ask_reaches_the_phone_on_a_tick(_isolated, sent, monkeypatch):
    """The good signal: his phone carries the id of the question the deck wrote
    down, without anyone having instrumented the endpoint that wrote it."""
    monkeypatch.setattr(app_mod, "_state", {"sessions": []})
    ask = asking.record(_isolated, agent="acme-growth", tool="Bash",
                        subject="gh pr create --fill", cwd="/tmp/acme")

    app_mod._page_the_owner()

    assert len(sent) == 1
    assert f"(ask {ask.id})" in sent[0]["text"]


def test_the_tick_pages_each_question_once_not_once_a_second(_isolated, sent,
                                                             monkeypatch):
    """The collector runs at 1 Hz and an ask stays pending until he answers it.
    Unguarded, one question is 3,600 messages an hour and a banned number."""
    monkeypatch.setattr(app_mod, "_state", {"sessions": []})
    asking.record(_isolated, agent="acme-growth", tool="Bash",
                  subject="git push", cwd="/tmp/acme")

    for _ in range(20):
        app_mod._page_the_owner()

    assert len(sent) == 1


def test_an_answered_question_is_never_paged(_isolated, sent, monkeypatch):
    """He answered it on the board a second before the tick ran. Buzzing him
    about a question he has already settled is how he learns to ignore it."""
    monkeypatch.setattr(app_mod, "_state", {"sessions": []})
    ask = asking.record(_isolated, agent="acme-growth", tool="Bash",
                        subject="git push", cwd="/tmp/acme")
    asking.answer(_isolated, ask.id, "once", Path(_isolated).parent / "rules.json")

    app_mod._page_the_owner()

    assert sent == []


def test_the_tick_leaves_the_manager_finishing_in_the_app(_isolated, sent,
                                                          monkeypatch):
    """OWNER RULING 2026-09-30: a run finishing is news, and news is app-only.
    Only urgent things reach WhatsApp -- see
    `tests/test_whatsapp_is_urgent_only.py`."""
    monkeypatch.setattr(app_mod, "_state", {"sessions": [
        {"session_id": "S-boss", "name": "cos", "state": "DONE", "pid": 11},
        {"session_id": "S-hand", "name": "worker", "state": "DONE", "pid": 12},
    ]})
    monkeypatch.setattr(app_mod.manager_mod, "read_crown",
                        lambda: {"session_id": "S-boss", "pid": 11})

    app_mod._page_the_owner()

    assert sent == []


def test_a_failing_tick_does_not_take_the_board_down(_isolated, monkeypatch):
    """The board is the product. A wedged bridge costs a notification, never
    the thing he is looking at."""
    monkeypatch.setattr(app_mod, "_state", {"sessions": []})
    asking.record(_isolated, agent="a", tool="Bash", subject="x", cwd="/tmp")

    def boom(*_a, **_k):
        raise RuntimeError("bridge on fire")

    monkeypatch.setattr(phone.notify, "send", boom)
    app_mod._page_the_owner()  # must not raise


# ── inbound reaches the same door the board uses ────────────────────────────


class FakeSurface:
    def __init__(self, result=None, error=None):
        self.calls = []
        self._result = result or {"ok": True, "resumed": True}
        self._error = error

    def answer_ask(self, ask_id, reply):
        self.calls.append((ask_id, reply))
        if self._error:
            raise self._error
        return self._result


def test_his_typed_answer_settles_the_ask_he_named(_isolated, sent, monkeypatch):
    """THE requirement, end to end through the daemon: two questions open, he
    quotes the older id, and the older one is what gets answered."""
    older = asking.record(_isolated, agent="acme-growth", tool="Bash",
                          subject="git push", cwd="/tmp/acme")
    asking.record(_isolated, agent="orion", tool="Bash",
                  subject="rm -rf build", cwd="/tmp/orion")

    surface = FakeSurface()
    monkeypatch.setattr(app_mod, "_client_surface", surface)

    app_mod._phone_answer(f"{older.id} always")

    assert surface.calls == [(older.id, "always")]


def test_an_ambiguous_answer_settles_nothing_and_says_why(_isolated, sent,
                                                          monkeypatch):
    """Two open, a bare '1'. Guessing would approve an action he was never
    shown, on a machine he is not at."""
    first = asking.record(_isolated, agent="acme-growth", tool="Bash",
                          subject="git push", cwd="/tmp/acme")
    second = asking.record(_isolated, agent="orion", tool="Bash",
                           subject="rm -rf build", cwd="/tmp/orion")

    surface = FakeSurface()
    monkeypatch.setattr(app_mod, "_client_surface", surface)

    app_mod._phone_answer("1")

    assert surface.calls == []
    assert len(sent) == 1
    assert first.id in sent[0]["text"] and second.id in sent[0]["text"]


def test_he_is_told_the_answer_landed(_isolated, sent, monkeypatch):
    """A confirmation is not a nicety. He is away from the desk with no board
    in front of him; silence after answering is indistinguishable from the
    reply never arriving."""
    ask = asking.record(_isolated, agent="acme-growth", tool="Bash",
                        subject="git push", cwd="/tmp/acme")
    monkeypatch.setattr(app_mod, "_client_surface",
                        FakeSurface({"ok": True, "resumed": True}))

    app_mod._phone_answer(f"{ask.id} once")

    assert len(sent) == 1
    assert ask.id in sent[0]["text"]


def test_a_refused_answer_is_reported_as_refused_not_as_done(_isolated, sent,
                                                             monkeypatch):
    """Same rule as notify's exit codes, one layer up: never tell him it
    landed when it did not."""
    ask = asking.record(_isolated, agent="acme-growth", tool="Bash",
                        subject="git push", cwd="/tmp/acme")
    boom = app_mod.api.Refused(409, "already_answered", "it was already 'never'")
    monkeypatch.setattr(app_mod, "_client_surface", FakeSurface(error=boom))

    app_mod._phone_answer(f"{ask.id} once")

    assert len(sent) == 1
    assert "already" in sent[0]["text"].lower()


def test_ordinary_chat_is_not_treated_as_an_answer(_isolated, sent, monkeypatch):
    """He talks to sessions on this channel too. A message that is not an
    answer must reach neither the ask ledger nor his phone as a refusal."""
    asking.record(_isolated, agent="acme-growth", tool="Bash",
                  subject="git push", cwd="/tmp/acme")
    surface = FakeSurface()
    monkeypatch.setattr(app_mod, "_client_surface", surface)

    app_mod._phone_answer("never mind, I'll look at it later")

    assert surface.calls == []
    assert sent == []
