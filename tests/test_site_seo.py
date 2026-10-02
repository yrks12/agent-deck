"""shaliach.me (docs/site) is findable: by Google, by Bing, and by AI answer engines.

Every page carries the tags a search result and a link preview are built from, the
structured data parses, the sitemap lists every page, robots.txt lets the AI crawlers
in, llms.txt describes the product in plain words, every image has alt text, and every
page loads the visitor counter.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "docs" / "site"
BASE = "https://shaliach.me/"
PAGES = sorted(p for p in SITE.rglob("*.html") if "videos" not in p.relative_to(SITE).parts[:-1])
AI_CRAWLERS = ["Googlebot", "Google-Extended", "Bingbot", "GPTBot", "OAI-SearchBot", "ChatGPT-User",
               "ClaudeBot", "Claude-SearchBot", "Claude-User", "PerplexityBot"]
COMPARISONS = ["alternatives", "vs/openai-dots", "vs/grok", "vs/meta-muse", "vs/devin", "vs/openclaw"]


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta, self.links, self.imgs, self.scripts = {}, {}, [], []
        self.h1 = 0
        self.title = ""
        self._in, self._buf, self._type = None, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = a.get("name") or a.get("property")
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link" and a.get("rel"):
            self.links.setdefault(a["rel"], a.get("href", ""))
        elif tag == "img":
            self.imgs.append(a)
        elif tag == "h1":
            self.h1 += 1
        elif tag in ("title", "script"):
            self._in, self._buf, self._type = tag, [], a.get("type")
            if tag == "script" and a.get("src"):
                self.scripts.append(("src", a["src"]))

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == self._in:
            text = "".join(self._buf)
            if tag == "title":
                self.title = text.strip()
            elif self._type == "application/ld+json":
                self.scripts.append(("ld", text))
            self._in = None


def _parse(page: Path) -> _Page:
    p = _Page()
    p.feed(page.read_text(encoding="utf-8"))
    return p


def _url(page: Path) -> str:
    rel = page.relative_to(SITE).as_posix()
    if rel == "index.html":
        return BASE
    if rel.endswith("/index.html"):
        return BASE + rel[: -len("index.html")]
    return BASE + rel


def _jsonld(p: _Page) -> list:
    out = []
    for kind, text in p.scripts:
        if kind == "ld":
            data = json.loads(text)
            out += data.get("@graph", [data]) if isinstance(data, dict) else data
    return out


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.relative_to(SITE).as_posix())
def test_every_page_has_the_tags_a_result_and_a_preview_need(page):
    p = _parse(page)
    assert 10 <= len(p.title) <= 70, f"title is {len(p.title)} chars: {p.title!r}"
    assert 50 <= len(p.meta.get("description", "")) <= 170, "meta description 50-170 chars"
    assert p.links.get("canonical") == _url(page)
    assert p.meta.get("og:url") == _url(page)
    assert p.meta.get("og:image", "").startswith(BASE), "og:image is an absolute URL on the site"
    assert p.meta.get("og:title") and p.meta.get("og:description")
    assert p.meta.get("twitter:card") == "summary_large_image"
    assert p.links.get("icon"), "favicon"
    assert p.h1 == 1, f"exactly one h1, found {p.h1}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.relative_to(SITE).as_posix())
def test_every_page_has_structured_data_that_parses(page):
    items = _jsonld(_parse(page))
    assert items, "no JSON-LD"
    assert all(isinstance(i, dict) and i.get("@type") for i in items)


def test_the_home_page_describes_the_app_the_maker_the_faq_and_the_film():
    items = _jsonld(_parse(SITE / "index.html"))
    types = {i["@type"] for i in items}
    assert {"SoftwareApplication", "Organization", "FAQPage", "VideoObject", "WebSite"} <= types
    faq = next(i for i in items if i["@type"] == "FAQPage")
    assert len(faq["mainEntity"]) >= 6
    video = next(i for i in items if i["@type"] == "VideoObject")
    for key in ("name", "description", "thumbnailUrl", "uploadDate", "contentUrl", "duration"):
        assert video.get(key), key
    html = (SITE / "index.html").read_text(encoding="utf-8")
    for q in faq["mainEntity"]:
        assert q["name"] in html, f"FAQ question not visible on the page: {q['name']}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.relative_to(SITE).as_posix())
def test_every_image_has_alt_text(page):
    for img in _parse(page).imgs:
        assert "alt" in img, f"<img src={img.get('src')}> has no alt"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.relative_to(SITE).as_posix())
def test_every_page_loads_the_visitor_counter(page):
    srcs = [s for k, s in _parse(page).scripts if k == "src"]
    assert any(s.rsplit("/", 1)[-1] == "stats.js" for s in srcs), "stats.js tracker missing"
    assert (SITE / "stats.js").is_file()


def test_the_sitemap_lists_every_page_and_nothing_else():
    xml = (SITE / "sitemap.xml").read_text(encoding="utf-8")
    listed = set(re.findall(r"<loc>([^<]+)</loc>", xml))
    assert listed == {_url(p) for p in PAGES}


def test_robots_lets_search_and_ai_crawlers_in_and_names_the_sitemap():
    robots = (SITE / "robots.txt").read_text(encoding="utf-8")
    assert f"Sitemap: {BASE}sitemap.xml" in robots
    groups = re.split(r"\n\s*\n", robots)
    for bot in AI_CRAWLERS:
        group = next((g for g in groups if re.search(rf"^User-agent:\s*{re.escape(bot)}\s*$", g, re.M)), None)
        assert group, f"robots.txt does not name {bot}"
        assert re.search(r"^Allow:\s*/\s*$", group, re.M) and not re.search(r"^Disallow:\s*/\s*$", group, re.M)


def test_llms_txt_describes_the_product_plainly():
    short = (SITE / "llms.txt").read_text(encoding="utf-8")
    full = (SITE / "llms-full.txt").read_text(encoding="utf-8")
    assert short.startswith("# Shaliach")
    for text in (short, full):
        assert "source-available" in text.lower() and "not affiliated" in text.lower()
        said = text.lower().replace("not open source", "").replace("is it open source?", "")
        assert "open source" not in said, "Shaliach is source-available, never open source"
    for path in COMPARISONS:
        assert f"{BASE}{path}/" in short


@pytest.mark.parametrize("path", COMPARISONS)
def test_every_comparison_page_exists_and_cites_its_sources(path):
    html = (SITE / path / "index.html").read_text(encoding="utf-8")
    if path != "alternatives":
        assert len(re.findall(r'href="https://(?!shaliach\.me|fonts\.g|github\.com/[^/"]+/shaliach)', html)) >= 2, "cite sources"
    said = re.sub(r"openclaw[^.<]{0,40}open[- ]source|open[- ]source[^.<]{0,40}openclaw", "", html.lower())
    said = said.replace("not open source", "").replace("is shaliach open source", "")
    assert "open source" not in said, "only OpenClaw is called open source"


def test_the_videos_page_lists_no_hebrew_video_and_opens_with_the_launch_film():
    html = (SITE / "videos.html").read_text(encoding="utf-8")
    assert "hebrew" not in html.lower() and not re.search(r"[֐-׿]", html)
    tour = re.search(r"var TOUR = \[\s*\[\"(\w+)\"", html)
    assert tour and tour.group(1) == "launch"
    assert "hebrew" not in (SITE / "sitemap.xml").read_text(encoding="utf-8").lower()
