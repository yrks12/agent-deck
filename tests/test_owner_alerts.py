"""Which things on the deck may buzz his phone, decided in ONE place.

THE REQUEST, verbatim: *"they should be able to notify me when they need me
and when they send messages intended to me."* Two classes, and everything else
stays quiet:

* NEEDS_YOU -- a decision card a desk put in front of him (and, in the feed, a
  pending approval or handoff; those come from their own ledgers).
* FOR_YOU   -- a desk's message meant for him: the turn-final answer of a desk
  that reports to him, the reply of any desk he wrote to, and an urgent line.

NOT desk<->desk chatter, NOT progress. `say` is the progress channel by its own
tool description ("On it -- checking Acme first."), and a report's turn-final
prose is addressed to its boss unless HE was the one who wrote to it.

Pure: records in, a label out. The classifier is fed the office log in write
order, exactly as `api.Surface` folds it.
"""

from server import owner_alerts as oa
from server.owner_alerts import FOR_YOU, NEEDS_YOU

BOSSES = {"atlas": None, "wake-probe": None, "scout": "atlas"}


def classifier():
    return oa.Classifier(boss_of=lambda name: BOSSES.get(name))


def rec(to, sender, text="hello", **extra):
    return {"ts": 1.0, "id": f"{sender}-{to}-{text}"[:12], "to": to,
            "from": sender, "text": text, **extra}


def test_a_decision_card_needs_him():
    c = classifier()
    assert c.classify(rec("owner", "atlas", "Ship it?", kind="decision",
                          decision={"id": "dec_1"})) == NEEDS_YOU


def test_a_plain_reply_he_did_not_ask_for_is_quiet():
    """MEASURED: 45 of 81 pushes in one hour were the chief's plain replies.
    A desk's answer is in the app; it buzzes only when he asked for it."""
    c = classifier()
    assert c.classify(rec("owner", "atlas", "Done: deployed.", spoke=True)) is None
    c.classify(rec("atlas", "owner", "deploy it"))
    assert c.classify(rec("owner", "atlas", "Done: deployed.", spoke=True)) == FOR_YOU
    assert c.classify(rec("owner", "atlas", "Also: logs clean.", spoke=True)) is None, (
        "one notification per thing he asked, not one per line")


def test_a_test_desk_never_notifies():
    """MEASURED: 14 of 81 pushes were `wake-probe (engineer test desk)`. Its
    roster `test` flag is False; only its label says so, and its turns were
    started by the deck and by messages posted as him. So the desk itself is
    what is checked, not who wrote to it."""
    c = oa.Classifier(boss_of=lambda n: None,
                      is_test=lambda n: n == "wake-probe")
    c.classify(rec("wake-probe", "owner", "say hi"))
    assert c.classify(rec("owner", "wake-probe", "hi", spoke=True)) is None
    assert c.classify(rec("owner", "wake-probe", "pick", kind="decision")) is None
    assert c.classify(rec("owner", "wake-probe", "fire", said=True, urgent=True)) is None
    # The good signal: a real desk in the same position still reaches him.
    c.classify(rec("atlas", "owner", "say hi"))
    assert c.classify(rec("owner", "atlas", "hi", spoke=True)) == FOR_YOU


def test_what_marks_a_test_desk():
    assert oa.is_test_desk(name="wake-probe", label="", test=False)
    assert oa.is_test_desk(name="x", label="wake-probe (engineer test desk)", test=False)
    assert oa.is_test_desk(name="x", label="", test=True)
    assert not oa.is_test_desk(name="acme-qa", label="Acme QA", test=False)
    assert not oa.is_test_desk(name="atlas", label="COS", test=False)


def test_a_reports_turn_final_prose_is_its_bosses_business():
    c = classifier()
    assert c.classify(rec("owner", "scout", "scanned 40 files", spoke=True)) is None


def test_a_report_he_wrote_to_answers_him_once():
    c = classifier()
    assert c.classify(rec("scout", "owner", "what did you find?")) is None
    assert c.classify(rec("owner", "scout", "three leaks", spoke=True)) == FOR_YOU
    # The next turn was not a reply to him: back to its boss's business.
    assert c.classify(rec("owner", "scout", "and a fourth", spoke=True)) is None


def test_progress_lines_do_not_buzz_him():
    c = classifier()
    assert c.classify(rec("owner", "atlas", "On it -- checking Acme first.",
                          said=True)) is None


def test_an_urgent_line_is_for_him():
    c = classifier()
    assert c.classify(rec("owner", "atlas", "prod is down", said=True,
                          urgent=True)) == FOR_YOU


def test_desk_to_desk_chatter_is_never_for_him():
    c = classifier()
    assert c.classify(rec("scout", "atlas", "look at the logs")) is None
    assert c.classify(rec("atlas", "scout", "found it", said=True)) is None


def test_the_decks_own_notices_and_his_own_words_are_not_news():
    c = classifier()
    assert c.classify(rec("owner", "atlas", "[Agent Deck] restarted",
                          restarted=True)) is None
    assert c.classify(rec("atlas", "owner", "please deploy")) is None
    assert c.classify(rec("owner", "deck", "system line", spoke=True)) is None
    assert c.classify({"ts": 2.0, "ack": "abc"}) is None


def test_an_engineer_probe_does_not_make_a_report_owe_him():
    """Only HIS words make a report's answer his. An engineer's probe on a
    test desk is not him asking."""
    c = classifier()
    c.classify(rec("scout", "engineer-test", "ping"))
    assert c.classify(rec("owner", "scout", "pong", spoke=True)) is None


def test_the_label_is_one_of_two_words():
    assert {NEEDS_YOU, FOR_YOU} == {"needs_you", "for_you"}


def test_a_desk_answering_the_engineer_is_not_talking_to_him():
    """MEASURED on the box, first day live: 33 of 50 alerts in 24h were a
    test desk answering the engineer's probes. A turn the engineer started is
    the engineer's answer, even from a desk that reports to Sam."""
    c = classifier()
    c.classify(rec("wake-probe", "engineer", "say one, two, three"))
    assert c.classify(rec("owner", "wake-probe", "One. Two. Three.", spoke=True)) is None
    c.classify(rec("wake-probe", "engineer-test", "and again"))
    assert c.classify(rec("owner", "wake-probe", "Again.", spoke=True)) is None
    # His own word puts it back in front of him.
    c.classify(rec("wake-probe", "owner", "are you there?"))
    assert c.classify(rec("owner", "wake-probe", "Here.", spoke=True)) == FOR_YOU
