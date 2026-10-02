"""Detector: the browser board has a Money view.

Premise (measured by reading web/): the board is static HTML + JS, so the only
way to pin the view without a browser is to read the files. Class swept: every
page carries the nav entry, the script reads /api/money, refreshes with
refresh=1, and never puts a server string into innerHTML unescaped.
"""

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
PAGES = ("index.html", "feed.html", "manager.html")


def money_js() -> str:
    path = WEB / "money.js"
    assert path.exists(), "web/money.js is missing: the board has no Money view"
    return path.read_text()


@pytest.mark.parametrize("page", PAGES)
def test_every_page_has_a_money_tab(page):
    html = (WEB / page).read_text()
    assert 'href="/#money"' in html, f"{page} has no way to reach Money"


def test_index_hosts_the_view_and_loads_the_script():
    html = (WEB / "index.html").read_text()
    assert 'id="money"' in html
    assert "/static/money.js" in html


def test_view_reads_the_money_route_and_can_refresh():
    js = money_js()
    assert "/api/money" in js
    assert "refresh=1" in js


def test_view_says_what_the_founder_needs():
    js = money_js()
    for phrase in ("Money in", "Money out", "Net", "No sales after",
                   "Warming up", "Claude work",
                   "Costs include Claude work at API prices",
                   "Exchange rates approximate", "Refresh"):
        assert phrase in js, phrase


def test_every_server_string_is_escaped_before_innerhtml():
    js = money_js()
    assert "function esc(" in js
    for line in js.splitlines():
        if "innerHTML" in line and "=" in line:
            assert "html`" in line, line


def test_template_interpolations_never_carry_raw_server_fields():
    """In every html`` template, a ${...} that reads a field off server data
    (c.name, e.desk, data.x) must sit inside esc()/money()/pct()/roiBadge()."""
    js = money_js()
    safe_call = re.compile(r"^\s*(esc|money|pct|raw|roiBadge|netClass|bigNumber|flagged)\(")
    for body in re.findall(r"html`(.*?)`(?=[;,)\s])", js, re.S):
        for expr in re.findall(r"\$\{(.*?)\}", body):
            if safe_call.match(expr):
                continue
            stripped = re.sub(r"\.(map|join|slice|filter)\b", "", expr)
            assert not re.search(r"\b[a-z]\w*\.\w+", stripped), expr


def test_the_product_is_called_shaliach():
    assert "Agent Deck" not in money_js()
