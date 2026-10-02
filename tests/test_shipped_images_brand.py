"""Shipped pictures carry the product's current name, never the old one.

The product was renamed from "Agent Deck" to Shaliach. The README, the docs and the
social card show screenshots, a GIF and posters from ``docs/images`` and
``docs/media``; a picture that still says "Agent Deck" in a window title or a label
is a stale brand that text search cannot see. This detector reads every shipped
picture with OCR (tesseract) and fails on the old name.

How it reads a picture, so a dark title bar is not missed:

* each frame is read in grayscale and inverted grayscale,
* and again in overlapping horizontal bands, because tesseract's page layout
  analysis sometimes drops a lone title over a busy screenshot;
* a GIF is read on its first frame plus a few frames spread across it.

The match tolerates OCR spacing ("AgentDeck", "Agent  Deck", "Agent-Deck").
``ALLOWED`` lists a picture that intentionally says the old name (for example a
"formerly Agent Deck" note) with the reason; it is empty on purpose.

Needs the ``tesseract`` binary and Pillow. Without either the test skips, so a CI
runner that has no OCR stays green; run it locally before shipping new pictures.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IMAGE_DIRS = (ROOT / "docs" / "images", ROOT / "docs" / "media")
SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}

# relative path -> why this picture may say the old name. Keep it empty unless a
# picture deliberately reads "formerly Agent Deck".
ALLOWED: dict[str, str] = {}

OLD_NAME = re.compile(r"a\s*g\s*e\s*n\s*t[\s\-_.]{0,3}d\s*e\s*c\s*k", re.IGNORECASE)

GIF_SAMPLES = 4  # frames read besides the first
BANDS = 14       # horizontal bands per frame (each overlaps the next by half)


def _tesseract() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
        if Path(candidate).exists():
            return candidate
    return None


def _shipped_images() -> list[Path]:
    return sorted(p for d in IMAGE_DIRS if d.is_dir() for p in d.rglob("*")
                  if p.is_file() and p.suffix.lower() in SUFFIXES)


def _frames(path: Path, Image):
    """The frames to read: one for a still, the first plus a spread for a GIF."""
    im = Image.open(path)
    count = getattr(im, "n_frames", 1)
    if count <= 1:
        return [im.convert("RGB")]
    picks = sorted({0, *(round(i * (count - 1) / GIF_SAMPLES) for i in range(1, GIF_SAMPLES + 1))})
    out = []
    for i in picks:
        im.seek(i)
        out.append(im.convert("RGB"))
    return out


def _views(frame, ImageOps):
    """Grayscale and inverted, whole and in overlapping bands."""
    gray = ImageOps.grayscale(frame)
    views = [gray, ImageOps.invert(gray)]
    w, h = gray.size
    band = max(1, (2 * h) // (BANDS + 1))
    step = max(1, band // 2)
    for top in range(0, max(1, h - step), step):
        views.append(gray.crop((0, top, w, min(h, top + band))))
    return views


def _ocr(tesseract: str, image, workdir: Path, n: int) -> str:
    target = workdir / f"v{n}.png"
    image.save(target)
    done = subprocess.run([tesseract, str(target), "-"], capture_output=True, text=True, timeout=120)
    return done.stdout


def old_name_hits(path: Path, tesseract: str) -> list[str]:
    """Every OCR'd snippet of the old name in this picture (empty when clean)."""
    from PIL import Image, ImageOps

    views = [v for f in _frames(path, Image) for v in _views(f, ImageOps)]
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=6) as pool:
        texts = list(pool.map(lambda nv: _ocr(tesseract, nv[1], Path(tmp), nv[0]), enumerate(views)))
    hits = []
    for text in texts:
        hits += [m.group(0) for m in OLD_NAME.finditer(text)]
    return sorted(set(hits))


def test_the_matcher_tolerates_ocr_spacing():
    for seen in ("Agent Deck", "AgentDeck", "agent  deck", "Agent-Deck (fixture)", "A gent Deck"):
        assert OLD_NAME.search(seen), seen
    for clean in ("Shaliach", "Agent screen", "a deck", "Agents decide"):
        assert not OLD_NAME.search(clean), clean


def test_the_allowlist_names_real_pictures():
    for rel in ALLOWED:
        assert (ROOT / rel).is_file(), f"ALLOWED names a missing picture: {rel}"


@pytest.mark.parametrize("path", _shipped_images(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_shipped_picture_says_the_old_name(path: Path):
    tesseract = _tesseract()
    if tesseract is None:
        pytest.skip("tesseract is not installed; the OCR brand check runs where it is")
    pytest.importorskip("PIL", reason="Pillow is not installed; the OCR brand check needs it")
    rel = str(path.relative_to(ROOT))
    if rel in ALLOWED:
        pytest.skip(f"allowed: {ALLOWED[rel]}")
    hits = old_name_hits(path, tesseract)
    assert not hits, f"{rel} still shows the old product name: {hits}"
