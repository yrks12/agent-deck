"""The web board labels the plan meter unofficial, like the API and the apps.

The meter reads an undocumented Anthropic endpoint and is on by default (owner
ruling 2026-10-01), so every client that draws it says so.
"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def test_the_board_draws_an_unofficial_label_beside_the_meters():
    html = (WEB / "index.html").read_text()
    m = re.search(r'id="meters"></div>\s*<div class="meters__note"[^>]*>([^<]*)</div>', html)
    assert m, "no label right after the meters"
    assert "unofficial" in m.group(1).lower()
    assert ".meters__note" in (WEB / "style.css").read_text()
