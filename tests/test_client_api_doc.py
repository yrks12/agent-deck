"""`docs/client-api.md` is the contract. This is what stops it becoming fiction.

**Why this file exists.** The `blocked` field and the `pretrust` block shipped
in `server/api.py` and were described in a *report* instead of in the doc. The
macOS client went looking, found nothing, and reverse-engineered the key names
and the reason slugs out of the server source. It got them right. The next one
will not, and a client reading the server is precisely the drift the doc is for.

A claim is only true when the artefact carries it. These assert the artefact
carries it, and keep carrying it: a new `Trust` reason added to
`server/pretrust.py` fails here until the table in §3.1 names it. That matters
because the client renders a *sentence per slug* and falls back to a generic
message for one it does not know -- so an undocumented slug does not crash
anything, it just quietly shows the wrong words forever.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import re
from pathlib import Path

import pytest

from server import api as api_mod
from server import pretrust
from server import spawn as spawn_mod

DOC = Path(__file__).resolve().parent.parent / "docs" / "client-api.md"


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text()


def _trust_reasons() -> set[str]:
    """Every literal `reason` `server/pretrust.py` can return.

    Parsed, not grepped, so a reason added in a new `Trust(...)` is picked up
    without anyone remembering to update a list here.
    """
    tree = ast.parse((Path(pretrust.__file__)).read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "Trust" or len(node.args) < 2:
            continue
        reason = node.args[1]
        if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
            found.add(reason.value)
    return found


def test_every_reason_pretrust_can_return_is_named_in_the_doc(doc):
    """The GOOD signal: each slug appears verbatim. A client that meets an
    undocumented one shows its generic fallback for a specific cause."""
    reasons = _trust_reasons()
    assert reasons, "parsed no reasons at all -- this test has stopped working"

    missing = sorted(r for r in reasons if f"`{r}`" not in doc)
    assert not missing, (
        f"undocumented pretrust reasons: {missing}. Add them to the table in "
        f"docs/client-api.md §3.1 with a sentence a person can act on.")


def test_the_five_failure_slugs_are_in_one_table_with_guidance(doc):
    """Naming a slug somewhere in the file is not documenting it. Each failure
    reason needs its own row saying what the person can do."""
    section = doc[doc.index("### 3.1"):doc.index("### The preview line")]
    for slug in ("lock_busy", "unexpected_shape", "write_failed",
                 "not_deck_workspace", "engine_not_covered"):
        assert f"`{slug}`" in section, slug
    assert "state == \"OFFLINE\"" in section
    assert "clears by itself" in section


def test_the_row_the_server_sends_is_the_row_the_doc_describes(doc):
    """Both directions. A field in the payload and not in the doc is the bug
    that started this; a field in the doc and not in the payload is the one
    that wastes a client's afternoon."""
    for field in ("blocked", "pretrust", "muted"):
        assert field in doc, field
    assert "\"muted\"" in doc or "`muted`" in doc


def test_the_interview_response_shape_in_the_doc_matches_the_server(doc):
    """The 201 sample is what a client codes against. It has to carry the same
    keys `interview_agent` actually returns."""
    section = doc[doc.index("### 11.1"):doc.index("### 11.2")]
    for key in ("provisional", "thread_id", "pretrust", "muted", "reason"):
        assert key in section, key
    # ok: false must not read as a failed hire -- the desk exists.
    assert "does **not** mean the hire failed" in section


def test_the_startup_gates_are_documented_including_the_one_not_handled(doc):
    """The gate we chose not to close is the one a reader most needs told. Two
    surprises in two cycles came from a modal nobody had written down."""
    section = doc[doc.index("### 11.2"):doc.index("### `DELETE /v1/agents")]
    assert "Quick safety check" in section
    assert "New MCP server found" in section
    assert "external includes" in section
    assert "codex" in section and "opencode" in section


def test_pretrust_is_wired_into_the_route_the_doc_points_at(doc):
    """Guards against the inverse failure: a doc describing a field the server
    stopped sending.

    The trust write moved out of `api.interview_agent` and into
    `spawn.spawn_terminal`, because `api.start_agent` and `app.roster_start`
    were starting sessions without it. So the wiring is checked where it now
    lives -- on the path all three doors share -- and the surface is still
    checked for the two fields the doc promises a client.
    """
    assert "pretrust.pretrust(" in Path(spawn_mod.__file__).read_text()
    source = Path(api_mod.__file__).read_text()
    assert '"blocked"' in source
    assert '"pretrust": started["pretrust"]' in source
    assert '"muted": list(vouched.muted)' in Path(
        spawn_mod.__file__).read_text()


