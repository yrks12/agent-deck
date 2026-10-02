"""A desk sends him a file -- an image, a video, a PDF -- and it shows in his chat.

Owner, 2026-10-01: "can the chat send me videos or image files and I will see
it there?" MEASURED before this: a desk could only write a path into its
message. `api.attachments` turned that into a chip, and a chip of a path on
the box is not something his phone can open -- a render the desk made was a
string he could not see.

`mcp__deck__send_file(path, caption?)`:

* copies the bytes into the deck's attachment store (`server/uploads.py`), so
  the same authenticated, range-served route that serves his own uploads
  serves it, with a poster frame for a video;
* posts ONE message in the desk's chat with him carrying the file, so his
  bubble draws it (and it buzzes him: a file a desk sends him is for him);
* refuses a path outside the desk's own folders (its working directory, its
  deck workspace, its computer's home), resolved through symlinks, and a file
  over the cap -- with the next step in the refusal.
"""

import json
import os

import pytest

from server import api as api_mod
from server import deck_mcp, decisions, features, office, owner_alerts, uploads
from tests.test_deck_mcp import records, tool

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(decisions, "DEFAULT_PATH", tmp_path / "decisions.json")
    monkeypatch.setattr(uploads, "make_preview", lambda src, **kw: None)
    work = tmp_path / "repo"
    work.mkdir()
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1, "agents": [
        {"name": "atlas", "cwd": str(work), "engine": "claude",
         "mission": "run", "reports_to": None},
        {"name": "scout", "cwd": str(tmp_path / "workspaces" / "scout"),
         "engine": "claude", "mission": "look", "reports_to": "atlas"},
    ]}))
    (tmp_path / "workspaces" / "scout").mkdir(parents=True)
    monkeypatch.setattr(deck_mcp, "ROSTER_PATH", roster)
    return tmp_path


def test_the_tool_is_offered_with_path_required_and_caption_optional():
    spec = next(t for t in deck_mcp.TOOLS if t["name"] == "send_file")
    assert spec["inputSchema"]["required"] == ["path"]
    assert set(spec["inputSchema"]["properties"]) == {"path", "caption"}


def test_an_image_lands_in_the_store_and_one_message_carries_it(bus):
    src = bus / "repo" / "chart.png"
    src.write_bytes(PNG)
    out, err = tool("atlas", "send_file", path=str(src), caption="Q3 chart")
    assert not err, out
    stored = uploads.folder() / out["id"] / "chart.png"
    assert stored.read_bytes() == PNG
    assert out["url"] == f"/v1/attachments/{out['id']}/chart.png"
    assert out["media"] == "image" and out["bytes"] == len(PNG)

    (record,) = records(bus)
    assert record["to"] == office.OWNER_INBOX and record["from"] == "atlas"
    assert record["text"] == f"Q3 chart\n\nAttached image: {stored}"
    assert record["file"]["id"] == out["id"]
    [entry] = api_mod.attachments(record["text"])
    assert entry["url"] == out["url"] and entry["media"] == "image"


def test_a_video_is_named_a_video_and_gets_its_poster(bus, monkeypatch):
    made = []
    monkeypatch.setattr(uploads, "make_preview",
                        lambda src, **kw: made.append(src))
    src = bus / "repo" / "demo reel.mp4"
    src.write_bytes(b"\x00" * 100)
    out, err = tool("atlas", "send_file", path=str(src))
    assert not err, out
    assert out["media"] == "video"
    stored = uploads.folder() / out["id"] / "demo-reel.mp4"
    assert made == [stored]
    (record,) = records(bus)
    assert record["text"] == f"Attached video: {stored}"


def test_a_relative_path_is_from_the_desks_own_folder(bus):
    (bus / "repo" / "out").mkdir()
    (bus / "repo" / "out" / "report.pdf").write_bytes(b"%PDF-1.7")
    out, err = tool("atlas", "send_file", path="out/report.pdf")
    assert not err, out
    assert out["media"] == "pdf"
    assert records(bus)[0]["text"].startswith("Attached file: ")


def test_a_junior_desk_sends_into_its_own_chat_with_him(bus):
    src = bus / "workspaces" / "scout" / "find.png"
    src.write_bytes(PNG)
    out, err = tool("scout", "send_file", path=str(src))
    assert not err, out
    (record,) = records(bus)
    assert record["to"] == office.OWNER_INBOX and record["from"] == "scout"


def test_its_computers_home_is_one_of_its_folders(bus):
    home = bus / "browser" / "computers" / "scout" / "Downloads"
    home.mkdir(parents=True)
    (home / "invoice.pdf").write_bytes(b"%PDF")
    out, err = tool("scout", "send_file", path=str(home / "invoice.pdf"))
    assert not err, out


