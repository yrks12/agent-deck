"""Event wake-ups (Feature C): the pollers that run IN the deck, no model turn.

Failed deploys (`gh run list --status failure`), Gmail replies to threads the
mailbox sent (users.history.list from a stored historyId) and, as a fallback,
Stripe's own event list. Each is off until deck.toml `[events]` names it; the
first poll of a source only records where it is, so turning one on never
replays history into a desk.
"""

from __future__ import annotations

import json

from server import deckconfig, event_pollers, events


def _hub(tmp_path, routes):
    store = events.Store(tmp_path / "events")
    store.save_routes(routes)
    sent: list = []
    hub = events.Hub(store, deliver=lambda d, t: sent.append((d, t)) or "delivered",
                     window=0)
    return hub, sent


RUNS = [{"databaseId": 11, "workflowName": "Deploy", "headBranch": "main",
         "displayTitle": "fix: x", "url": "https://github.com/o/r/actions/runs/11"}]


def test_deploys_seed_first_then_report_only_new_failures(tmp_path):
    hub, sent = _hub(tmp_path, [{"source": "deploy", "desk": "eng"}])
    state = event_pollers.State(hub.store.root)
    rows = list(RUNS)
    run = lambda argv: json.dumps(rows)  # noqa: E731
    assert event_pollers.poll_deploys(hub, ["o/r"], run=run, state=state) == []
    assert sent == []
    rows.insert(0, {**RUNS[0], "databaseId": 12, "displayTitle": "feat: y",
                    "url": "https://github.com/o/r/actions/runs/12"})
    out = event_pollers.poll_deploys(hub, ["o/r"], run=run, state=state)
    assert [r["ref"] for r in out] == ["12"]
    assert sent[0][0] == "eng" and "Deploy failed on main" in sent[0][1]
    # Seeded state survives a restart: nothing is replayed.
    again = event_pollers.State(hub.store.root)
    assert event_pollers.poll_deploys(hub, ["o/r"], run=run, state=again) == []


def test_a_gh_failure_costs_one_repo_not_the_poll(tmp_path):
    hub, _ = _hub(tmp_path, [])
    state = event_pollers.State(hub.store.root)

    def run(argv):
        raise RuntimeError("gh: not logged in")

    assert event_pollers.poll_deploys(hub, ["o/r", "o/s"], run=run, state=state) == []


class FakeGmail:
    def __init__(self) -> None:
        self.history: list[dict] = []
        self.next_id = "200"

    def get(self, path: str, params: dict | None = None) -> dict:
        if path == "profile":
            return {"historyId": "100", "emailAddress": "me@example.com"}
        if path == "history":
            assert params["startHistoryId"] in ("100", "200")
            return {"history": self.history, "historyId": self.next_id}
        if path.startswith("messages/"):
            return {"payload": {"headers": [
                {"name": "From", "value": "Client <c@example.org>"},
                {"name": "Subject", "value": "Re: quote"}]}}
        raise AssertionError(path)


def _added(mid, tid, *labels):
    return {"messagesAdded": [{"message": {"id": mid, "threadId": tid,
                                           "labelIds": list(labels)}}]}


def test_gmail_reports_only_replies_on_threads_the_mailbox_sent(tmp_path):
    hub, sent = _hub(tmp_path, [{"source": "gmail", "desk": "outreach"}])
    state = event_pollers.State(hub.store.root)
    gmail = FakeGmail()
    assert event_pollers.poll_gmail(hub, gmail, state=state) == []   # seeds
    gmail.history = [_added("m1", "t1", "SENT"),
                     _added("m2", "t1", "INBOX", "UNREAD"),
                     _added("m3", "t9", "INBOX", "UNREAD")]
    out = event_pollers.poll_gmail(hub, gmail, state=state)
    assert [r["ref"] for r in out] == ["m2"]
    assert sent[0][0] == "outreach"
    assert "Client <c@example.org>" in sent[0][1] and "Re: quote" in sent[0][1]
    assert state.data["gmail_history_id"] == "200"


def test_the_events_section_defaults_every_source_off():
    cfg = deckconfig.DeckConfig()
    assert cfg.events.deploy_poll is False
    assert cfg.events.gmail_poll is False
    assert cfg.events.stripe_poll is False
    assert cfg.events.stripe_port == 0


def test_the_events_section_reads_from_deck_toml(tmp_path):
    path = tmp_path / "deck.toml"
    path.write_text('[events]\ndeploy_poll = true\ndeploy_repos = ["o/r"]\n'
                    'stripe_port = 7795\n')
    cfg = deckconfig.load(path)
    assert cfg.events.deploy_poll and cfg.events.deploy_repos == ("o/r",)
    assert cfg.events.stripe_port == 7795


def test_gmail_is_off_without_its_three_secrets(monkeypatch):
    for name in event_pollers.GMAIL_ENV:
        monkeypatch.delenv(name, raising=False)
    assert event_pollers.gmail_from_env() is None


def test_the_desks_line_names_only_the_sources_that_are_on(monkeypatch):
    from server import features
    monkeypatch.setattr(deckconfig, "load", lambda *a, **k: deckconfig.DeckConfig())
    off = features.events_line()
    assert "failed GitHub deploys" not in off and "Stripe" not in off
    on = deckconfig.DeckConfig(events=deckconfig.EventsSection(
        deploy_poll=True, deploy_repos=("o/r",)))
    monkeypatch.setattr(deckconfig, "load", lambda *a, **k: on)
    assert "failed GitHub deploys" in features.events_line()
    assert "do not poll" in features.events_line().lower()
