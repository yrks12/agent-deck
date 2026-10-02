"""What the speaker needs out of the transcript.

The card only ever needed 220 characters of the last reply. Speech needs the
whole thing, and it needs the message id -- without the id nothing can be
deduped, and without the full text the manager would be cut off mid-sentence.
"""

import json

from server.sources.transcript import TranscriptTail

LONG = (
    "He deployed at 8:44 and is waiting on your call about #372. "
    "The reviewer passed #377 on every site it converts, and d6 is asking "
    "whether it may run pytest. " * 6
)


def write(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def assistant(text, message_id="msg-1"):
    return {
        "type": "assistant",
        "timestamp": "2026-08-10T09:00:00.000Z",
        "isSidechain": False,
        "message": {
            "id": message_id,
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": text}],
        },
    }


def test_the_full_reply_is_kept_for_speech(tmp_path):
    path = tmp_path / "t.jsonl"
    write(path, [assistant(LONG)])
    tail = TranscriptTail(path, owner="sid-m")
    state = tail.poll()

    assert len(state.last_assistant) <= 221, "the card copy stays short"
    assert len(state.last_assistant_full) > 500, "speech needs the whole reply"
    assert state.last_assistant_full.startswith("He deployed at 8:44")


def test_the_reply_carries_its_message_id(tmp_path):
    path = tmp_path / "t.jsonl"
    write(path, [assistant("short one", message_id="msg-abc")])
    state = TranscriptTail(path, owner="sid-m").poll()
    assert state.last_assistant_id == "msg-abc"


def test_a_later_reply_replaces_the_earlier_one(tmp_path):
    path = tmp_path / "t.jsonl"
    write(path, [assistant("first", "msg-1"), assistant("second", "msg-2")])
    state = TranscriptTail(path, owner="sid-m").poll()
    assert state.last_assistant_full == "second"
    assert state.last_assistant_id == "msg-2"


def test_a_tool_call_does_not_clobber_the_last_reply(tmp_path):
    """A turn usually ends with tool calls after the prose; the prose must win."""
    path = tmp_path / "t.jsonl"
    record = assistant("the answer", "msg-9")
    record["message"]["content"].append(
        {"type": "tool_use", "id": "tu-1", "name": "Bash", "input": {"command": "ls"}}
    )
    write(path, [record])
    state = TranscriptTail(path, owner="sid-m").poll()
    assert state.last_assistant_full == "the answer"
    assert state.last_assistant_id == "msg-9"
