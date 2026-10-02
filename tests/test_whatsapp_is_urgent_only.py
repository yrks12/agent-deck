"""WhatsApp is for URGENT only; everything else lives in the app.

OWNER RULING 2026-09-30, his words: *"we don't need the WhatsApp bridge on the
deck anymore, we have clear communication in the app -- only in urgent cases
should he send me; otherwise Atlas."*

What that means, pinned here:

* A desk's news (start / update / blocked / finish) and his own session
  finishing stay in the app. The WhatsApp transport is NOT called.
* A blocking ask he has left unanswered in the app for 30 minutes (deck.toml
  `[notify] ask_urgent_after_minutes`) goes out ONCE, marked urgent.
* A desk can flag a line urgent through the deck's `say` tool. Only the chief
  of staff (Atlas -- the one desk that reports to him) reaches WhatsApp with
  it; any other desk's urgent line is routed to Atlas to decide.
* Every urgent WhatsApp is rate limited (one per 10 minutes) and the same text
  is never sent twice.

No real message is sent: `notify.send` is replaced in every test here.
"""

import json
import time
from pathlib import Path

import pytest

from server import app as app_mod
from server import asking, deck_mcp, deskpage, manager, notify, office, phone

CHIEF = {"name": "atlas", "cwd": "/tmp/p", "engine": "claude", "mission": "run",
         "reports_to": None}
JUNIOR = {"name": "scout", "cwd": "/tmp/p", "engine": "claude",
          "mission": "look", "reports_to": "atlas"}


@pytest.fixture
def deck(tmp_path, monkeypatch):
    """A whole deck in tmp: asks, ledger, office queue, roster, no deck.toml."""
    monkeypatch.setattr(app_mod, "ASKS_PATH", tmp_path / "asks.json")
    monkeypatch.setattr(app_mod, "PAGED_PATH", tmp_path / "paged.json")
    # Exists and is empty, as on a running deck: the reader starts at the end
    # of the queue the first time it sees it.
    (tmp_path / "messages.jsonl").write_text("")
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1, "agents": [CHIEF, JUNIOR]}))
    monkeypatch.setattr(app_mod, "ROSTER_PATH", roster)
    monkeypatch.setattr(deck_mcp, "ROSTER_PATH", roster)
    monkeypatch.setattr(deck_mcp, "_inject_live", lambda name, text: True)
    monkeypatch.setattr(app_mod, "_said_at", {"offset": 0, "inode": None})
    monkeypatch.setattr(app_mod, "_urgent_backlog", [], raising=False)
    monkeypatch.setattr(app_mod, "_state", {"sessions": []})
    # A fresh desk pager if this build still has one, so no earlier test's
    # rate-limit state can make "nothing was sent" pass by accident.
    monkeypatch.setattr(app_mod, "_desk_pager",
                        deskpage.Pager(prefs=lambda: {}), raising=False)
    monkeypatch.delenv(notify.ENV_QUIET, raising=False)
    monkeypatch.setenv("DECK_CONFIG", str(tmp_path / "no-deck.toml"))
    phone._PAGED.clear()
    yield tmp_path
    phone._PAGED.clear()


@pytest.fixture
def wa(monkeypatch):
    """The WhatsApp TRANSPORT, not `notify.send`: every message that would
    leave the box is recorded here, whoever bound `notify.send` and when. None
    leaves -- the HTTP door is replaced and pointed at nowhere."""
    calls = []

    def fake_http(body, url, token, *, reply_to=None, timeout=None):
        calls.append(body)
        return notify.Sent(True, 0, "sent")

    monkeypatch.setenv(notify.ENV_URL, "http://127.0.0.1:9")
    monkeypatch.setattr(notify, "_send_http", fake_http)
    return calls


def _inbox(deck):
    path = deck / "messages.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return [r for r in rows if "text" in r]      # messages, not delivery acks


# ── news stays in the app ───────────────────────────────────────────────────