def test_the_relay_is_published_in_the_contract_not_in_a_report(doc):
    """A direct thread now returns messages whose `thread_id` is a `peer:` id.

    That is the whole wire attribution -- there is no new field to discover --
    so a client that has not been told will render a worker's dense counts as
    if the desk had said them to the owner. This is exactly the drift this file
    exists for: the mechanism ships in `Surface._fan` and must be in §6.2, not
    in an agent's report.
    """
    assert hasattr(api_mod.Surface, "_fan"), "the relay is gone; delete this"
    section = doc[doc.index("### 6.2"):doc.index("### `POST /v1/threads/")]
    # The rule a client renders from, and the two labels it renders.
    assert "`thread_id` on a message is not always the thread you asked for" \
        in section
    assert "to <name>" in section and "from <name>" in section
    # And the two things that stop it being read as a firehose or a duplicate.
    assert "both parties" in section
    assert "same `id`" in section


# -- the per-agent preferences, and whether the SERVER acts on any of them ---
#
# A switch that does nothing is worse than an absent switch: he flips it, the
# product ignores him, and he has no way to tell a broken feature from a
# misunderstood one. `notifications` was exactly that until per-desk mute
# shipped -- and the doc has gone on saying "nothing consumes it yet", which
# is the same defect pointing the other way: a client author reading that
# hides a switch that now works.
#
# Swept, not listed. The keys come out of `Surface.patch_agent` and the
# consumers come out of the `server/` package, so a key added to the PATCH, or
# a key that grows its first real consumer, fails here until the doc says what
# the server does with it.

PREF_TABLE_HEADING = "### 9.1 What the server actually does with a preference"

#: A dict read, in either spelling. Prose that merely mentions the word is not
#: a consumer, and counting it as one is how a table like this starts lying.
_READ = r'(?:get\(|\[)\s*["\']{key}["\']'


def _patchable_pref_keys() -> set[str]:
    """Every key `PATCH /v1/agents/{name}` can write into the prefs sidecar.

    Parsed out of `patch_agent`, not typed here, so this sweeps the class: a
    sixth preference added to that method is covered the day it lands.
    """
    source = Path(api_mod.__file__).read_text()
    tree = ast.parse(source)
    function = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "patch_agent"), None)
    assert function is not None, \
        "Surface.patch_agent is gone -- this sweep has stopped working"

    keys: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.For):
            continue
        if "pref_patch" not in (ast.get_source_segment(source, node) or ""):
            continue
        if isinstance(node.iter, (ast.Tuple, ast.List)):
            keys.update(element.value for element in node.iter.elts
                        if isinstance(element, ast.Constant)
                        and isinstance(element.value, str))
    return keys


def _server_consumers(key: str) -> list[str]:
    """The modules outside `server/api.py` that READ this preference."""
    import re

    pattern = re.compile(_READ.format(key=re.escape(key)))
    server_dir = Path(api_mod.__file__).resolve().parent
    return sorted(path.relative_to(server_dir.parent).as_posix()
                  for path in server_dir.rglob("*.py")
                  if path.name != "api.py" and pattern.search(path.read_text()))


def _pref_row(doc: str, key: str) -> str:
    assert PREF_TABLE_HEADING in doc, (
        f"docs/client-api.md has no {PREF_TABLE_HEADING!r} section. The client "
        "has no way to tell a preference the server acts on from one it only "
        "stores, which is how a dead switch ships.")
    table = doc[doc.index(PREF_TABLE_HEADING):]
    table = table[:table.index("\n---")]
    row = next((line for line in table.splitlines()
                if line.startswith(f"| `{key}`")), "")
    assert row, (
        f"`{key}` can be written by PATCH /v1/agents/<name> and has no row in "
        f"docs/client-api.md {PREF_TABLE_HEADING!r}. Say what the server does "
        "with it, or the client ships a switch nobody can account for.")
    return row


def test_every_preference_the_client_can_write_says_what_the_server_does(doc):
    """The good signal: a row per key, naming the module that acts on it.

    MEASURED on this tree: `avatar` is read by `server/onboard.py` and
    `server/roster.py` (a desk names its own), `notifications` by
    `server/deskpage.py` (per-desk mute), and `pinned` and `section` by
    nothing at all.
    """
    keys = _patchable_pref_keys()
    assert keys, "parsed no preference keys -- this test has stopped working"

    for key in sorted(keys):
        row = _pref_row(doc, key)
        for module in _server_consumers(key):
            assert module in row, (
                f"{module} reads the {key!r} preference and the doc's row for "
                f"it does not say so: {row!r}. A client author reads this row "
                "to decide whether the switch is worth shipping.")


