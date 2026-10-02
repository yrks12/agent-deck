"""S-A "Always on" acceptance (A-1..A-7) from docs/plans/2026-09-30-overhaul.md.

Run against a real deck:  bin/overhaul-acceptance.sh   (DECK_URL, DECK_TOKEN).
Every check asserts the GOOD signal (a reply, a `system` role, a wake event) --
never the absence of an error, because absence is what "nothing ran" looks like.

READ-ONLY by default. A-1..A-4 and the A-7 dry run post messages / routines, so
they skip (and the script treats a skip as NOT green) unless DECK_ACCEPT_WRITE=1.
That is wave 4 only: the box is held by another slice until then.
"""
import json
import os
import subprocess
import time
import uuid
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.live

URL = os.environ.get("DECK_URL", "http://10.99.0.1:7789/v1").rstrip("/")
SSH = os.environ.get("DECK_SSH", "deckop@10.99.0.1")
PROBE = os.environ.get("DECK_PROBE_DESK", "wake-probe")
COS = os.environ.get("DECK_COS", "atlas")
WINDOW_H = float(os.environ.get("DECK_SOAK_HOURS", "24"))
RETIRE_CMD = os.environ.get("DECK_RETIRE_CMD", "")  # run over ssh to retire PROBE
WRITE = os.environ.get("DECK_ACCEPT_WRITE") == "1"
needs_write = pytest.mark.skipif(
    not WRITE, reason="writes to the box; wave-4 only (set DECK_ACCEPT_WRITE=1)")


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


def ssh(cmd):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", SSH, cmd],
                          capture_output=True, text=True, timeout=120)


def agents():
    return {a["name"]: a for a in call("GET", "/agents")[1]["agents"]}


def messages(thread, pages=30):
    """Every message in a thread, oldest first, walking `before` cursors."""
    out, before = [], None
    for _ in range(pages):
        q = "?limit=200" + (f"&before={before}" if before else "")
        page = call("GET", f"/threads/{thread}/messages{q}")[1]
        out = page.get("messages", []) + out
        if not page.get("has_more_before"):
            break
        before = page["next_before"]
    return out


def poll(fn, timeout, every=3.0):
    end = time.time() + timeout
    while True:
        got = fn()
        if got or time.time() >= end:
            return got
        time.sleep(every)


def send(thread, text):
    status, body = call("POST", f"/threads/{thread}/messages", {"text": text, "as": "engineer"})
    assert status == 201, f"send to {thread} -> {status} {body}"
    return body["message"]


def replies_after(thread, ts):
    return [m for m in messages(thread, pages=2) if m["role"] == "agent" and m["ts"] > ts]


def retire_probe():
    """Leave PROBE ASLEEP (the K3 state). Fails loudly if it cannot."""
    if agents().get(PROBE, {}).get("state") != "ASLEEP" and RETIRE_CMD:
        assert ssh(RETIRE_CMD).returncode == 0, "DECK_RETIRE_CMD failed"
    state = poll(lambda: agents().get(PROBE, {}).get("state") == "ASLEEP", 120)
    assert state, f"{PROBE} is {agents().get(PROBE, {}).get('state')!r}, not ASLEEP: retire it first"


# --------------------------------------------------------------------- A-1..A-4
@needs_write
def test_A1_a_message_to_a_retired_desk_is_answered_within_90s_and_delivered():
    retire_probe()
    sent = send(f"direct:{PROBE}", "reply with the word pong")
    got = poll(lambda: [m for m in replies_after(f"direct:{PROBE}", sent["ts"])
                        if "pong" in m["text"].lower()], 90)
    assert got, "no `pong` agent message within 90 s of messaging a retired desk"
    mine = next(m for m in messages(f"direct:{PROBE}", pages=2) if m["id"] == sent["id"])
    assert mine["delivery"]["state"] == "delivered"


@needs_write
def test_A2_the_woken_desk_is_the_same_session_and_remembers():
    thread = f"direct:{PROBE}"
    first = send(thread, "Remember the number 4217 for a test. Reply ok.")
    assert poll(lambda: replies_after(thread, first["ts"]), 120), "desk never acknowledged the number"
    before = agents()[PROBE]["session_id"]
    assert before, "probe has no session_id before the retire"
    retire_probe()
    asked = send(thread, "Earlier I asked you to remember a number for a test. What number was it?")
    got = poll(lambda: [m for m in replies_after(thread, asked["ts"]) if "4217" in m["text"]], 90)
    assert got, "the woken desk did not say 4217 (memory lost)"
    assert agents()[PROBE]["session_id"] == before, "wake started a NEW session"
    assert not [m for m in messages(thread, pages=2)
                if m["ts"] > asked["ts"] and "This desk was restarted" in m["text"]]


