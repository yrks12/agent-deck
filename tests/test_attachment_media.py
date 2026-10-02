"""A held attachment says what it is, how big, and where its picture is.

Owner, 2026-10-01: "can the chat send me videos or image files and I will see
it there?" A path chip cannot play. The apps need, per attachment the deck
holds: what kind of media it is (image, video, audio, pdf, file), its content
type and size (to say "12 MB" before he taps), and a small picture to draw
before anything is downloaded -- a video's poster frame, a big image's
thumbnail. The poster is made once, with ffmpeg, on the box.

`kind` stays `image`/`file`/`link`: a client built before this decodes `kind`
as a closed enum, and a new value there would drop the whole message.
"""

import shutil
import subprocess

import pytest

from server import api as api_mod
from server import uploads
from tests.test_owner_attachments import Rig

ATT = "att_0123456789abcdef"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def _held(rig, name, data=b"x" * 2048):
    path = rig.dir / "attachments" / ATT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("name,media", [
    ("a.png", "image"), ("a.JPG", "image"), ("a.mp4", "video"),
    ("a.mov", "video"), ("a.webm", "video"), ("a.mp3", "audio"),
    ("a.m4a", "audio"), ("a.pdf", "pdf"), ("a.zip", "file"), ("noext", "file"),
])
def test_media_of_names_what_the_app_should_draw(name, media):
    assert uploads.media_of(name) == media


def test_a_held_video_derives_media_type_size_and_poster(rig):
    path = _held(rig, "demo.mp4")
    (path.parent / "demo.mp4.poster.jpg").write_bytes(b"jpg")
    [entry] = api_mod.attachments(f"here it is\n\nAttached video: {path}")
    assert entry == {
        "kind": "file", "value": str(path),
        "url": f"/v1/attachments/{ATT}/demo.mp4",
        "media": "video", "mime": "video/mp4", "bytes": 2048,
        "name": "demo.mp4",
        "preview_url": f"/v1/attachments/{ATT}/demo.mp4.poster.jpg",
    }


def test_a_held_image_keeps_kind_image_and_has_no_preview_until_one_is_made(rig):
    path = _held(rig, "shot.png")
    [entry] = api_mod.attachments(f"Attached image: {path}")
    assert entry["kind"] == "image" and entry["media"] == "image"
    assert entry["mime"] == "image/png"
    assert "preview_url" not in entry


def test_a_path_the_deck_does_not_hold_gains_nothing(rig):
    assert api_mod.attachments("see /tmp/x.mp4") == [
        {"kind": "file", "value": "/tmp/x.mp4"}]


def test_a_held_path_whose_file_is_gone_still_has_its_url(rig):
    path = rig.dir / "attachments" / ATT / "gone.mp4"
    [entry] = api_mod.attachments(f"Attached video: {path}")
    assert entry["url"] == f"/v1/attachments/{ATT}/gone.mp4"
    assert entry["media"] == "video" and "bytes" not in entry


# ── the poster / thumbnail, made with ffmpeg ─────────────────────────────────


def test_a_video_poster_is_one_frame_scaled_down(tmp_path):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        open(argv[-1], "wb").write(b"jpg")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    src = tmp_path / "clip.mp4"
    src.write_bytes(b"v")
    out = uploads.make_preview(src, run=run, ffmpeg="/usr/bin/ffmpeg")
    assert out == tmp_path / "clip.mp4.poster.jpg" and out.read_bytes() == b"jpg"
    [argv] = seen
    assert argv[0] == "/usr/bin/ffmpeg" and "-frames:v" in argv
    assert str(src) in argv and argv[-1] == str(out)


def test_a_small_image_needs_no_thumbnail(tmp_path):
    src = tmp_path / "small.png"
    src.write_bytes(b"p" * 1000)
    assert uploads.make_preview(src, run=None, ffmpeg="/usr/bin/ffmpeg") is None


def test_no_ffmpeg_means_no_preview_never_an_error(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"v")
    assert uploads.make_preview(src, ffmpeg=None) is None


def test_ffmpeg_failing_leaves_no_half_written_poster(tmp_path):
    def run(argv, **kw):
        open(argv[-1], "wb").write(b"half")
        return subprocess.CompletedProcess(argv, 1, b"", b"bad")

    src = tmp_path / "clip.mp4"
    src.write_bytes(b"v")
    assert uploads.make_preview(src, run=run, ffmpeg="/usr/bin/ffmpeg") is None
    assert not (tmp_path / "clip.mp4.poster.jpg").exists()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="no ffmpeg here")
def test_a_real_short_video_gets_a_real_jpeg_poster(tmp_path):
    src = tmp_path / "real.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=10:duration=0.3",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    out = uploads.make_preview(src)
    assert out is not None and out.read_bytes()[:2] == b"\xff\xd8"
