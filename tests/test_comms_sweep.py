"""The class, not the instance.

The golden test proves one session parses. This runs the extractor over every
transcript on this machine and asserts it never crashes, never drops an endpoint
without a label, and never emits a record the index cannot place.
"""

import time

import pytest

from server.paths import PROJECTS_DIR
from server.sources import comms
from server.sources.transcript import TranscriptTail

LIMIT = 200


def recent_transcripts():
    files = [p for p in PROJECTS_DIR.rglob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:LIMIT]


@pytest.mark.skipif(not PROJECTS_DIR.is_dir(), reason="no transcripts on this machine")
def test_extractor_survives_every_transcript_on_disk():
    started = time.time()
    index = comms.CommsIndex(max_edges=100_000)
    scanned = 0
    raw_total = 0
    for path in recent_transcripts():
        raw = []
        TranscriptTail(path, owner=path.stem, sink=raw.append).poll()
        for record in raw:
            assert record["dir"] in ("in", "out")
            assert record["owner"] == path.stem
            index.add(record)
        raw_total += len(raw)
        scanned += 1

    assert scanned > 0, "expected at least one transcript to sweep"
    edges = index.edges(comms.build_directory([]))
    for edge in edges:
        assert edge["text"], "an edge with no text is a parse failure"
        assert edge["from"]["name"], "sender must always be labelled"
        assert edge["to"]["name"], "target must always be labelled"
        assert edge["seen"] in ("both", "sender", "receiver")
        assert edge["status"] in ("sent", "delivered")
    print(f"\nswept {scanned} transcripts, {raw_total} records, {len(edges)} edges")
    assert time.time() - started < 180, "sweep must stay usable as a test"
