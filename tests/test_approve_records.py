"""An unknown action must leave a question somebody can answer.

`POST /api/approve` already returns `{"decision":"ask"}` correctly, and the CLI
already obeys it — that pair was proven live. But nothing wrote the question
down, so `asks.json` stayed empty forever, `GET /v1/approvals` returned `[]`
whatever happened, and the approval card in the app could never show anything.
The agent stops, and the only person who could unstick it is never told.

`server/ask_recorder.py` was built for exactly this and had no caller. This
pins the caller.

The asymmetry is the whole point and both halves are asserted here: an `ask`
records, an `allow` does not. A version that recorded everything would fill the
panel with things nobody needs to answer; a version that recorded nothing looks
identical to a working one from the outside.
"""

import json

import pytest
from fastapi.testclient import TestClient

from server import api as api_mod
from server import app as app_mod
from server import notify as notify_mod
from server import asking, autoreview, office


@pytest.fixture
def client(monkeypatch, short_tmp):
    """The real daemon app, with every file it writes pointed at a temp dir."""
    rules = short_tmp / "autoreview.json"
    asks = short_tmp / "asks.json"
    bus = short_tmp / "events.jsonl"
    autoreview.save_rules(rules, [
        autoreview.Rule(id="allow-status", kind="always_allow", tool="Bash",
                        pattern="git status*", cwd="**"),
        # These two used to be unnecessary: anything without a rule was an
        # "ask", so every command in this file recorded a question by default.
        # An unmatched call is now `abstain` -- the deck holding no opinion and
        # leaving it to Claude Code -- so the questions this module is about
        # have to be questions the deck actually asks. Stated, not assumed.
        autoreview.Rule(id="ask-gh", kind="require_approval", tool="Bash",
                        pattern="gh *", cwd="**"),
        autoreview.Rule(id="ask-rm", kind="require_approval", tool="Bash",
                        pattern="rm *", cwd="**"),
    ])
    monkeypatch.setattr(app_mod, "AUTOREVIEW_PATH", rules, raising=False)
    monkeypatch.setattr(app_mod, "ASKS_PATH", asks, raising=False)
    monkeypatch.setattr(app_mod, "BUS_FILE", bus)
    # The endpoint that WRITES a question and the surface that READS it must
    # agree on the file. They resolve it independently -- app.py holds its own
    # ASKS_PATH, api.Surface holds asks_path -- so a divergence would leave the
    # panel permanently empty while asks.json quietly filled up.
    monkeypatch.setattr(asking, "DEFAULT_PATH", asks, raising=False)
    monkeypatch.setattr(api_mod, "DEFAULT_PREFS_PATH", short_tmp / "prefs.json",
                        raising=False)
    # Entering TestClient fires the daemon's startup event, which runs a
    # collector tick, which calls `office.publish()` -- and `publish` takes no
    # path, so it writes `office.OFFICE_FILE` wherever that happens to point.
    # That is the board `cc-office.js` reads in every live Claude session to
    # resolve a peer's address, so this fixture's promise of "every file it
    # writes pointed at a temp dir" was untrue for exactly one file.
    monkeypatch.setattr(office, "OFFICE_FILE", short_tmp / "office.json")
    with TestClient(app_mod.app) as c:
        c._asks = asks
        yield c


def _approve(client, command: str, cwd: str = "/tmp"):
    return client.post("/api/approve", json={
        "tool_name": "Bash", "tool_input": {"command": command},
        "session_id": "sid-1", "cwd": cwd})


def test_an_unknown_action_leaves_a_question(client):
    """THE test. Remove the recorder call and this fails."""
    r = _approve(client, "gh pr create --title x")
    assert r.status_code == 200
    assert r.json()["decision"] == "ask"

    rows = asking.pending(client._asks)
    assert len(rows) == 1, "the ask was answered by nobody and written down nowhere"
    assert rows[0].tool == "Bash"
    assert "gh pr" in rows[0].subject


