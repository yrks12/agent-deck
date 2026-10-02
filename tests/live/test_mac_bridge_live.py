"""Mac-bridge live detectors L-1..L-6 (docs/plans/2026-09-30-mac-bridge.md).

Run against a real deck:  bin/mac-bridge-acceptance.sh   (DECK_URL, DECK_TOKEN).

Every check asserts the GOOD signal -- an online node, a `done` job with the real
ProductVersion, a `cancelled` job, a refusal row -- never the absence of an error.
Each test starts with READ-ONLY preconditions (the MB1 routes answer, a desk's argv
carries the `mac` MCP server, a Mac is online). Those are what make every detector
red on a box that has not shipped the bridge, without posting anything.

Only after the preconditions hold does a test do the part that writes (message the
throwaway desk `mac-probe`) or needs a person/UI test (Pause, Stop, grant card).
Those parts pytest.skip -- and the harness treats any skip as NOT green -- unless
DECK_ACCEPT_WRITE=1 (and, for the UI-driven steps, the hook env named per test).

Env: DECK_URL (with or without /v1), DECK_TOKEN | DECK_TOKEN_FILE, DECK_SSH,
     DECK_PROBE_DESK (mac-probe), DECK_MAC_NODE (node id or name; default primary),
     DECK_ACCEPT_WRITE, DECK_MAC_PAUSE_CMD / DECK_MAC_RESUME_CMD / DECK_MAC_STOP_CMD /
     DECK_MAC_GRANT_CMD / DECK_MAC_DENY_CMD (shell commands that drive the Mac UI,
     e.g. an XCUITest run of macos/UITests/MacBridgeUITests.swift with
     YOS_SCREEN_IS_FREE=1), DECK_MAC_ACTIVITY (mac-activity.jsonl path),
     DECK_LID_TEST=1 (L-6, after a person closed the lid for 2 min).
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_RAW = os.environ.get("DECK_URL", "http://10.99.0.1:7789").rstrip("/")
URL = _RAW if _RAW.endswith("/v1") else _RAW + "/v1"
SSH = os.environ.get("DECK_SSH", "deckop@10.99.0.1")
PROBE = os.environ.get("DECK_PROBE_DESK", "mac-probe")
WANT_NODE = os.environ.get("DECK_MAC_NODE", "")
ACTIVITY = Path(os.environ.get(
    "DECK_MAC_ACTIVITY",
    str(Path.home() / "Library/Application Support/Agent Deck/mac-activity.jsonl")))
WRITE = os.environ.get("DECK_ACCEPT_WRITE") == "1"
PROMPT = ("Use mcp__mac__run to run sw_vers on my Mac and reply with the "
          "ProductVersion line only.")


def _token() -> str:
    tok = os.environ.get("DECK_TOKEN", "")
    if not tok and os.environ.get("DECK_TOKEN_FILE"):
        tok = open(os.environ["DECK_TOKEN_FILE"]).read()
    assert tok.strip(), "DECK_TOKEN (or DECK_TOKEN_FILE) is not set"
    return tok.strip()


def call(method, path, body=None):
    """(status, json). The token only ever goes into the header."""
    req = urllib.request.Request(
        URL + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + _token(),
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}


def sh(cmd, timeout=120):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def ssh(cmd):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", SSH, cmd],
                          capture_output=True, text=True, timeout=60)


def poll(fn, timeout, every=2.0):
    end = time.time() + timeout
    while True:
        got = fn()
        if got or time.time() >= end:
            return got
        time.sleep(every)


def need_write():
    if not WRITE:
        pytest.skip("writes to the box / needs the Mac; set DECK_ACCEPT_WRITE=1")


def need_hook(name, why):
    cmd = os.environ.get(name, "")
    if not cmd:
        pytest.skip(f"{name} not set: {why}")
    return cmd


# ---------------------------------------------------------------- read-only preconditions
def online_node():
    """GOOD signal: MB1 owner route answers 200 and a Mac is online. Asserts, never skips."""
    status, body = call("GET", "/nodes")
    assert status == 200, f"GET /v1/nodes -> {status} (the node API is not deployed)"
    nodes = body.get("nodes", [])
    if WANT_NODE:
        nodes = [n for n in nodes if WANT_NODE in (n["node_id"], n["name"])]
    live = [n for n in nodes if n.get("online")]
    assert live, f"no Mac is online (nodes: {[n.get('name') for n in nodes]})"
    return next((n for n in live if n.get("primary")), live[0])


def probe_has_mac_tool():
    """GOOD signal: some running desk's argv carries the `mac` MCP server (MB3/S-11)."""
    out = ssh("ps -eo args | grep -F -- --mcp-config | grep -c '[s]erver.mac_mcp'")
    # the [s] keeps grep from matching its own command line
    n = int((out.stdout or "0").strip().splitlines()[-1] or 0) if out.returncode in (0, 1) else 0
    assert n > 0, "no running desk has `server.mac_mcp` in its --mcp-config argv"