@pytest.mark.parametrize("where", ["/etc/passwd", "elsewhere"])
def test_a_path_outside_its_folders_is_refused(bus, where):
    if where == "elsewhere":
        (bus / "elsewhere").write_bytes(b"secret")
        where = str(bus / "elsewhere")
    out, err = tool("scout", "send_file", path=where)
    assert err and out["reason"] == "outside_your_folders"
    assert str(bus / "workspaces" / "scout") in out["detail"]
    assert records(bus) == [] and not uploads.folder().exists()


def test_another_desks_folder_is_outside_yours(bus):
    (bus / "repo" / "plan.pdf").write_bytes(b"%PDF")
    out, err = tool("scout", "send_file", path=str(bus / "repo" / "plan.pdf"))
    assert err and out["reason"] == "outside_your_folders"


def test_a_symlink_out_of_its_folder_is_refused(bus):
    (bus / "secret.txt").write_text("token")
    link = bus / "repo" / "innocent.txt"
    os.symlink(bus / "secret.txt", link)
    out, err = tool("atlas", "send_file", path=str(link))
    assert err and out["reason"] == "outside_your_folders"


def test_dot_dot_cannot_climb_out(bus):
    (bus / "secret.txt").write_text("token")
    out, err = tool("atlas", "send_file", path=str(bus / "repo" / ".." / "secret.txt"))
    assert err and out["reason"] == "outside_your_folders"


@pytest.mark.parametrize("make,reason", [
    (lambda p: None, "no_such_file"),
    (lambda p: p.mkdir(), "not_a_file"),
    (lambda p: p.write_bytes(b""), "empty_file"),
])
def test_what_is_not_a_sendable_file_says_so(bus, make, reason):
    target = bus / "repo" / "thing"
    make(target)
    out, err = tool("atlas", "send_file", path=str(target))
    assert err and out["reason"] == reason


def test_over_the_cap_is_refused_with_how_to_shrink_it(bus, monkeypatch):
    monkeypatch.setattr(deck_mcp, "SEND_FILE_MAX", 10)
    (bus / "repo" / "big.mp4").write_bytes(b"x" * 11)
    out, err = tool("atlas", "send_file", path=str(bus / "repo" / "big.mp4"))
    assert err and out["reason"] == "too_large"
    assert "ffmpeg" in out["detail"] and "compress" in out["detail"]
    assert records(bus) == [] and not uploads.folder().exists()


def test_the_cap_is_about_200_mb():
    assert deck_mcp.SEND_FILE_MAX == 200 * 1024 * 1024


def test_a_long_caption_is_refused(bus):
    (bus / "repo" / "a.png").write_bytes(PNG)
    out, err = tool("atlas", "send_file", path=str(bus / "repo" / "a.png"),
                    caption="x" * (deck_mcp.CAPTION_MAX + 1))
    assert err and out["reason"] == "too_long"


def test_an_unknown_desk_sends_nothing(bus):
    (bus / "repo" / "a.png").write_bytes(PNG)
    out, err = tool("ghost", "send_file", path=str(bus / "repo" / "a.png"))
    assert err and out["reason"] == "unknown_desk"


# ── he hears about it, in words that say what it is ─────────────────────────


def test_a_file_a_desk_sends_him_is_for_him():
    c = owner_alerts.Classifier()
    record = {"to": office.OWNER_INBOX, "from": "atlas", "said": True,
              "text": "x", "file": {"id": "att_0123456789abcdef"}}
    assert c.classify(record) == owner_alerts.FOR_YOU


@pytest.mark.parametrize("name,said", [
    ("demo.mp4", "sent you a video"), ("shot.png", "sent you an image"),
    ("memo.m4a", "sent you an audio clip"), ("q3.pdf", "sent you a PDF"),
    ("data.zip", "sent you a file"),
])
def test_the_push_says_what_was_sent(name, said):
    att = [{"kind": "file", "value": "/x", "url": "/v1/attachments/a/" + name,
            "media": uploads.media_of(name)}]
    assert api_mod.alert_body(f"Attached file: /x", att) == said
    assert api_mod.alert_body(f"Look\n\nAttached file: /x", att) == \
        f"{said}: Look"


def test_a_message_with_no_held_file_keeps_its_words():
    assert api_mod.alert_body("Deployed.", []) == "Deployed."


# ── every desk knows the tool exists ─────────────────────────────────────────


def test_desks_are_told_they_can_send_him_files():
    entry = next(f for f in features.FEATURES if f.key == "send_file")
    assert entry.tools == ("mcp__deck__send_file",)
    assert "200 MB" in entry.line and "video" in entry.line
    assert "mcp__deck__send_file" in features.section()
