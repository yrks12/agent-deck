"""His screenshots and files reach the desk -- from the phone, which has no path
the desk can read.

Owner, 2026-09-30: "On the phone I can't add or paste screenshots or files."

The Mac's [+] puts a local path in the text and `api.attachments` derives the
chip from it (§9). A phone path means nothing on the box, so the bytes have to
travel. The contract, kept as small as the voice-note one:

* `POST /v1/threads/{id}/attachments` -- the file is the raw body,
  `Content-Type` its type, `X-Deck-Filename` its name (percent-encoded). It is
  stored on the box and answered with the box path the desk will read.
* The app then sends an ORDINARY message whose text carries that path
  (`Attached image: <path>`), so the desk reads it like any path -- Claude
  Code's Read tool shows it the picture -- and the Mac sees the same chip.
* The derived attachment for such a path gains a `url`, and
  `GET /v1/attachments/{id}/{name}` serves the bytes back so his bubble can
  draw the picture he sent.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import features, office
from server import uploads

TOKEN = "t-secret-not-a-real-credential"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DESK = {"name": "atlas", "cwd": "/tmp/p", "engine": "claude", "mission": "run",
        "label": "Chief", "charter": "Own it.", "reports_to": None}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class Rig:
    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
        monkeypatch.setattr(office, "BUS_DIR", tmp_path)
        (tmp_path / "messages.jsonl").write_text("")
        (tmp_path / "roster.json").write_text(
            json.dumps({"version": 1, "agents": [DESK]}))
        monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
        self.dir = tmp_path
        self.delivered = []

        def deliver(name, text):
            self.delivered.append((name, text))
            return True

        app = FastAPI()
        surface = api_mod.register(app, surface=api_mod.Surface(
            snapshot=lambda: {"generated_at": 1.0, "sessions": []},
            deliver=deliver,
            roster_path=tmp_path / "roster.json",
            prefs_path=tmp_path / "prefs.json"), background=False)
        app.include_router(uploads.build_router(surface))
        self.client = TestClient(app)

    def upload(self, data=PNG, name="Screenshot 2026-09-30 at 10.00.png",
               mime="image/png", thread="direct:atlas", headers=None):
        from urllib.parse import quote
        h = {**(AUTH if headers is None else headers), "Content-Type": mime}
        if name is not None:
            h["X-Deck-Filename"] = quote(name)
        return self.client.post(f"/v1/threads/{thread}/attachments",
                                content=data, headers=h)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def test_an_upload_lands_on_the_box_and_answers_the_path_the_desk_reads(rig):
    r = rig.upload()
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"].startswith("att_") and len(body["id"]) == 20
    assert body["kind"] == "image"
    assert body["bytes"] == len(PNG)
    # A name the path pattern in `api.attachments` matches end to end: spaces
    # would cut the path in two in the message text.
    assert body["name"] == "Screenshot-2026-09-30-at-10.00.png"
    path = rig.dir / "attachments" / body["id"] / body["name"]
    assert body["path"] == str(path)
    assert path.read_bytes() == PNG
    assert body["url"] == f"/v1/attachments/{body['id']}/{body['name']}"


def test_the_bytes_come_back_for_his_bubble(rig):
    up = rig.upload().json()
    r = rig.client.get(up["url"], headers=AUTH)
    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-type"] == "image/png"


def test_a_file_that_is_not_an_image_is_a_file(rig):
    r = rig.upload(data=b"%PDF-1.7 fake", name="invoice.pdf",
                   mime="application/pdf")
    assert r.status_code == 201
    assert r.json()["kind"] == "file"
    got = rig.client.get(r.json()["url"], headers=AUTH)
    # Its real type, so QuickLook on the phone knows it is a PDF
    # (tests/test_attachment_serving.py).
    assert got.headers["content-type"] == "application/pdf"


def test_a_message_carrying_the_path_derives_an_image_with_its_url(rig):
    up = rig.upload().json()
    text = f"what is wrong here?\n\nAttached image: {up['path']}"
    sent = rig.client.post("/v1/threads/direct:atlas/messages", headers=AUTH,
                           json={"text": text, "as": "owner"})
    assert sent.status_code == 201, sent.text
    message = sent.json()["message"]
    [entry] = [a for a in message["attachments"] if a["value"] == up["path"]]
    # Plus what it is and how big (tests/test_attachment_media.py).
    assert {k: entry[k] for k in ("kind", "value", "url")} == \
        {"kind": "image", "value": up["path"], "url": up["url"]}
    # And the desk got the path itself, which its Read tool opens.
    (name, delivered), = rig.delivered
    assert name == "atlas" and up["path"] in delivered


def test_a_path_outside_the_upload_folder_gets_no_url(rig):
    found = api_mod.attachments("see /tmp/att_0123456789abcdef/x.png")
    assert found == [{"kind": "image", "value": "/tmp/att_0123456789abcdef/x.png"}]


def test_too_big_is_413_and_nothing_is_written(rig, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_BYTES", 10)
    r = rig.upload(data=b"x" * 11)
    assert r.status_code == 413
    assert r.json()["reason"] == "too_large"
    assert not (rig.dir / "attachments").exists()


def test_empty_is_refused(rig):
    r = rig.upload(data=b"")
    assert r.status_code == 400
    assert r.json()["reason"] == "empty_file"


def test_no_token_is_refused(rig):
    assert rig.upload(headers={}).status_code == 401


def test_an_unknown_desk_is_refused_before_anything_is_stored(rig):
    r = rig.upload(thread="direct:nobody")
    assert r.status_code == 404
    assert not (rig.dir / "attachments").exists()


def test_a_name_cannot_climb_out_of_its_folder(rig):
    r = rig.upload(name="../../roster.json", mime="application/json")
    assert r.status_code == 201
    body = r.json()
    assert "/" not in body["name"] and ".." not in body["name"].strip(".")
    assert (rig.dir / "attachments" / body["id"] / body["name"]).is_file()


def test_no_name_is_still_a_file_with_a_sane_name(rig):
    r = rig.upload(name=None, mime="image/jpeg")
    assert r.status_code == 201
    assert r.json()["name"] == "attachment.jpg"


def test_a_forged_id_or_name_is_404_not_a_file_read(rig):
    rig.upload()
    for url in ("/v1/attachments/att_zzzz/x.png",
                "/v1/attachments/att_0123456789abcdef/..%2Froster.json"):
        assert rig.client.get(url, headers=AUTH).status_code == 404


def test_desks_are_told_how_his_attachments_arrive():
    line = next(f for f in features.FEATURES if f.key == "attachments")
    assert "Read" in line.line and "Attached image:" in line.line
    assert features.covers_route("/v1/threads/{thread_id}/attachments")
    assert features.covers_route("/v1/attachments/{att_id}/{name}")
