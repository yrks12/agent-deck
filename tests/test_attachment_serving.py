"""A file an agent sends him is served so his phone can PLAY it, not just name it.

Owner, 2026-10-01: "can the chat send me videos or image files and I will see
it there?"

`GET /v1/attachments/{id}/{name}` served every non-image as
`application/octet-stream`, whole, with no `Accept-Ranges`. iOS AVPlayer
refuses to play a video from a server that does not answer byte ranges
(it asks `Range: bytes=0-1` first), and QuickLook on the phone decides what a
file is from its content type. So:

* each attachment carries its real content type (video, audio, PDF, text),
  and anything a browser would execute (HTML, SVG) is still octet-stream;
* a `Range` request answers 206 with `Content-Range`, and every answer says
  `Accept-Ranges: bytes`;
* the same auth as before.
"""

import pytest

from server import uploads
from tests.test_owner_attachments import AUTH, Rig

MP4 = bytes(range(256)) * 40  # 10240 bytes, every offset distinguishable


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def _put(rig, data, name):
    att = "att_0123456789abcdef"
    path = rig.dir / "attachments" / att / name
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    return f"/v1/attachments/{att}/{name}"


@pytest.mark.parametrize("name,mime", [
    ("clip.mp4", "video/mp4"),
    ("clip.mov", "video/quicktime"),
    ("clip.m4v", "video/x-m4v"),
    ("clip.webm", "video/webm"),
    ("song.mp3", "audio/mpeg"),
    ("memo.m4a", "audio/mp4"),
    ("take.wav", "audio/wav"),
    ("report.pdf", "application/pdf"),
    ("shot.png", "image/png"),
    ("notes.txt", "text/plain; charset=utf-8"),
])
def test_each_attachment_carries_its_real_content_type(rig, name, mime):
    r = rig.client.get(_put(rig, b"abc", name), headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"] == mime
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name", ["page.html", "page.htm", "logo.svg",
                                  "thing.bin"])
def test_what_a_browser_would_execute_is_never_served_as_itself(rig, name):
    r = rig.client.get(_put(rig, b"<script>1</script>", name), headers=AUTH)
    assert r.headers["content-type"] == "application/octet-stream"


def test_a_range_answers_206_with_exactly_those_bytes(rig):
    url = _put(rig, MP4, "clip.mp4")
    r = rig.client.get(url, headers={**AUTH, "Range": "bytes=0-1"})
    assert r.status_code == 206
    assert r.content == MP4[:2]
    assert r.headers["content-range"] == f"bytes 0-1/{len(MP4)}"

    r = rig.client.get(url, headers={**AUTH, "Range": "bytes=5000-"})
    assert r.status_code == 206
    assert r.content == MP4[5000:]
    assert r.headers["content-range"] == f"bytes 5000-{len(MP4) - 1}/{len(MP4)}"
    assert r.headers["content-type"] == "video/mp4"


def test_a_range_past_the_end_is_416(rig):
    url = _put(rig, MP4, "clip.mp4")
    r = rig.client.get(url, headers={**AUTH, "Range": f"bytes={len(MP4)}-"})
    assert r.status_code == 416


def test_a_range_still_needs_the_token(rig):
    url = _put(rig, MP4, "clip.mp4")
    assert rig.client.get(url, headers={"Range": "bytes=0-1"}).status_code == 401


def test_the_type_table_is_one_function():
    assert uploads.content_type("A.MP4") == "video/mp4"
    assert uploads.content_type("noext") == "application/octet-stream"