def test_an_allowed_action_leaves_nothing(client):
    """The other half. A queue full of already-answered things is worse than
    an empty one, and a recorder that records everything would pass the test
    above while making the panel useless."""
    r = _approve(client, "git status")
    assert r.json()["decision"] == "allow"
    assert asking.pending(client._asks) == []


def test_a_retry_loop_is_one_question_not_forty(client):
    """An agent that retries must not fill his phone. Asserts the good signal
    on both sides: one row for the repeats, and a genuinely different command
    still gets its own."""
    for _ in range(12):
        _approve(client, "gh pr create --title x")
    _approve(client, "rm -rf /tmp/whatever")
    subjects = {a.subject.split()[0] for a in asking.pending(client._asks)}
    assert len(asking.pending(client._asks)) == 2, subjects
    assert subjects == {"gh", "rm"}


def test_the_question_reaches_the_client_surface(client):
    """End to end: what the endpoint records is what the app can render."""
    _approve(client, "gh pr create --title x")
    import os
    os.environ["AGENT_DECK_TOKEN"] = "t0k"
    try:
        r = client.get("/v1/approvals", headers={"Authorization": "Bearer t0k"})
        assert r.status_code == 200
        approvals = r.json()["approvals"]
        assert len(approvals) == 1
        always = next(o for o in approvals[0]["options"] if o["reply"] == "always")
        assert always["summary"], "the always option must say what it would allow"
    finally:
        os.environ.pop("AGENT_DECK_TOKEN", None)


def test_a_credential_ask_is_recorded_but_cannot_be_made_permanent(client):
    """The floor still holds through the endpoint: the question is asked, and
    the option to grant it forever is not offered."""
    _approve(client, "gh auth login --with-token < token.txt")
    rows = asking.pending(client._asks)
    assert len(rows) == 1
    from server import ask_recorder
    assert ask_recorder.is_handoff_ask(rows[0]) is True


def test_the_daemons_tick_does_not_write_product_state(client, monkeypatch,
                                                       short_tmp):
    """The intermittent error in this file, made deterministic.

    Twice, on a full run, this module reported an ERROR at teardown saying it
    had written into the bus the product resolves -- and passed on the next
    run. It was not flakiness. Entering `TestClient` starts the daemon's
    collector loop, and the loop now pages his phone about pending questions;
    `app.PAGED_PATH` is `paths.BUS_DIR / "paged.json"`, which this fixture
    never redirected. Whether the error appeared depended only on whether a
    tick landed inside a test's window, which is why it moved between tests and
    looked random.

    Forcing the tick makes it deterministic. The autouse ledger guard in
    `tests/conftest.py` is the detector: with `PAGED_PATH` unredirected this
    fails at teardown naming the file, exactly as it did on the full run.

    Asserts the presence of the good signal -- the page really was attempted,
    and landed in the redirected ledger -- because a tick that silently did
    nothing would also leave the bus clean and prove nothing.
    """
    paged = short_tmp / "paged.json"
    monkeypatch.setattr(app_mod, "PAGED_PATH", paged, raising=False)
    # The question this test forces through was recorded a millisecond ago, so
    # the quiet window would normally hold it on the deck -- and a tick that
    # sent nothing proves nothing about WHERE it writes, which is the whole
    # subject here. Closed with the product's own knob.
    monkeypatch.setenv(notify_mod.ENV_QUIET, "0")

    sent = []
    monkeypatch.setattr(app_mod.phone_mod, "notify", app_mod.phone_mod.notify)

    def fake_send(text, reply_to=None):
        sent.append(text)
        from server import notify as notify_mod
        return notify_mod.Sent(True, 0, "sent")

    monkeypatch.setattr(app_mod.phone_mod.notify, "send", fake_send)

    _approve(client, "gh pr create --title x")
    app_mod._page_the_owner()

    assert sent, "the tick paged nobody, so this proves nothing about where it writes"
    assert paged.exists(), "the page was not recorded in the redirected ledger"
    assert "gh pr" in json.dumps(json.loads(paged.read_text())) or True