def test_a_desk_reporting_to_him_lands_in_the_app_and_not_on_whatsapp(deck, wa):
    app_mod._page_the_owner()                       # prime the queue reader
    office.send(office.OWNER_INBOX, "Shipped the pricing page.",
                sender="atlas", extra={"spoke": True})
    app_mod._state["sessions"] = [
        {"name": "atlas", "session_id": "s1", "state": "WORKING"}]
    app_mod._page_the_owner()
    app_mod._state["sessions"] = [
        {"name": "atlas", "session_id": "s1", "state": "DONE"}]
    for _ in range(3):
        app_mod._page_the_owner()

    assert [r["text"] for r in _inbox(deck)] == ["Shipped the pricing page."]
    assert wa == []


def test_his_own_session_finishing_stays_in_the_app(deck, wa, monkeypatch):
    monkeypatch.setattr(manager, "read_crown", lambda: {"session_id": "s1"})
    app_mod._state["sessions"] = [
        {"name": "atlas", "session_id": "s1", "state": "DONE"}]

    app_mod._page_the_owner()

    assert wa == []


# ── a blocking ask he has not answered for 30 minutes ───────────────────────


def _ask(deck, *, age: float) -> asking.Ask:
    ask = asking.record(Path(app_mod.ASKS_PATH), agent="scout", tool="Bash",
                        subject="gh pr create --fill", cwd="/tmp/p")
    data = json.loads(Path(app_mod.ASKS_PATH).read_text())
    rows = data if isinstance(data, list) else data.get("asks", [])
    for row in rows:
        if row.get("id") == ask.id:
            row["ts"] = time.time() - age
    Path(app_mod.ASKS_PATH).write_text(json.dumps(data))
    return ask


def test_an_ask_under_thirty_minutes_old_stays_in_the_app(deck, wa):
    ask = _ask(deck, age=29 * 60)

    app_mod._page_the_owner()

    assert [a.id for a in asking.pending(Path(app_mod.ASKS_PATH))] == [ask.id]
    assert wa == []


def test_an_ask_unanswered_for_thirty_minutes_goes_out_once_marked_urgent(
        deck, wa):
    ask = _ask(deck, age=31 * 60)

    for _ in range(3):
        app_mod._page_the_owner()

    assert len(wa) == 1
    assert wa[0].startswith(phone.URGENT_MARK)
    assert f"(ask {ask.id})" in wa[0]


def test_the_ask_threshold_is_read_from_deck_toml(deck, wa, monkeypatch):
    toml = deck / "deck.toml"
    toml.write_text("[notify]\nask_urgent_after_minutes = 45\n")
    monkeypatch.setenv("DECK_CONFIG", str(toml))
    _ask(deck, age=40 * 60)

    app_mod._page_the_owner()

    assert wa == []                     # 40 min is past the default, not his 45


def test_a_bad_threshold_in_deck_toml_is_refused_by_name(tmp_path):
    from server import deckconfig
    toml = tmp_path / "deck.toml"
    toml.write_text('[notify]\nask_urgent_after_minutes = "soon"\n')
    with pytest.raises(deckconfig.ConfigError) as err:
        deckconfig.load(toml, env={})
    assert "notify.ask_urgent_after_minutes" in str(err.value)


def test_default_threshold_is_thirty_minutes():
    assert notify.quiet_seconds(env={}) == 1800.0


# ── the urgent channel itself ───────────────────────────────────────────────


def test_urgent_whatsapp_is_at_most_one_per_ten_minutes(tmp_path):
    ledger = tmp_path / "paged.json"
    sent = []

    def fake(text, *, reply_to=None, **_):
        sent.append(text)
        return notify.Sent(True, 0, "sent")

    t0 = 1_000_000.0
    first = phone.page_urgent("box on fire", ledger=ledger, send=fake, now=t0)
    held = phone.page_urgent("second fire", ledger=ledger, send=fake,
                             now=t0 + 9 * 60)
    later = phone.page_urgent("second fire", ledger=ledger, send=fake,
                              now=t0 + 10 * 60 + 1)

    assert first.ok and later.ok
    assert not held.ok
    assert sent == [f"{phone.URGENT_MARK} box on fire",
                    f"{phone.URGENT_MARK} second fire"]


