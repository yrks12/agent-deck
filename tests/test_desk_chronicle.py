"""The chronicle: "everything since day one" in one cheap, durable read.

Owner, 2026-10-01: Atlas could not answer it and offered a document "tomorrow
morning". The chronicle is one dated line per desk per day, derived from the
deck's record with no model call, and a closed day is sealed to disk.
"""

import json

import pytest

from server import chronicle, history, learning, office

DAY1 = 1788700000.0          # 2026-09-06
DAY2 = DAY1 + 86400 * 20     # 2026-09-26
NOW = DAY2 + 3600
ROSTER = [
    {"name": "atlas", "cwd": "/tmp/a", "engine": "claude", "mission": "run",
     "reports_to": None},
    {"name": "scout", "cwd": "/tmp/s", "engine": "claude", "mission": "look",
     "reports_to": "atlas"},
]


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(learning, "TEAM_DIR", tmp_path / "team-memory")
    monkeypatch.setattr(chronicle, "PATH", tmp_path / "chronicle.json")
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1, "agents": ROSTER}))
    monkeypatch.setattr(history, "ROSTER_PATH", roster)
    _write(office.MESSAGES_FILE, [
        {"ts": DAY1, "id": "a", "to": "atlas", "from": "owner",
         "text": "Get acme.example.com to its first paying user"},
        {"ts": DAY1 + 9, "id": "n", "to": "atlas", "from": "deck",
         "text": "Approved: ask 1. You may now run Bash"},
        {"ts": DAY1 + 60, "id": "b", "to": "owner", "from": "atlas",
         "text": "Shipped the acme pricing page and hired a growth desk"},
        {"ts": DAY2, "id": "c", "to": "scout", "from": "atlas",
         "text": "Check the acme signup"},
        {"ts": DAY2 + 60, "id": "d", "to": "owner", "from": "scout",
         "text": "Signup works end to end"},
    ])
    _write(tmp_path / "events.jsonl", [
        {"ts": DAY1 - 10, "event": "hire", "name": "atlas",
         "reports_to": None, "by": "owner"}])
    return tmp_path


def test_day_one_is_in_the_chronicle_with_what_he_asked_and_what_was_done(bus):
    out = chronicle.read("atlas", desk="atlas", now=NOW)
    assert out["first"] == "2026-09-06"
    day1 = out["lines"][0]
    assert day1.startswith("2026-09-06 atlas:")
    assert "hired atlas" in day1
    assert "Get acme.example.com to its first paying user" in day1
    assert "Shipped the acme pricing page" in day1
    assert "Approved: ask 1" not in day1          # the deck's noise is not his ask
    assert any("Worked with scout" in line for line in out["lines"])


def test_a_closed_day_is_sealed_and_survives_the_logs(bus):
    chronicle.read("atlas", now=NOW)
    sealed = json.loads((bus / "chronicle.json").read_text())["days"]
    assert "2026-09-06" in sealed["atlas"]
    assert "2026-09-26" not in sealed.get("scout", {})   # today stays live
    office.MESSAGES_FILE.write_text("")                    # the log is trimmed
    out = chronicle.read("atlas", desk="atlas", now=NOW)
    assert out["lines"][0].startswith("2026-09-06 atlas:")


def test_the_chief_reads_the_team_and_a_desk_reads_its_own(bus):
    team = chronicle.read("atlas", now=NOW)
    assert {line.split(":")[0].split()[1] for line in team["lines"]} == {
        "atlas", "scout"}
    own = chronicle.read("scout", now=NOW)
    assert all(" scout:" in line for line in own["lines"])
    with pytest.raises(history.HistoryError):
        chronicle.read("scout", desk="atlas", now=NOW)


def test_backfill_cli_seals_without_a_model(bus, capsys):
    assert chronicle.main(["--backfill"]) == 0
    assert (bus / "chronicle.json").exists()
    assert "desk-days" in capsys.readouterr().out