def test_a_preference_no_module_reads_is_declared_client_only(doc):
    """The other half, and the one that keeps him from flipping a dead switch.

    `pinned` and `section` are real preferences with no server behaviour, and
    that is a legitimate design -- §3 already says the server imposes no order
    and the client sorts. What is NOT legitimate is leaving a client author to
    discover it. The row has to say it in words.
    """
    keys = _patchable_pref_keys()
    assert keys, "parsed no preference keys -- this test has stopped working"

    inert = [key for key in sorted(keys) if not _server_consumers(key)]
    assert inert, "every preference now has a consumer -- retire this test"

    for key in inert:
        row = _pref_row(doc, key)
        assert "client presentation only" in row, (
            f"nothing under server/ reads the {key!r} preference, so the doc "
            f"must call it client presentation only. Row reads: {row!r}")


# ── the handoff routes: served, called every two seconds, documented nowhere ──
#
# MEASURED on 2026-09-07: his Mac polls `GET /v1/handoffs` every ~2s, 200 OK,
# and `docs/client-api.md` runs 12 (approvals) -> 13 (routines) -> 14 (groups)
# -> 15 (screen) -> 16 (terminal). There is no handoffs section at all. The
# stated contract is silent on a feature the app is already calling.
#
# That is worse than an undocumented route. `server/handoff.py` opens by saying
# this is NOT the approval layer: an approval is a permission the desk could
# have been granted, a handoff is a step the desk CANNOT take at all -- a 2FA
# code, a CAPTCHA, an SMS confirmation, `gh auth login`. A client author with
# only the doc in front of them has no way to learn that, and the one paragraph
# that does mention handoffs (§15) gets three facts wrong.


def _handoff_section(doc: str) -> str:
    """The handoffs section, or "" -- so a missing section fails loudly."""
    start = doc.find("## 12.5")
    if start < 0:
        return ""
    end = doc.find("\n## ", start + 1)
    return doc[start:end if end > 0 else len(doc)]


def test_the_handoff_routes_are_in_the_contract_at_all(doc):
    """Both routes, named, in a section of their own.

    Asserts the presence of the section rather than the absence of a gap: a
    grep for "handoff" already passes today on the paragraph that is wrong.
    """
    section = _handoff_section(doc)
    assert section, (
        "docs/client-api.md documents no handoff routes. The app polls "
        "GET /v1/handoffs every two seconds against the live deck and answers "
        "on POST /v1/handoffs/{id}; a client author reading the contract "
        "would not know either route exists")
    for route in ("GET /v1/handoffs", "POST /v1/handoffs/"):
        assert route in section, f"{route} is not in the handoffs section"


def test_the_two_verbs_are_documented_as_different_instructions(doc):
    """`done` and `skipped`, and what each one MEANS.

    Rule 2 of server/handoff.py: they are different instructions, not different
    words -- `done` means the desk re-checks that the step actually worked,
    `skipped` means it abandons that path and reports what it cannot finish.
    Collapse them and a skipped handoff becomes an infinite retry loop. A doc
    that lists the verbs without that distinction invites exactly that.
    """
    section = _handoff_section(doc)
    assert "`done`" in section and "`skipped`" in section, (
        "the handoffs section does not name both verbs")
    assert "re-check" in section or "recheck" in section, (
        "the doc does not say that `done` makes the desk RE-CHECK the step "
        "actually worked -- without it, a client author reads `done` as "
        "'dismiss the card'")
    assert "abandon" in section, (
        "the doc does not say that `skipped` makes the desk ABANDON that path "
        "for good; collapsing the two verbs is an infinite retry loop")


