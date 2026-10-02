"""The real-time path that costs nothing: the deck posts to HIS ntfy server.

The phone app is signed with a free Apple team, and free provisioning cannot
use APNs. ntfy's own iOS app can: a self-hosted ntfy forwards a content-free
"poll request" (a message id, no text) through ntfy.sh's APNs, and the app
then fetches the message from HIS server. So the deck posts each alert to a
private topic on his box, and nothing he is told leaves the box except to his
own phone.

What must hold:

1. OFF unless configured. No URL, no request, no state -- a deck that was
   never set up for ntfy behaves exactly as before.
2. A first run sends NOTHING and starts at the head, like the app's first
   poll: switching it on must not replay a month.
3. Each alert is posted once. A second tick with nothing new posts nothing.
4. A failed post does not move the cursor: the same alert is tried again next
   tick, and nothing after it jumps the queue.
5. The post carries a title, the text, a priority by class, and a click link
   that opens the right thread in the Agent Deck app. JSON body, so a desk's
   name with an em dash does not break an HTTP header.
"""

import json

from server import ntfy

URL = "http://10.99.0.1:8090/deck-k3y"


def alert(n, kind="for_you", source="message", agent="atlas", card=""):
    return {"id": f"msg:{n}", "kind": kind, "source": source, "agent": agent,
            "thread_id": f"direct:{agent}", "card_id": card,
            "title": f"{agent} — for you", "body": f"line {n}",
            "cursor": f"{n:018d}-m{n}", "ts": float(n)}


class Feed:
    """The surface's `owner_alerts`, as the pusher sees it."""

    def __init__(self):
        self.items = []

    def __call__(self, since):
        if since is None:
            head = self.items[-1]["cursor"] if self.items else f"{0:018d}-0"
            return {"alerts": [], "next_since": head}
        fresh = [a for a in self.items if a["cursor"] > since]
        return {"alerts": fresh,
                "next_since": fresh[-1]["cursor"] if fresh else since}


def pusher(tmp_path, feed, sent, *, fail=lambda body: False):
    def send(request):
        body = json.loads(request.data)
        if fail(body):
            raise OSError("ntfy down")
        sent.append((request, body))

    return ntfy.Pusher(tmp_path / "ntfy.json", fetch=feed, opener=send)


def test_it_is_off_unless_configured(tmp_path):
    feed, sent = Feed(), []
    feed.items.append(alert(1))
    posted = pusher(tmp_path, feed, sent).tick(env={})
    assert posted == [] and sent == []
    assert not (tmp_path / "ntfy.json").exists()


def test_a_first_run_starts_at_the_head_and_sends_nothing(tmp_path):
    feed, sent = Feed(), []
    feed.items.append(alert(1))
    p = pusher(tmp_path, feed, sent)
    assert p.tick(env={"DECK_NTFY_URL": URL}) == []
    assert sent == []
    feed.items.append(alert(2))
    assert p.tick(env={"DECK_NTFY_URL": URL}) == ["msg:2"]


def test_each_alert_is_posted_once(tmp_path):
    feed, sent = Feed(), []
    p = pusher(tmp_path, feed, sent)
    env = {"DECK_NTFY_URL": URL, "DECK_NTFY_TOKEN": "tk_abc"}
    p.tick(env=env)
    feed.items += [alert(1), alert(2, kind="needs_you", source="approval",
                                    card="k7x2p")]
    assert p.tick(env=env) == ["msg:1", "msg:2"]
    assert p.tick(env=env) == []
    # And across a restart: the cursor is on disk.
    again = pusher(tmp_path, feed, sent)
    assert again.tick(env=env) == []
    assert len(sent) == 2


def test_the_post_is_shaped_for_the_ntfy_app(tmp_path):
    feed, sent = Feed(), []
    p = pusher(tmp_path, feed, sent)
    env = {"DECK_NTFY_URL": URL, "DECK_NTFY_TOKEN": "tk_abc"}
    p.tick(env=env)
    feed.items.append(alert(1, kind="needs_you", source="approval",
                            card="k7x2p"))
    p.tick(env=env)

    request, body = sent[0]
    assert request.full_url == "http://10.99.0.1:8090/"
    assert request.get_header("Authorization") == "Bearer tk_abc"
    assert body["topic"] == "deck-k3y"
    assert body["title"] == "atlas — for you"
    assert body["message"] == "line 1"
    assert body["priority"] == 4
    assert body["click"] == ("agentdeck://open?thread=direct%3Aatlas"
                             "&card=k7x2p&alert=msg%3A1")


def test_a_failed_post_is_retried_and_nothing_jumps_it(tmp_path):
    feed, sent = Feed(), []
    down = {"now": True}
    p = pusher(tmp_path, feed, sent,
               fail=lambda body: down["now"] and body["message"] == "line 1")
    env = {"DECK_NTFY_URL": URL}
    p.tick(env=env)
    feed.items += [alert(1), alert(2)]
    assert p.tick(env=env) == []
    assert sent == []
    down["now"] = False
    assert p.tick(env=env) == ["msg:1", "msg:2"]


def test_an_unusable_url_is_not_configured():
    assert ntfy.target({"DECK_NTFY_URL": "not a url"}) is None
    assert ntfy.target({"DECK_NTFY_URL": "http://box:8090/"}) is None
    assert ntfy.target({"DECK_NTFY_URL": URL}) == ("http://10.99.0.1:8090/",
                                                   "deck-k3y", "")
