"""A desk waits ONCE for a deploy or a long job -- it never polls.

MEASURED (48h of box transcripts): desks watched deploys with background loops
(`until grep ...; sleep 15; done`). Every background-job notice re-reads the
desk's whole ~400k context, so one polled deploy cost millions of tokens
(~96M a day, 20% of the Work account). The brief must tell every desk to use
ONE blocking wait with a timeout.
"""

from server import features, rules


def test_brief_tells_desks_to_wait_once_not_poll():
    text = features.section().lower()
    assert "wait once" in text
    assert "timeout" in text
    assert "background" in text and "poll" in text
    # and it rides the rules version, so running desks hear it on next message
    assert any("wait once" in line.lower() for line in rules.lines())
