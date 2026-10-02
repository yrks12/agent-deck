"""Detector: the browser board has a Standing approvals screen.

Premise (measured by reading web/ and docs/client-api.md section 23): the board
is static HTML + JS, so the pure parts of web/standing.js run under node and the
wiring is pinned by reading the files. Class swept: every board page links the
screen; every refusal `reason` in the contract reads as plain words; every
server string is escaped before innerHTML.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
PAGES = ("index.html", "feed.html", "manager.html", "events.html")
REASONS = ("bad_kind", "missing_field", "no_limit", "bad_limit", "too_wide",
           "bad_policy", "unknown_policy", "unknown_ask", "never_coverable",
           "not_proposed", "revoked", "no_desk")

node = pytest.mark.skipif(shutil.which("node") is None, reason="node missing")


def js_path() -> Path:
    path = WEB / "standing.js"
    assert path.exists(), "web/standing.js is missing: no Standing approvals screen"
    return path


def call(expr: str):
    script = (f"const S = require({json.dumps(str(js_path()))});"
              f"process.stdout.write(JSON.stringify({expr}));")
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.parametrize("page", PAGES)
def test_every_page_links_the_screen(page):
    assert 'href="/#standing"' in (WEB / page).read_text(), page


def test_index_hosts_the_view_the_badge_and_the_script():
    html = (WEB / "index.html").read_text()
    assert 'id="standing"' in html
    assert 'id="standing-badge"' in html
    assert "/static/standing.js" in html


@node
def test_usage_reads_like_a_person_would_say_it():
    row = {"kind": "send_email", "limits": {"count_per_day": 80, "usd_per_day": None},
           "usage": {"count": 43, "usd": 0.0}}
    assert call(f"S.usageLines({json.dumps(row)})") == ["43/80 emails today"]
    row = {"kind": "spend_money", "limits": {"count_per_day": None, "usd_per_day": 5},
           "usage": {"count": 2, "usd": 1.2}}
    assert call(f"S.usageLines({json.dumps(row)})") == ["$1.20/$5.00 today"]


@node
def test_both_limits_show_both_lines_and_no_limit_shows_none():
    row = {"kind": "run_command", "limits": {"count_per_day": 10, "usd_per_day": 3},
           "usage": {"count": 1, "usd": 0}}
    assert call(f"S.usageLines({json.dumps(row)})") == [
        "1/10 commands today", "$0.00/$3.00 today"]
    row = {"kind": "call_api", "limits": {}, "usage": {}}
    assert call(f"S.usageLines({json.dumps(row)})") == []


@node
def test_proposed_come_first_and_are_counted():
    rows = [{"id": "a", "status": "active"}, {"id": "b", "status": "proposed"},
            {"id": "c", "status": "revoked"}, {"id": "d", "status": "proposed"}]
    order = call(f"S.ordered({json.dumps(rows)}).map(r => r.id)")
    assert order[:2] == ["b", "d"] and order[-1] == "c"
    assert call(f"S.proposedCount({json.dumps(rows)})") == 2


@node
@pytest.mark.parametrize("reason", REASONS)
def test_every_refusal_reason_is_plain_words(reason):
    text = call(f"S.refusalText({{reason: {json.dumps(reason)}}})")
    assert text and "_" not in text, text


@node
def test_unknown_refusal_falls_back_to_the_servers_detail():
    assert call('S.refusalText({reason: "weird", detail: "because"})') == "because"


@node
def test_escape_neutralises_markup():
    assert call('S.esc("<img onerror=x>&\\"")') == "&lt;img onerror=x&gt;&amp;&quot;"


def test_every_server_string_is_escaped_before_innerhtml():
    js = js_path().read_text()
    assert "function esc(" in js
    for body in re.findall(r"html`(.*?)`(?=[;,)\s])", js, re.S):
        for expr in re.findall(r"\$\{(.*?)\}", body):
            if re.match(r"^\s*(esc|raw)\(", expr):
                continue
            stripped = re.sub(r"\.(map|join|slice|filter)\b", "", expr)
            assert not re.search(r"\b[a-z]\w*\.\w+", stripped), expr


def test_screen_talks_to_the_contract_routes():
    js = js_path().read_text()
    for needle in ("/api/standing-approvals", "/approve", "DELETE"):
        assert needle in js, needle
    assert "Approve" in js and "Dismiss" in js and "Revoke" in js


@node
def test_form_values_become_the_contract_body():
    form = {"desk": "atlas", "kind": "send_email", "tool": "", "pattern": "",
            "count": "80", "usd": "", "recipients": "@acme.com, bob@example.com ",
            "account": "me@acme.com", "expires": "2030-01-02", "note": "n"}
    body = call(f"S.policyBody({json.dumps(form)})")
    assert body["desk"] == "atlas" and body["kind"] == "send_email"
    assert body["tool"] == "*" and body["pattern"] == "*"
    assert body["limits"] == {"count_per_day": 80, "usd_per_day": None,
                              "recipients": ["@acme.com", "bob@example.com"],
                              "account": "me@acme.com"}
    assert body["expires_at"] == 1893542400  # 2030-01-02 00:00 UTC
    assert body["note"] == "n"


@node
def test_blank_limits_and_expiry_go_out_as_null_so_the_server_can_refuse():
    body = call('S.policyBody({desk: "*", kind: "run_command", pattern: "gh pr*"})')
    assert body["limits"]["count_per_day"] is None
    assert body["limits"]["usd_per_day"] is None
    assert body["expires_at"] is None and body["pattern"] == "gh pr*"


@node
def test_audit_line_says_who_what_and_how_much():
    line = {"ts": 1790000000, "desk": "atlas", "kind": "spend_money",
            "action": "stripe charge", "count_after": 3, "usd_after": 4.5,
            "cost_usd": 1.5, "policy_id": "sa_1"}
    text = call(f"S.auditText({json.dumps(line)})")
    for bit in ("atlas", "stripe charge", "$1.50", "3 today"):
        assert bit in text, text


def test_screen_can_add_edit_and_read_the_audit():
    js = js_path().read_text()
    for needle in ('"PATCH"', '"POST", ""', "/audit", 'data-act="edit"',
                   'data-act="add"', 'name="kind"', 'name="expires"'):
        assert needle in js, needle


@node
def test_only_cards_that_offer_a_standing_option_get_the_button():
    rows = [{"id": "a", "standing_option": {"available": True, "route": "/v1/approvals/a/standing"}},
            {"id": "b", "standing_option": {"available": False}},
            {"id": "c"}]
    assert call(f"S.standingCards({json.dumps(rows)}).map(r => r.id)") == ["a"]


@node
def test_a_card_limit_becomes_the_from_a_card_body():
    body = call('S.limitBody({count: "20", usd: "", expires: "", note: "ok"})')
    assert body == {"limits": {"count_per_day": 20, "usd_per_day": None},
                    "expires_at": None, "note": "ok"}


def test_card_posts_to_the_row_route_and_says_so_in_words():
    js = js_path().read_text()
    assert "Always, up to a limit" in js
    assert "standing_option.route" in js
    assert 'data-act="always"' in js
    assert "/api/approvals" in js


def test_the_board_no_longer_asks_for_a_token():
    js = js_path().read_text()
    for gone in ("localStorage", "Authorization", "Bearer", "/v1/"):
        assert gone not in js, gone