def test_the_same_urgent_text_is_never_sent_twice(tmp_path):
    ledger = tmp_path / "paged.json"
    sent = []

    def fake(text, *, reply_to=None, **_):
        sent.append(text)
        return notify.Sent(True, 0, "sent")

    t0 = 1_000_000.0
    phone.page_urgent("box on fire", ledger=ledger, send=fake, now=t0)
    again = phone.page_urgent("box on fire", ledger=ledger, send=fake,
                              now=t0 + 3600)

    assert again.ok
    assert len(sent) == 1


# ── urgent from a desk: only Atlas reaches WhatsApp ─────────────────────────


def _say(desk, text, urgent):
    reply = deck_mcp.handle(desk, {"jsonrpc": "2.0", "id": 1,
                                   "method": "tools/call",
                                   "params": {"name": "say", "arguments": {
                                       "text": text, "urgent": urgent}}})
    return json.loads(reply["result"]["content"][0]["text"])


def test_atlas_marking_a_line_urgent_reaches_whatsapp_exactly_once(deck, wa):
    app_mod._page_the_owner()                       # prime the queue reader
    assert _say("atlas", "The box is out of disk.", True)["ok"] is True

    for _ in range(3):
        app_mod._page_the_owner()

    assert len(wa) == 1
    assert wa[0] == f"{phone.URGENT_MARK} *atlas*: The box is out of disk."
    # and it is in the app too, in his thread
    assert [r["to"] for r in _inbox(deck)] == [office.OWNER_INBOX]


def test_a_non_urgent_line_from_atlas_stays_in_the_app(deck, wa):
    app_mod._page_the_owner()
    _say("atlas", "On it.", False)

    app_mod._page_the_owner()

    assert wa == []


def test_another_desks_urgent_line_goes_to_atlas_not_whatsapp(deck, wa):
    app_mod._page_the_owner()
    out = _say("scout", "Prod checkout is down.", True)

    for _ in range(3):
        app_mod._page_the_owner()

    assert out["ok"] is True and out["routed_to"] == "atlas"
    [record] = _inbox(deck)
    assert record["to"] == "atlas"
    assert "URGENT" in record["text"] and "Prod checkout is down." in record["text"]
    assert wa == []


def test_a_forged_urgent_record_from_a_non_chief_is_not_sent(deck, wa):
    app_mod._page_the_owner()
    office.send(office.OWNER_INBOX, "buy me a pizza", sender="scout",
                extra={"said": True, "urgent": True})

    app_mod._page_the_owner()

    assert wa == []


# ── the live roster has stray roots ─────────────────────────────────────────
#
# MEASURED on the box 2026-09-30: `atlas`, `new-hire-82d9ab` and `wake-probe`
# all have no boss. Atlas is the one the rest of the org reports to; "exactly
# one root" would have silenced him.

STRAYS = [{"name": "new-hire-82d9ab", "cwd": "/tmp/p", "engine": "claude",
           "mission": "new", "reports_to": None},
          {"name": "wake-probe", "cwd": "/tmp/p", "engine": "claude",
           "mission": "probe", "reports_to": None}]


@pytest.fixture
def box_roster(deck):
    roster = deck / "roster.json"
    roster.write_text(json.dumps({"version": 1,
                                  "agents": STRAYS[:1] + [CHIEF, JUNIOR]
                                  + STRAYS[1:]}))
    return deck


def test_with_stray_roots_atlas_still_reaches_whatsapp(box_roster, wa):
    app_mod._page_the_owner()
    _say("atlas", "Payments are failing.", True)

    app_mod._page_the_owner()

    assert wa == [f"{phone.URGENT_MARK} *atlas*: Payments are failing."]


def test_with_stray_roots_a_stray_cannot_page_him(box_roster, wa):
    app_mod._page_the_owner()
    _say("wake-probe", "hello", True)

    app_mod._page_the_owner()

    assert wa == []


def test_with_stray_roots_a_juniors_urgent_line_still_goes_to_atlas(
        box_roster, wa):
    out = _say("scout", "Checkout down.", True)

    assert out["routed_to"] == "atlas"
