"""S-C "Live talk" server side + C-4, and the server half of D-3 (K6).

C-1 (mic prompts) and C-2 (push-to-talk latency) need a human, a microphone and
the installed app, so they cannot be scripted here: bin/overhaul-acceptance.sh
prints them as the manual checklist. What can be read from the box is read here.
The call flow itself creates a call on PROBE (never an owner desk), so it needs
DECK_ACCEPT_WRITE=1.
"""
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request

import pytest

from test_overhaul_always_on import PROBE, URL, agents, call, messages, needs_write, poll, replies_after

pytestmark = pytest.mark.live


def _paths():
    return json.load(urllib.request.urlopen(URL.rsplit("/v1", 1)[0] + "/openapi.json", timeout=15))["paths"]


def test_C_the_call_routes_are_mounted():
    paths = _paths()
    missing = [p for p in ("/v1/calls", "/v1/calls/{call_id}/end") if p not in paths]
    assert not missing, f"not mounted: {missing}"


def test_D3_agent_settings_carry_voice_and_avatar_as_objects_or_null():
    a = call("GET", f"/agents/{PROBE if PROBE in agents() else next(iter(agents()))}")[1]
    assert "voice" in a, "GET /v1/agents/{name} has no `voice` key (K6)"
    assert a.get("avatar_look") is None or isinstance(a["avatar_look"], dict), \
        f"`avatar_look` must be {{shape,color}} or null, got {a.get('avatar_look')!r}"


@needs_write
def test_D3_patching_avatar_and_voice_round_trips_on_the_settings_read():
    want = {"avatar": {"shape": "hexagon", "color": 3}, "voice": {"id": "", "rate": 1.1}}
    status, body = call("PATCH", f"/agents/{PROBE}", want)
    assert status == 200, body
    got = call("GET", f"/agents/{PROBE}")[1]
    # an avatar OBJECT on PATCH is read back as `avatar_look` (docs/client-api.md K6)
    assert got["avatar_look"] == want["avatar"] and got["voice"] == want["voice"]


@needs_write
def test_C3_a_call_of_three_exchanges_ends_with_a_closing_line_and_a_chat_summary():
    status, body = call("POST", "/calls", {"agent": PROBE})
    assert status == 201, body
    cid = body["call_id"]
    assert re.fullmatch(r"call_[0-9a-f]{12}", cid) and body["thread_id"] == f"direct:{PROBE}"
    assert set(body["voice"]) >= {"id", "rate"}
    thread = body["thread_id"]
    for i in range(3):
        t0 = time.time()
        s, sent = call("POST", f"/threads/{thread}/messages",
                       {"text": f"say the number {i + 1}", "as": "sam", "channel": "voice", "call_id": cid})
        assert s == 201 and sent["message"]["channel"] == "voice", (s, sent)
        assert poll(lambda: replies_after(thread, t0), 90), f"no spoken reply to utterance {i + 1}"
    t_end = time.time()
    s, ended = call("POST", f"/calls/{cid}/end")
    assert s == 200 and ended.get("turns") == 3, (s, ended)
    assert call("POST", f"/calls/{cid}/end")[0] == 409, "ending twice must be 409"
    assert call("POST", "/calls/call_000000000000/end")[0] == 404
    closing = [m for m in messages(thread, pages=2) if m["ts"] >= t_end - 5 and "The call ended" in m["text"]]
    assert closing, "no 'The call ended' line in the thread"
    assert poll(lambda: [m for m in replies_after(thread, t_end) if "The call ended" not in m["text"]], 60), \
        "the desk posted no chat summary within 60 s of hang-up"


SWIFT = """import Speech
for id in ["en-US", "he-IL"] {
    print(id, SFSpeechRecognizer(locale: Locale(identifier: id))?.supportsOnDeviceRecognition ?? false)
}
"""


def test_C4_on_device_recognition_is_available_for_en_US_and_hebrew_is_reported(tmp_path, record_property):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("needs macOS + swiftc (run on the owner's Mac)")
    (tmp_path / "m.swift").write_text(SWIFT)
    build = subprocess.run(["swiftc", "-o", str(tmp_path / "m"), str(tmp_path / "m.swift")],
                           capture_output=True, text=True, timeout=180)
    assert build.returncode == 0, build.stderr[-300:]
    out = subprocess.run([str(tmp_path / "m")], capture_output=True, text=True, timeout=30).stdout
    support = dict(line.split() for line in out.splitlines() if re.match(r"\w\w-\w\w (true|false)$", line))
    record_property("he-IL supportsOnDeviceRecognition", support.get("he-IL"))  # reported, not gated
    assert support.get("en-US") == "true", f"en-US on-device recognition unavailable: {support}"