@needs_write
def test_A3_a_routine_wakes_a_retired_desk_and_renders_as_system_routine():
    thread = f"direct:{PROBE}"
    retire_probe()
    status, body = call("POST", "/routines", {
        "agent": PROBE, "prompt": "reply with the word pong", "enabled": True,
        "trigger": {"kind": "cron", "spec": "* * * * *", "tz": "UTC"}})
    assert status == 201, body
    try:
        fire = poll(lambda: next((m for m in messages(thread, pages=2)
                                  if m["author"] == "routine"), None), 120)
        assert fire, "no message authored `routine` appeared"
        assert fire["role"] == "system"
        assert poll(lambda: replies_after(thread, fire["ts"]), 90), "no reply within 90 s of the fire"
    finally:
        call("DELETE", f"/routines/{body['routine']['id']}")


@needs_write
def test_A4_a_peer_message_wakes_a_retired_desk_which_replies_to_the_sender():
    """Tests the WAKE, not obedience: a throwaway peer relays a neutral question
    through message_desk, PROBE (retired) must wake and answer the peer."""
    peer = os.environ.get("DECK_PEER_DESK", "")  # an existing throwaway desk, left alone afterwards
    created = not peer
    if created:
        peer = "qa-peer-" + uuid.uuid4().hex[:6]
        status, body = call("POST", "/agents", {
            "name": peer, "label": "QA relay", "cwd": "/tmp", "engine": "claude",
            "charter": "Throwaway test relay. When the owner asks you to ask another desk "
                       "something, call message_desk with exactly that text, then say done."})
        assert status == 201, (f"cannot create the throwaway peer ({body.get('reason')}: "
                               f"{body.get('detail')}); free a slot or set DECK_PEER_DESK", status)
    try:
        assert call("POST", f"/agents/{peer}/start")[0] in (200, 201, 202)
        retire_probe()
        t0 = time.time()
        send(f"direct:{peer}", f"Use message_desk to ask {PROBE}: 'what time is it on your machine?'")
        peer_thread = "peer:" + "|".join(sorted([peer, PROBE]))
        got = poll(lambda: [m for m in messages(peer_thread, pages=2)
                            if m["author"] == PROBE and m["ts"] > t0], 120)
        assert got, f"{PROBE} never replied to {peer} after a peer message (wake failed)"
    finally:
        if created:
            call("DELETE", f"/agents/{peer}")


# ------------------------------------------------------------------------ A-5
def _routine_fires():
    prompts = [r["prompt"][:60] for r in call("GET", "/routines")[1]["routines"]]
    return [m for m in messages(f"direct:{COS}")
            if m["role"] != "agent" and any(m["text"].startswith(p) for p in prompts)]


def test_A5_routine_fires_read_as_author_routine_role_system():
    fires = _routine_fires()
    assert fires, "no routine fire found in the chief's thread to grade"
    wrong = [(m["author"], m["role"], m["ts"]) for m in fires
             if (m["author"], m["role"]) != ("routine", "system")]
    assert not wrong, f"{len(wrong)}/{len(fires)} routine fires still read as {wrong[:2]}"


def test_A5_deck_notices_read_as_author_deck_role_system_never_the_owner():
    notices = [m for m in messages(f"direct:{COS}")
               if m["text"].startswith(("Hired: ", "[Agent Deck]", "Approved: ask "))
               and m["role"] != "agent"]
    assert notices, "no deck-generated notice found to grade"
    wrong = [m["text"][:30] for m in notices if (m["author"], m["role"]) != ("deck", "system")]
    assert not wrong, f"{len(wrong)}/{len(notices)} deck notices drawn as the owner: {wrong[:2]}"


def test_A5_every_message_carries_channel_and_kind():
    msgs = messages(f"direct:{COS}", pages=1)
    assert msgs, "no messages to grade"
    lacking = [m["id"] for m in msgs if (m.get("channel"), m.get("kind")) != ("text", "text")]
    assert not lacking, f"{len(lacking)}/{len(msgs)} messages lack channel/kind (K1)"


# ------------------------------------------------------------------------ A-6
def _inbound_in_window():
    since, out = time.time() - WINDOW_H * 3600, []
    for name in agents():
        out += [(name, m) for m in messages(f"direct:{name}")
                if m["role"] in ("owner", "system") and m["ts"] >= since]
    assert out, f"no owner/system message in the last {WINDOW_H:g} h to grade"
    return out


