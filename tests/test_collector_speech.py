"""The join between the collector and the speaker.

Every other piece is tested in isolation: the tail keeps the full reply, the
speaker decides whether to say it. This asserts the collector actually hands one
to the other on a tick -- the seam where a rename or a moved line would leave the
manager permanently silent with every other test still green.
"""

from server.collector import Collector
from server.sources.bus import SessionSignals
from server.sources.sessions import RawSession
from server.sources.transcript import TranscriptState


class FakeTail:
    def __init__(self, state):
        self.state = state

    def poll(self):
        return self.state


class FakeAgents:
    def poll(self, forced_states=None):
        return []


def collector_with(state, done_at):
    collector = Collector()
    session = RawSession(
        pid=4242, session_id="sid-m", cwd="/tmp", name="mgr",
        status="idle", kind="interactive", started_at=0,
    )
    collector._tracked["sid-m"] = type(
        "T", (), {"tail": FakeTail(state), "agents": FakeAgents()}
    )()
    signals = {"sid-m": SessionSignals(done_at=done_at)}

    heard = []
    collector.speaker.check = lambda session_id, **kw: heard.append((session_id, kw))
    collector._build(session, signals, now=done_at + 1)
    return heard


def test_the_collector_offers_the_reply_to_the_speaker():
    state = TranscriptState(
        last_assistant="clipped copy",
        last_assistant_full="the whole reply, every word of it",
        last_assistant_id="msg-77",
    )
    heard = collector_with(state, done_at=1000.0)
    assert heard, "the collector never offered the reply to the speaker"
    session_id, kwargs = heard[0]
    assert session_id == "sid-m"
    assert kwargs["text"] == "the whole reply, every word of it", "must pass the FULL reply"
    assert kwargs["message_id"] == "msg-77"
    assert kwargs["done_at"] == 1000.0


def test_a_session_that_has_not_finished_a_turn_is_not_offered():
    state = TranscriptState(last_assistant_full="something", last_assistant_id="msg-1")
    assert collector_with(state, done_at=0.0) == []
