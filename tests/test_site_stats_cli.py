"""bin/site-stats: the shaliach.me numbers in plain lines.

The fetching is thin (gh api + the abacus counters); what is tested is that the
numbers that come back are added up and worded right.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    loader = importlib.machinery.SourceFileLoader("site_stats", str(ROOT / "bin" / "site-stats"))
    spec = importlib.util.spec_from_loader("site_stats", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


GH = {
    "views": {"count": 36, "uniques": 1, "views": [
        {"timestamp": "2026-10-01T00:00:00Z", "count": 35, "uniques": 1},
        {"timestamp": "2026-10-02T00:00:00Z", "count": 4, "uniques": 2}]},
    "clones": {"count": 79, "uniques": 4, "clones": []},
    "referrers": [{"referrer": "linkedin.com", "count": 3, "uniques": 2}],
    "repo": {"stargazers_count": 5, "forks_count": 1},
    "releases": [
        {"tag_name": "v0.10.0", "assets": [
            {"name": "Shaliach-0.10.0-macos.zip", "download_count": 7},
            {"name": "Shaliach-0.10.0-macos.zip.sha256", "download_count": 9}]},
        {"tag_name": "site-videos", "assets": [{"name": "a.mp4", "download_count": 50}]},
    ],
}


def test_github_lines_say_views_clones_stars_downloads_and_referrers():
    lines = _load().github_lines(GH, today="2026-10-02")
    text = "\n".join(lines)
    assert "repo views (14 days): 36 (1 unique); today 4 (2 unique)" in text
    assert "repo clones (14 days): 79 (4 unique)" in text
    assert "stars: 5  forks: 1" in text
    # only the app downloads count -- checksums and the site's own video files are not installs
    assert "app downloads: 7 (v0.10.0: 7)" in text
    assert "linkedin.com 3 (2 unique)" in text


def test_site_lines_add_up_the_days_and_name_every_bucket():
    counts = {
        "views-2026-10-01": 0, "views-2026-10-02": 12, "visits-2026-10-02": 5,
        "page-home": 10, "page-videos": 2,
        "ref-linkedin": 4, "ref-direct": 1, "ref-hn": 0,
        "copy-install-server": 2, "copy-install-mac": 0,
    }
    lines = _load().site_lines(counts, days=["2026-10-01", "2026-10-02"])
    text = "\n".join(lines)
    assert "site views: 12 total; today 12" in text
    assert "site visits (arrivals from outside): 5 total; today 5" in text
    assert "pages: home 10, videos 2" in text
    assert "came from: linkedin 4, direct 1" in text
    assert "copied install command: server 2, mac 0" in text