def test_A6_every_owner_or_system_message_in_the_window_got_an_agent_reply():
    grace, unanswered = time.time() - 300, []
    for name, m in _inbound_in_window():
        if m["ts"] < grace and not [x for x in messages(f"direct:{name}", pages=3)
                                    if x["role"] == "agent" and x["ts"] > m["ts"]]:
            unanswered.append((name, m["text"][:30]))
    assert not unanswered, f"{len(unanswered)} messages never answered: {unanswered[:3]}"


def test_A6_no_message_sits_undelivered_or_sent_for_over_5_minutes():
    stuck = [(n, m["text"][:30]) for n, m in _inbound_in_window()
             if m["ts"] < time.time() - 300 and m["role"] == "owner"
             and (m.get("delivery") or {}).get("state") in ("undelivered", "sent")]
    assert not stuck, f"stuck: {stuck[:3]}"


def test_A6_the_window_contains_a_real_retire_and_a_wake_that_answered_it():
    since = time.time() - WINDOW_H * 3600
    log = ssh("grep -c 'bg retire' ~/.claude/daemon.log").stdout.strip()
    assert log.isdigit() and int(log) >= 1, "daemon.log shows no `bg retire`"
    ev = ssh("grep -h '\"wake\"' ~/.claude/agent-bus/events.jsonl").stdout.splitlines()
    wakes = [j for j in map(json.loads, filter(None, ev))
             if j.get("ts", 0) >= since and j.get("state") in ("woken", "restarted")]
    assert wakes, "events.jsonl has no `wake` event in the window: the wake path never ran"


# ------------------------------------------------------------------------ A-7
DOCTOR = os.environ.get("DECK_DOCTOR", "/opt/agent-deck/bin/deckdoctor")


# The service's own environment (deploy/deckdoctor.service): ssh's PATH lacks ~/.local/bin,
# where `claude` lives, so the auth check is always red without this. Alerts go to a
# FILE and dedupe state to a temp path: a live run must never speak in the chief's thread.
DOCTOR_ENV = ("PATH=/home/deckop/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
              "DECK_URL=http://10.99.0.1:7789")
DOCTOR_PY = os.environ.get("DECK_DOCTOR_PY", "/opt/agent-deck/.venv/bin/python")


def run_doctor(extra_env="", state=None, alert=None):
    tag = uuid.uuid4().hex[:8]
    state = state or f"/tmp/qa-dd-state-{tag}.json"
    alert = alert or f"/tmp/qa-dd-alert-{tag}.jsonl"
    r = ssh(f"{DOCTOR_ENV} DECKDOCTOR_STATE={state} DECKDOCTOR_ALERT=file:{alert} {extra_env} "
            f"{DOCTOR_PY} {DOCTOR}")
    return r, state, alert


def _alert_lines(alert):
    return [ln for ln in ssh(f"cat {alert} 2>/dev/null").stdout.splitlines() if ln.strip()]


def test_A7_deckdoctor_is_green_with_real_thresholds():
    r, state, alert = run_doctor()
    ssh(f"rm -f {state} {alert}")
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    assert lines, f"deckdoctor printed no JSON line (rc={r.returncode}): {r.stderr.strip()[:120]}"
    out = json.loads(lines[-1])
    assert out["ok"] is True and r.returncode == 0, out
    assert {"auth", "disk_pct", "daemon", "stuck_messages", "desks_asleep"} <= set(out["checks"])


@needs_write
def test_A7_a_red_check_posts_exactly_one_deck_line_and_does_not_repeat():
    """Exactly one alert per red check across two runs -- read from the alert FILE."""
    state = f"/tmp/qa-dd-state-{uuid.uuid4().hex[:8]}.json"
    alert = state.replace("state", "alert").replace(".json", ".jsonl")
    try:
        for _ in range(2):
            r, _, _ = run_doctor("DISK_RED_PCT=1 DECKDOCTOR_AUTH_PROBE=0", state, alert)
            assert r.returncode == 1, f"a red doctor must exit 1, got {r.returncode}: {r.stdout[-200:]}"
        lines = _alert_lines(alert)
        assert len(lines) == 1, f"expected exactly one deck line across two red runs, saw {len(lines)}"
        assert json.loads(lines[0])["from"] == "deck"
    finally:
        ssh(f"rm -f {state} {alert}")
