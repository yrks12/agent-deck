"""shaliach.me (docs/site): every local file a page points at is published.

The gallery videos and posters (docs/site/videos/*) are not committed: the Pages build
downloads them from the public repo's `site-videos` release, so for those the test
checks the build step instead of the disk.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "docs" / "site"
PAGES = sorted(p for p in SITE.rglob("*.html") if "videos" not in p.relative_to(SITE).parts[:-1])
REF = re.compile(r'(?:src|href|poster|data-wide|data-tall)="([^"#?]+)"')
BANNED = ["streamline your workflow"]


def _local_refs(page: Path) -> list[str]:
    refs = []
    for ref in REF.findall(page.read_text(encoding="utf-8")):
        if re.match(r"[a-z]+:", ref) or ref.startswith("//"):
            continue
        refs.append(ref)
    return refs


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.relative_to(SITE).as_posix())
def test_every_local_reference_is_published(page):
    text = page.read_text(encoding="utf-8")
    missing = []
    for ref in _local_refs(page):
        if ref.startswith("videos/"):
            continue  # fetched from the site-videos release at build time
        if ref.endswith("/"):
            ref += "index.html"
        base = SITE if ref.startswith("/") else page.parent
        ref = ref.lstrip("/")
        stems = [ref] if "." in ref.rsplit("/", 1)[-1] or ref == "install" else [ref + ".mp4", ref + ".jpg"]
        missing += [s for s in stems if not (base / s).resolve().is_file()]
    assert not missing, f"{page.name} points at files that are not in docs/site: {missing}"
    assert not any(b in text.lower() for b in BANNED)


def test_the_gallery_videos_are_fetched_from_the_release_at_build():
    wf = (ROOT / ".github" / "workflows" / "pages.yml").read_text()
    assert "gh release download site-videos" in wf
    assert "-D docs/site/videos" in wf


def test_the_hero_film_is_web_sized():
    for name in ("hero-16x9.mp4", "hero-9x16.mp4"):
        size = (SITE / "media" / name).stat().st_size
        assert size < 8_000_000, f"{name} is {size} bytes; re-run scripts/site-hero.sh"


def test_the_home_page_plays_the_hero_muted_on_a_loop_with_a_sound_button():
    text = (SITE / "index.html").read_text(encoding="utf-8")
    video = re.search(r"<video[^>]*>", text).group(0)
    for attr in ("autoplay", "muted", "loop", "playsinline", 'poster="media/hero-16x9.jpg"'):
        assert attr in video, attr
    assert 'id="sound"' in text