def jobs(node_id):
    status, body = call("GET", f"/nodes/{node_id}/jobs?limit=50")
    assert status == 200, f"GET /v1/nodes/{node_id}/jobs -> {status}"
    return body.get("jobs", [])


def ask(text):
    status, body = call("POST", f"/threads/direct:{PROBE}/messages", {"text": text, "as": "engineer"})
    assert status == 201, f"send to {PROBE} -> {status} {body}"
    return body["message"]


def replies_after(ts):
    page = call("GET", f"/threads/direct:{PROBE}/messages?limit=50")[1]
    return [m for m in page.get("messages", []) if m["role"] == "agent" and m["ts"] > ts]


def new_jobs(node_id, since, **match):
    return [j for j in jobs(node_id)
            if j.get("desk") == PROBE and j.get("created_at", j.get("started", 0)) >= since
            and all(j.get(k) == v for k, v in match.items())]


def activity_rows():
    if not ACTIVITY.exists():
        return []
    return [json.loads(line) for line in ACTIVITY.read_text().splitlines() if line.strip()]


# ------------------------------------------------------------------------------- L-1
def test_L1_a_desk_runs_sw_vers_on_the_users_mac_and_replies_with_the_real_version():
    node = online_node()
    probe_has_mac_tool()
    need_write()
    t0 = time.time()
    sent = ask(PROMPT)
    want = sh("sw_vers -productVersion").stdout.strip()
    assert want, "local sw_vers gave nothing"
    got = poll(lambda: [m for m in replies_after(sent["ts"]) if m["text"].strip() == want], 90)
    assert got, f"no reply equal to {want!r} within 90 s"
    done = poll(lambda: new_jobs(node["node_id"], t0 - 5, state="done", exit=0), 10)
    assert done, "server audit copy shows no `done` exit-0 job for the probe"
    assert poll(lambda: [r for r in activity_rows() if r.get("job_id") == done[0]["id"]
                         or "sw_vers" in json.dumps(r)], 10), "no row in mac-activity.jsonl"


# ------------------------------------------------------------------------------- L-2
def test_L2_offline_is_a_card_not_a_hang_and_resume_retries_unprompted():
    node = online_node()
    probe_has_mac_tool()
    need_write()
    pause = need_hook("DECK_MAC_PAUSE_CMD", "drives Pause Mac access on the Mac")
    resume = need_hook("DECK_MAC_RESUME_CMD", "drives Resume")
    assert sh(pause).returncode == 0, "pause hook failed"
    try:
        t0 = time.time()
        sent = ask(PROMPT)
        refused = poll(lambda: new_jobs(node["node_id"], t0 - 5, state="refused"), 5)
        assert refused, "no refused job (mac_paused/mac_offline) within 5 s"
        assert refused[0].get("reason") in ("mac_paused", "mac_offline"), refused[0]
        cards = [h for h in call("GET", "/handoffs")[1].get("handoffs", [])
                 if PROBE in json.dumps(h)]
        assert len(cards) == 1, f"expected exactly one handoff card for {PROBE}, got {len(cards)}"
        said = poll(lambda: [m for m in replies_after(sent["ts"])
                             if any(w in m["text"].lower() for w in ("off", "offline", "paused"))], 60)
        assert said, "the desk never said the Mac is off within 60 s"
    finally:
        assert sh(resume).returncode == 0, "resume hook failed"
    want = sh("sw_vers -productVersion").stdout.strip()
    assert poll(lambda: [m for m in replies_after(sent["ts"]) if want in m["text"]], 90), \
        "after Resume the desk did not retry unprompted and post the ProductVersion"
    assert poll(lambda: not [h for h in call("GET", "/handoffs")[1].get("handoffs", [])
                             if PROBE in json.dumps(h)], 40), "card still open 40 s after Resume"


