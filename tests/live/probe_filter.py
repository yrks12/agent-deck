"""Pure helpers so a live line grades ONLY the replies to probes it sent itself."""


def replies_to_probes(msgs, probe_ids):
    """The first real agent message after each probe id, in probe order.

    `msgs` is a thread oldest-first. Old replies (before the first probe) and
    replies to anyone else's messages never appear: a probe with no reply yet
    contributes nothing, so a short result means "not answered", never "graded
    something stale". `[Agent Deck]` lines are not replies.
    """
    ids = list(probe_ids)
    at = {m["id"]: i for i, m in enumerate(msgs)}
    out = []
    for pid in ids:
        if pid not in at:
            continue
        start = at[pid]
        stop = min((at[o] for o in ids if o in at and at[o] > start), default=len(msgs))
        first = next((m for m in msgs[start + 1:stop]
                      if m["role"] == "agent" and not m["text"].startswith("[Agent Deck]")), None)
        if first:
            out.append(first)
    return out


def require_chief(env):
    """Which desk the B lines may message. None = not configured (skip).

    There is deliberately NO default: the owner's real chief is only ever
    messaged when `DECK_CHIEF_DESK` names it explicitly.
    """
    return (env.get("DECK_CHIEF_DESK") or "").strip() or None
