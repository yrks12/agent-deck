"""shaliach.me visitor counting (docs/site/stats.js).

No account, no cookies, no storage: each page view bumps a few public counters
(abacus, no signup) and, once a GoatCounter code is pasted into the file, also
reports to GoatCounter. The pure part -- which counters one page view bumps -- is
tested here through node; the pages are checked to load the script.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "docs" / "site"
STATS = SITE / "stats.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def keys(url: str, referrer: str = "", day: str = "2026-10-02") -> list[str]:
    js = (
        f"const s=require({json.dumps(str(STATS))});"
        f"console.log(JSON.stringify(s.keysFor({json.dumps(url)},{json.dumps(referrer)},{json.dumps(day)})))"
    )
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_a_linkedin_arrival_on_the_home_page_is_a_view_a_visit_and_a_linkedin_referral():
    got = keys("https://shaliach.me/", "https://www.linkedin.com/")
    assert sorted(got) == sorted(["views-2026-10-02", "page-home", "visits-2026-10-02", "ref-linkedin"])


@pytest.mark.parametrize("referrer,cls", [
    ("https://lnkd.in/abc", "linkedin"),
    ("https://news.ycombinator.com/item?id=1", "hn"),
    ("https://out.reddit.com/t3_x", "reddit"),
    ("https://www.reddit.com/r/ClaudeAI/", "reddit"),
    ("https://t.co/xyz", "x"),
    ("https://x.com/someone", "x"),
    ("https://github.com/yrks12/shaliach", "github"),
    ("https://www.google.com/", "google"),
    ("https://example.org/post", "other"),
    ("", "direct"),
])
def test_the_referrer_is_sorted_into_one_bucket(referrer, cls):
    assert f"ref-{cls}" in keys("https://shaliach.me/videos.html", referrer)


def test_a_utm_source_names_the_bucket_when_the_app_strips_the_referrer():
    assert "ref-linkedin" in keys("https://shaliach.me/?utm_source=linkedin")
    assert "ref-hn" in keys("https://shaliach.me/?ref=hn")


def test_moving_between_pages_of_the_site_is_a_view_but_not_a_new_visit():
    got = keys("https://shaliach.me/videos.html", "https://shaliach.me/")
    assert sorted(got) == ["page-videos", "views-2026-10-02"]


def test_previews_and_local_copies_count_nothing():
    assert keys("http://localhost:8000/", "") == []
    assert keys("file:///tmp/index.html", "") == []


@pytest.mark.parametrize("page", ["index.html", "videos.html"])
def test_every_page_loads_the_counter(page):
    assert '<script src="stats.js" defer></script>' in (SITE / page).read_text(encoding="utf-8")


def test_goatcounter_stays_off_until_a_code_is_pasted():
    text = STATS.read_text(encoding="utf-8")
    assert 'var GOATCOUNTER = "";' in text or 'var GOATCOUNTER = "' in text
    assert "gc.zgo.at/count.js" in text