# ------------------------------------------------------------------------------- L-3
def test_L3_grant_card_allow_completes_the_job_and_deny_blocks_it():
    node = online_node()
    probe_has_mac_tool()
    need_write()
    allow = need_hook("DECK_MAC_GRANT_CMD", "UI test taps 'Allow for 1 hour' on the grant card")
    deny = need_hook("DECK_MAC_DENY_CMD", "UI test taps 'Deny' on the grant card")
    want = sh("sw_vers -productVersion").stdout.strip()
    t0 = time.time()
    sent = ask(PROMPT)
    assert sh(allow).returncode == 0, "grant hook failed"
    assert poll(lambda: new_jobs(node["node_id"], t0 - 5, state="done", exit=0), 90), \
        "job did not complete after Allow for 1 hour"
    assert poll(lambda: [m for m in replies_after(sent["ts"]) if want in m["text"]], 60)
    t1 = time.time()
    ask(PROMPT)
    assert sh(deny).returncode == 0, "deny hook failed"
    assert poll(lambda: new_jobs(node["node_id"], t1 - 5, state="refused", reason="denied"), 90), \
        "no `denied` refusal after Deny"


# ------------------------------------------------------------------------------- L-4
def test_L4_stop_in_the_use_bar_cancels_a_running_job_within_5s_and_kills_it():
    node = online_node()
    probe_has_mac_tool()
    need_write()
    stop = need_hook("DECK_MAC_STOP_CMD", "UI test taps Stop in the in-use bar")
    t0 = time.time()
    ask('Use mcp__mac__run to run `sleep 300` on my Mac with timeout_s 300.')
    running = poll(lambda: new_jobs(node["node_id"], t0 - 5, state="running"), 60)
    assert running, "no running job appeared"
    assert sh("pgrep -f 'sleep 300'").stdout.strip(), "sleep 300 is not running on the Mac"
    t1 = time.time()
    assert sh(stop).returncode == 0, "stop hook failed"
    cancelled = poll(lambda: new_jobs(node["node_id"], t0 - 5, state="cancelled"), 5)
    assert cancelled, "job not `cancelled` within 5 s of Stop"
    assert time.time() - t1 < 15
    assert not sh("pgrep -f 'sleep 300'").stdout.strip(), "sleep 300 survived Stop"


# ------------------------------------------------------------------------------- L-5
def test_L5_reading_ssh_config_is_refused_blocked_path_and_logged():
    node = online_node()
    probe_has_mac_tool()
    need_write()
    t0 = time.time()
    ask("Use mcp__mac__read to read ~/.ssh/config on my Mac and show me the first line.")
    refused = poll(lambda: new_jobs(node["node_id"], t0 - 5, state="refused", kind="read"), 90)
    assert refused, "no refused read job"
    assert refused[0].get("reason") == "blocked_path", refused[0]
    assert poll(lambda: [r for r in activity_rows()
                         if "blocked_path" in json.dumps(r) and r.get("ts", 0) >= t0 - 5], 10), \
        "no `refused: blocked_path` row in mac-activity.jsonl"


# ------------------------------------------------------------------------------- L-6
def test_L6_after_a_lid_close_the_mac_is_back_online_within_40s():
    """Manual, not gated: a person closes the lid for 2 min mid-idle, reopens, then sets DECK_LID_TEST=1."""
    n = online_node()
    if os.environ.get("DECK_LID_TEST") != "1":
        pytest.skip("manual: close the lid 2 min, reopen, re-run with DECK_LID_TEST=1")
    assert poll(lambda: next((x for x in call("GET", "/nodes")[1]["nodes"]
                              if x["node_id"] == n["node_id"] and x["online"]), None), 40), \
        "Mac not back online within 40 s of reopening"
