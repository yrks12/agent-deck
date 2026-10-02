"""Golden counts against the session this feature was designed from.

`tests/fixtures/orion-comms.jsonl` is a frozen slice of session adb790ff: every
record that carries a cross-session message, bodies replaced with msg-out-<n> /
msg-in-<n>, and peer names, desk labels and paths replaced with synthetic ones. Structure, tags, targets, sockets and timestamps are untouched,
because those are the only things the extractor reads.

Frozen on purpose. The live session kept working while this was written and its
counts moved (65 -> more), so a golden pinned to the live file would rot within
the hour.

Two shapes matter, and missing either one loses a whole direction of traffic:
outbound is a SendMessage tool_use block, inbound is a plain STRING message
body, not a block list.
"""

from server.sources import comms
from server.sources.transcript import TranscriptTail
from tests.conftest import FIXTURES

EXPECTED_OUT = 65
EXPECTED_IN = 30


def collect(tmp_path):
    raw = []
    path = tmp_path / "adb790ff.jsonl"
    path.write_bytes((FIXTURES / "orion-comms.jsonl").read_bytes())
    TranscriptTail(path, owner="sid-main", sink=raw.append).poll()
    return raw


def test_every_sent_and_received_message_is_extracted(tmp_path):
    raw = collect(tmp_path)
    assert sum(1 for r in raw if r["dir"] == "out") == EXPECTED_OUT
    assert sum(1 for r in raw if r["dir"] == "in") == EXPECTED_IN


def test_outbound_records_carry_a_target_and_text(tmp_path):
    out = [r for r in collect(tmp_path) if r["dir"] == "out"]
    assert all(r["to_label"] for r in out)
    assert all(r["text"] for r in out)
    assert all(r["owner"] == "sid-main" for r in out)


def test_inbound_records_carry_a_resolvable_sender_pid(tmp_path):
    inbound = [r for r in collect(tmp_path) if r["dir"] == "in"]
    pids = [comms.pid_from_sock(r["from_sock"]) for r in inbound]
    assert all(isinstance(p, int) for p in pids)


def test_inbound_body_excludes_the_wrapper_tag(tmp_path):
    inbound = [r for r in collect(tmp_path) if r["dir"] == "in"]
    assert all("<cross-session-message" not in r["text"] for r in inbound)
    assert all(r["text"].startswith("msg-in-") for r in inbound)


def test_every_record_carries_a_real_timestamp(tmp_path):
    raw = collect(tmp_path)
    assert all(r["ts"] > 1_600_000_000 for r in raw), "ISO timestamps must convert"


def test_a_second_poll_emits_nothing_new(tmp_path):
    raw = []
    path = tmp_path / "adb790ff.jsonl"
    path.write_bytes((FIXTURES / "orion-comms.jsonl").read_bytes())
    tail = TranscriptTail(path, owner="sid-main", sink=raw.append)
    tail.poll()
    first = len(raw)
    tail.poll()
    assert len(raw) == first, "byte-offset tail must never re-emit"


def test_the_index_keeps_every_message_from_the_fixture(tmp_path):
    index = comms.CommsIndex()
    for record in collect(tmp_path):
        index.add(record)
    edges = index.edges(comms.build_directory([]))
    assert len(edges) == EXPECTED_OUT + EXPECTED_IN, (
        "the main session's peers are outside this fixture, so no two records "
        "are copies of one message and nothing may collapse"
    )
    assert all(edge["text"] for edge in edges)