def test_the_verb_the_server_refuses_is_not_the_one_the_doc_teaches(doc):
    """§15 taught `skip`. The server answers 400 to `skip`.

    MEASURED: `Surface.HANDOFF_OPTIONS` offers `done` and `skipped` only, and
    `resolve_handoff` reads `payload.get("reply")`. A client written to the old
    §15 sentence -- "he answers the handoff `done` / `skip`" -- gets
    400 bad_outcome on every tap, and the desk stays stopped forever.
    """
    from server import api as api_mod

    offered = {verb for verb, _ in api_mod.Surface.HANDOFF_OPTIONS}
    assert offered == {"done", "skipped"}, (
        f"the server now offers {sorted(offered)}; this test and the doc are "
        "both stale")
    # The doc may WARN about `skip`; what it must not do is teach it as an
    # answer. Checking for the bare word failed on the warning itself, which is
    # the good signal -- so this looks for the shapes that offer it as a verb.
    for taught in ("`done` / `skip`", "`done`/`skip`", "answers the handoff `skip`"):
        assert taught not in doc, (
            f"the doc offers {taught!r} as the answer. The server refuses "
            "`skip` with 400 bad_outcome, so every tap would leave the desk "
            "stopped forever")
    assert "Not `skip`" in doc, (
        "nothing in the doc warns a client author off `skip`, and §15 taught "
        "it for long enough that a client may already send it")


def test_the_doc_does_not_invent_fields_the_server_never_sends(doc):
    """§15 promised `surface=browser`. No such field exists.

    `server/handoff.py`'s `Handoff` carries id, ts, agent, kind, needs, state,
    where, evidence, status. A client branching on `surface` branches on a key
    that never arrives, so the browser take-over it was supposed to trigger
    never happens -- and nothing errors.
    """
    from server import handoff as handoff_mod

    fields = {f.name for f in dataclasses.fields(handoff_mod.Handoff)}
    assert "surface" not in fields, (
        "the server grew a `surface` field; this test is stale, not the doc")
    assert "surface=browser" not in doc, (
        "the doc tells a client to branch on `surface=browser`, which the "
        "server has never sent")


# -- §6.3 `delivery` ---------------------------------------------------------
#
# The client renders a sentence per slug and a marker per state, and it is the
# FAILURE marker that this whole field exists for. An undocumented state or
# reason does not crash anything -- it draws the generic fallback forever,
# which is how five messages to a dead desk came to look like five that landed.


def _delivery_section(doc: str) -> str:
    start = doc.index("### 6.3")
    return doc[start:doc.index("### `POST /v1/threads", start)]


def test_every_delivery_state_the_server_can_send_is_documented(doc):
    """Read out of `api.delivery_of` itself, so a fourth branch added there
    fails here until someone decides what the app draws for it."""
    source = inspect.getsource(api_mod.delivery_of)
    states = set(re.findall(r'"state": "(\w+)"', source))
    assert states == {"delivered", "sent", "undelivered"}, (
        f"delivery_of's state set changed to {sorted(states)}; the doc and "
        "every client's marker table are now stale")

    section = _delivery_section(doc)
    for state in sorted(states):
        assert f"`{state}`" in section, state


def test_every_undelivered_reason_is_a_row_a_person_can_act_on(doc):
    """Naming a slug is not documenting it. `no_session` means start the desk;
    `desk_blocked` means it needs hands at that machine. Those are different
    actions, and a client that cannot tell them apart tells him neither."""
    section = _delivery_section(doc)
    for slug in (api_mod.NO_SESSION, api_mod.DESK_BLOCKED):
        assert f"`{slug}`" in section, slug
    assert "start the desk" in section
    assert "hands at that machine" in section


def test_the_doc_does_not_promise_a_read_receipt(doc):
    """The one state the system cannot prove. Both ack writers -- `office.ack`
    after a socket push and `hooks/cc-office.js` on a turn -- append the same
    bare record, so "read" would be a guess on the screen this field exists to
    make honest. If the server ever learns to tell them apart, this test is
    what says the doc has to be rewritten before a client shows the tick."""
    source = inspect.getsource(api_mod.delivery_of)
    assert '"state": "read"' not in source, (
        "the server grew a `read` state; this test is stale, not the doc -- "
        "but only ship it if the ack record can actually distinguish them")
    assert '"state": "read"' not in doc, "the doc promises a receipt nobody sends"
    # Asserted as the DENIAL, not the absence of the word: the section has to
    # explain why the tick is missing, or the next client to want one adds it.
    assert "There is no `read` state" in _delivery_section(doc)


def test_the_doc_tells_the_client_the_state_moves(doc):
    """The load-bearing sentence, and it reads like housekeeping. A client that
    caches the first `delivery` it saw leaves a permanent red mark on a message
    that arrived fine the moment the desk came back."""
    section = _delivery_section(doc).lower()
    assert "recomputed on every read" in section
    assert "re-render" in section
