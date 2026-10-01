"""When the agent must stop driving, and how the human gets the keyboard.

`server/browser.py` gives a desk a real Chrome. This is the loop that makes it
safe to leave one running: the agent works until it hits a step it must never
attempt, stops, tells the owner exactly where things stand, waits while he does that
step on the same screen, and is told what happened.

The list of steps an agent must never attempt is the reference product's own
(spec M10 -- passwords, passkeys, 2FA, CAPTCHAs, payment confirmation), plus
the "confirm it's you" interstitial. We copy the list and change how it is
detected.

**Structure, never page text.** Text matching fails in both directions at once:

* it **misses** the real thing. A Stripe card field lives in a cross-origin
  iframe whose text the page cannot read at all, so the page about to take £39
  may not contain the word "card" anywhere;
* it **fires** on an article. A blog post titled "Why passwords are dead"
  carries every trigger word, and three false 2 a.m. handoffs teach the owner to
  ignore the queue -- at which point the real one is ignored too.

So the signals here are facts about the DOM and the frame tree: an
`<input type="password">` that is actually visible, a frame whose *whole
origin* is a known payment or challenge host, an `autocomplete` token from the
web standard, a CDP interstitial. `page_state["text"]` is never read. If that
ever changes, the "article about passwords" test in
tests/test_browser_takeover.py is what fails.

**One queue.** A browser block is a handoff with a URL, so it goes through
`handoff.raise_handoff` into the same `handoffs.json` everything else uses. A
second queue means a second place to look, and the one nobody looks at is the
one with the blocked agent in it.

**Surface.** Every block records that it happened on the *browser* surface and
carries the page's **origin** separately from its URL. An always-allow rule for
"advance the checkout on `shop.example.com`" must not silently authorise the
same action on the Mac, and a rule must be written against an origin, never a
URL with a session id in it. `server/autoreview.py` does not key on `surface`
today -- that gap is named in docs/the-browser.md, not quietly bridged here.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from typing import Callable

from . import atomic, handoff
from .browser import BrowserSession, origin_of, redactor_for

# The surface an action happened on. A rule granted here must not authorise
# the same action on the Mac; see the module docstring and docs/the-browser.md.
SURFACE = "browser"

# Kinds this module can detect, all of them already accepted by the handoff
# queue. "device_code" is in handoff.KINDS but has no structural signal on a
# web page, so it is not detected here -- an agent raises that one itself.
DETECTED_KINDS = ("payment", "2fa", "captcha", "login", "other")

# Whole-origin matches, never substrings: `https://js.stripe.com.evil.example`
# contains the real origin and is a different site.
PAYMENT_ORIGINS: frozenset[str] = frozenset({
    "https://js.stripe.com",
    "https://checkout.stripe.com",
    "https://hooks.stripe.com",
    "https://pay.google.com",
    "https://www.paypal.com",
    "https://www.sandbox.paypal.com",
    "https://checkout.paypal.com",
    "https://checkoutshopper-live.adyen.com",
    "https://assets.braintreegateway.com",
    "https://pay.klarna.com",
    "https://applepay.cdn-apple.com",
})

CAPTCHA_ORIGINS: frozenset[str] = frozenset({
    "https://www.google.com",
    "https://www.recaptcha.net",
    "https://hcaptcha.com",
    "https://newassets.hcaptcha.com",
    "https://challenges.cloudflare.com",
    "https://geo.captcha-delivery.com",
})

# Tokens from the HTML autocomplete standard. These are what a browser's own
# autofill keys on, which makes them the most reliable structural signal a page
# offers -- and unlike a class name or a placeholder, they are not prose.
PASSWORD_AUTOCOMPLETE = frozenset({"current-password", "new-password"})
OTP_AUTOCOMPLETE = frozenset({"one-time-code"})
PAYMENT_AUTOCOMPLETE = frozenset({
    "cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year",
})

_PHONE_LIMIT = handoff.PHONE_LIMIT

# ── passkeys ───────────────────────────────────────────────────────────────
# A passkey lives on the owner's phone or Mac, never in a desk's container, so
# a passkey-first sign-in dead-ends ("No passkeys available"). Detection is by
# whole host AND a path shape -- never by text, so a blog post about passkeys
# is not a challenge.
_PASSKEY_PATH = re.compile(
    r"/(challenge/pk|webauthn|fido|passkeys?)(/|$)", re.IGNORECASE)
_PASSKEY_HOST = re.compile(
    r"\A(accounts\.google\.com|github\.com|login\.live\.com|"
    r"login\.microsoftonline\.com|appleid\.apple\.com|idmsa\.apple\.com)\Z")


def is_passkey_challenge(url: str) -> bool:
    """Is this URL an identity provider's passkey prompt? PURE."""
    try:
        origin_of(str(url or ""))
    except ValueError:
        return False
    from urllib.parse import urlsplit
    parts = urlsplit(str(url).strip())
    return bool(_PASSKEY_HOST.match(parts.hostname or "")
                and _PASSKEY_PATH.search(parts.path or ""))


# Clicks the provider's own "use something else" control. It reads labels and
# clicks; it never focuses a field or sets a value, so no credential is typed.
_ANOTHER_WAY_JS = """(async () => {
  const want = /(try another way|use your password|use (a )?password|enter your password|other ways? to sign in|use another method|use a different method|sign in another way)/i;
  for (let i = 0; i < 16; i++) {
    const els = [...document.querySelectorAll('button, a, [role=button], [role=link]')];
    const hit = els.find((el) => want.test((el.innerText || el.textContent || '').trim()));
    if (hit) { hit.click(); return 'clicked'; }
    await new Promise((r) => setTimeout(r, 250));
  }
  return 'not-found';
})()"""


def passkey_steer_commands() -> list[tuple[str, dict]]:
    """CDP to run on ONE connection at a passkey page. PURE.

    A virtual authenticator with NO credentials: the browser answers the
    WebAuthn request itself (nothing to use), so no OS "passkey" sheet is
    left on the screen, and the page falls back to its other methods. Then
    click "Try another way". Nothing is ever typed.
    """
    return [
        ("WebAuthn.enable", {}),
        ("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal",
            "hasResidentKey": True, "hasUserVerification": True,
            "isUserVerified": True,
            "automaticPresenceSimulation": True}}),
        ("Runtime.evaluate", {"expression": _ANOTHER_WAY_JS,
                              "awaitPromise": True, "returnByValue": True}),
    ]


def passkey_needs(sess: BrowserSession) -> str:
    """What the card says, plainly. PURE."""
    return (f"This site wants a passkey. Passkeys are on your own phone/Mac, "
            f"not {sess.desk.capitalize()}'s computer — on the screen choose "
            f"'Try another way' → password or phone prompt.")


# ── detection ──────────────────────────────────────────────────────────────


def _inputs(page_state: dict) -> list[dict]:
    rows = page_state.get("inputs")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


#: A frame smaller than this is not a field a person can type into. Stripe's
#: controller and metrics frames are 1px high (and `visibility: hidden`); the
#: smallest real card element is ~19px high and a few hundred wide.
MIN_FRAME_WIDTH = 60
MIN_FRAME_HEIGHT = 10


def _frames(page_state: dict) -> list[tuple[str, str, dict]]:
    """Every frame as (origin, url, row), normalised.

    A frame may report an `origin` directly (CDP does) or only a `url`, in
    which case the origin is derived. Anything that is not a web origin --
    `about:blank`, a `data:` frame -- is dropped rather than guessed at.
    """
    rows = page_state.get("frames")
    if not isinstance(rows, list):
        return []
    out: list[tuple[str, str, dict]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for candidate in (row.get("origin"), row.get("url")):
            if not candidate:
                continue
            try:
                out.append((origin_of(str(candidate)),
                            str(row.get("url") or candidate), row))
            except ValueError:
                continue
            break
    return out


def _shown_frame(row: dict) -> bool:
    """Could a person see and use this frame?

    **Presence is not a signal.** MEASURED 2026-09-30: every page of
    dashboard.vapi.ai carries two `https://js.stripe.com` frames injected by
    Stripe.js -- `visibility: hidden`, 1px high -- and no card field at all.
    Counting those made a whole dashboard "payment" and walled a desk off its
    API-keys page. So a frame counts only when the driver says it is visible
    AND it has a real size; a frame reported without geometry does not count.
    """
    if row.get("hidden") is not False:
        return False
    try:
        width = float(row.get("width") or 0)
        height = float(row.get("height") or 0)
    except (TypeError, ValueError):
        return False
    return width >= MIN_FRAME_WIDTH and height >= MIN_FRAME_HEIGHT


def _shown_origins(page_state: dict) -> set[str]:
    return {origin for origin, _, row in _frames(page_state)
            if _shown_frame(row)}


# A challenge origin that also serves ordinary embeds (Maps on www.google.com)
# only counts on its challenge path.
_CAPTCHA_PATH = re.compile(r"/(recaptcha|captcha|turnstile|cdn-cgi/challenge)",
                           re.IGNORECASE)


def _is_captcha_frame(origin: str, url: str, row: dict) -> bool:
    if origin not in CAPTCHA_ORIGINS or not _shown_frame(row):
        return False
    parts = urlsplit(url)
    if "size=invisible" in (parts.query + "&" + parts.fragment).lower():
        return False  # reCAPTCHA v3 / invisible v2: nothing to solve
    if origin in ("https://www.google.com", "https://www.recaptcha.net"):
        return bool(_CAPTCHA_PATH.search(parts.path or ""))
    return True


def _visible(field: dict) -> bool:
    """Is this input one a person could actually type into?

    Password managers and framework autofill shims leave hidden password
    inputs on ordinary pages. Blocking on those fills the queue with handoffs
    for pages that have no visible form, which is the "cry wolf" failure mode
    this module exists to avoid.
    """
    if field.get("hidden") is True:
        return False
    if field.get("type") == "hidden":
        return False
    return not (field.get("disabled") is True)


def _autocomplete(field: dict) -> str:
    return str(field.get("autocomplete") or "").strip().lower()


def needs_human(page_state: dict) -> str | None:
    """The handoff kind this page demands, or None. PURE.

    `page_state` is the structural snapshot a driver reports:

        {"url": "https://shop.example.com/checkout",
         "inputs": [{"type": "password", "hidden": False,
                     "autocomplete": "current-password"}],
         "frames": [{"origin": "https://js.stripe.com"}],
         "interstitial": False}

    `text` may be present. It is never read.

    **The order is payment, 2FA, CAPTCHA, login, interstitial**, and it is
    load-bearing on a checkout page that carries several at once. Money is the
    irreversible one: the brief the owner reads at 2 a.m. has to be about the £39
    charge, not about the sign-in box next to it.
    """
    if not isinstance(page_state, dict):
        return None

    fields = _inputs(page_state)
    visible = [f for f in fields if _visible(f)]
    autos = {_autocomplete(f) for f in visible}

    # Everything below is about what a person can SEE. A payment or challenge
    # origin that is merely present -- Stripe.js's hidden controller frames,
    # an invisible reCAPTCHA -- is how a whole dashboard became "payment".
    if _shown_origins(page_state) & PAYMENT_ORIGINS \
            or autos & PAYMENT_AUTOCOMPLETE:
        return "payment"

    if autos & OTP_AUTOCOMPLETE:
        return "2fa"

    if any(_is_captcha_frame(o, u, r) for o, u, r in _frames(page_state)) \
            or any(f.get("sitekey") and str(f.get("size") or "").lower()
                   != "invisible" for f in visible):
        return "captcha"

    if any(str(f.get("type") or "").lower() == "password" for f in visible) \
            or autos & PASSWORD_AUTOCOMPLETE:
        return "login"

    if page_state.get("interstitial") is True:
        return "other"

    return None


# ── the scope a rule could be written from ─────────────────────────────────


def rule_scope(sess: BrowserSession, kind: str, url: str) -> dict:
    """Surface, desk, origin, kind -- and nothing that could ever expire. PURE.

    This is the shape a durable always-allow rule would be written from:
    "this desk may advance a payment step on `https://shop.example.com`".

    Two things are deliberately absent. The **full URL**, because a rules file
    is permanent, is read by every desk, and is not the vault -- a session id
    or one-time token stored there is a leak with a scope attached. And any
    notion of the Mac: `surface` is what stops a browser grant from being read
    as permission to run the same thing in a shell.
    """
    return {
        "surface": SURFACE,
        "desk": sess.desk,
        "origin": origin_of(url),
        "kind": _check_kind(kind),
    }


def _check_kind(kind: str) -> str:
    value = str(kind or "")
    if value not in handoff.KINDS:
        raise ValueError(f"kind must be one of {handoff.KINDS}, got {kind!r}")
    return value


# ── Allow / Allow always: the owner's answer, remembered ─────────────────
#
# Owner ruling 2026-09-30: nothing is dead-ended. A stop is ONE card; "Allow"
# lets the desk carry on by itself, "Allow always" does so for that desk and
# site for good. MEASURED before this existed: card fjuq on dashboard.vapi.ai
# was answered `done` and 4bxj was raised on the same origin three minutes
# later -- nothing between his answer and the next guard check remembered it.

GRANTS_FILE = "browser_grants.json"

#: How long a plain "Allow" covers that desk + site + kind. Long enough to
#: finish the step it was asked for; a new session next week is asked again.
ONCE_TTL = 3600.0

MAX_GRANTS = 500


def grants_path(handoffs_path: Path) -> Path:
    """The grants live beside the handoff queue they answer. PURE."""
    return Path(handoffs_path).parent / GRANTS_FILE


def _load_grants(path: Path) -> list[dict]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = raw.get("grants") if isinstance(raw, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def grant(path: Path, *, desk: str, origin: str, kind: str,
          always: bool) -> dict:
    """Record the owner's Allow for desk + origin + kind. Returns the row."""
    row = {"surface": SURFACE, "desk": str(desk), "origin": origin_of(origin),
           "kind": _check_kind(kind), "always": bool(always),
           "at": time.time()}
    same = (row["desk"], row["origin"], row["kind"])
    kept = [r for r in _load_grants(path)
            if (r.get("desk"), r.get("origin"), r.get("kind")) != same
            or (r.get("always") and not always)]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, json.dumps(
        {"version": 1, "grants": (kept + [row])[-MAX_GRANTS:]}))
    return row


def granted(path: Path, *, desk: str, origin: str, kind: str) -> str | None:
    """"always", "once", or None -- read from disk every time, so a grant
    written by the API process is honoured by the desk's next click."""
    try:
        want = (str(desk), origin_of(origin), str(kind))
    except ValueError:
        return None
    now = time.time()
    best = None
    for r in _load_grants(path):
        if (r.get("desk"), r.get("origin"), r.get("kind")) != want \
                or r.get("surface", SURFACE) != SURFACE:
            continue
        if r.get("always") is True:
            return "always"
        try:
            if now - float(r.get("at") or 0) < ONCE_TTL:
                best = "once"
        except (TypeError, ValueError):
            continue
    return best


_EVIDENCE = re.compile(r"(?:^|\s)(surface|desk|origin)=(\S+)")


def scope_of(h: handoff.Handoff) -> dict | None:
    """The browser scope a card was raised for, or None if it is not one."""
    found = dict(_EVIDENCE.findall(h.evidence or ""))
    if found.get("surface") != SURFACE or not found.get("origin"):
        return None
    try:
        origin = origin_of(found["origin"])
    except ValueError:
        return None
    return {"surface": SURFACE, "desk": found.get("desk") or h.agent,
            "origin": origin, "kind": h.kind}


def allowed_brief(h: handoff.Handoff, *, always: bool) -> str:
    """What the desk is told when the owner taps Allow. PURE."""
    scope = scope_of(h) or {"origin": h.where}
    span = ("from now on" if always else
            f"for the next {int(ONCE_TTL // 60)} minutes")
    wants = _WANTS.get(h.kind, _WANTS["other"])
    return (f"Handoff {h.id} resolved: ALLOWED. The owner approved you to "
            f"{wants} on {scope['origin']} {span}. Carry on and do it yourself -- "
            f"your browser is unlocked for it; clicks and typing there now go "
            f"through. Re-read the page first, then continue."
            + _SIGN_IN_NOTE.get(h.kind, ""))


#: Said on every Allow for a sign-in kind. It OVERRIDES the brief a running
#: desk was spawned with, which is frozen and may still say "never type a
#: password" -- MEASURED: a growth desk quoted that line back to the owner
#: after he had tapped Allow and told it to do it itself.
_PASSWORD_NOTE = (
    " You may create accounts and sign in yourself: this approval overrides "
    "any earlier line telling you not to type passwords. For every password "
    "field (sign-up, confirm, sign-in) call mcp__computer__type_password -- "
    "the deck generates a strong password or reuses the saved one, keeps it "
    "in the vault for the owner and the other desks, and types it; you never "
    "see it, so never put a password in chat or in type_text. If an email "
    "code is needed, read it yourself if you have mail access; otherwise ask "
    "the owner for just that code.")
_SIGN_IN_NOTE = {"login": _PASSWORD_NOTE, "2fa": _PASSWORD_NOTE,
                 "other": _PASSWORD_NOTE}


# ── what the owner is told ──────────────────────────────────────────────────────

_ASK = {
    "payment": "do the payment confirmation yourself",
    "login": "sign in yourself",
    "2fa": "enter the two-factor code yourself",
    "captcha": "solve the CAPTCHA yourself",
    "device_code": "enter the device code yourself",
    "other": "do this step yourself",
}

_STOPPED_AT = {
    "payment": "a payment confirmation",
    "login": "a sign-in",
    "2fa": "a two-factor prompt",
    "captcha": "a CAPTCHA",
    "device_code": "a device code",
    "other": "a step it must not attempt",
}


def takeover_brief(sess: BrowserSession, reason: str, url: str, *,
                   redact: Callable[[str], str] | None = None) -> str:
    """The message that lands on the owner's phone. PURE (given `redact`).

    Same shape as `handoff.compose`, which is already proven on a phone, and
    the same reason for the order: **state before the ask**. A message that
    says "I need you" and nothing about whether £39 has already left is a
    message that gets a laptop opened in a panic over a page that never
    submitted. The state line is what decides whether this is urgent, so it is
    the second thing read and it is bold.

    The state sentence is narrow enough to be true every time: this function is
    only ever called *because* the agent stopped, so nothing on this page has
    been submitted and nothing has been paid at this step. It does not claim
    anything about work the desk did earlier -- that is `handoff.state`'s job
    when an agent raises its own.

    `redact` defaults to the shape rules alone. Pass `browser.redactor_for(...)`
    to also strip the desk's vault values, which is what any caller with a
    vault path should do.
    """
    kind = _check_kind(reason)
    scrub = redact or handoff.redact
    origin = origin_of(url)
    where = scrub(url)
    if len(where) > handoff.FIELD_LIMIT:
        where = where[:handoff.FIELD_LIMIT - 1].rstrip() + "…"

    return "\n".join([
        f"*{sess.desk}'s screen* stopped at {_STOPPED_AT[kind]}.",
        "",
        ("*State: nothing on this page has been submitted and no payment has "
         "been made at this step. The agent has stopped and will not touch "
         "the screen while you have it.*"),
        "",
        f"You: take the browser over and {_ASK[kind]}.",
        "",
        f"Where: {where}",
        f"Site: {scrub(origin)} — {sess.desk}'s screen, display {sess.display}",
        "",
        ("Hand back when you are done. Whatever you sign into stays signed in "
         "for the agent — it is the same browser."),
    ])[:_PHONE_LIMIT]


# ── what the agent is told ─────────────────────────────────────────────────

# `done` and `skipped` must never collapse into one another -- the same rule
# `handoff.resume_message` is built on. `done` is a human's *claim*, so it
# orders a re-check; `skipped` orders abandonment, because an agent that reads
# a skip as "retry later" hammers a login screen until the account locks.
_RESUME = {
    "done": (
        "A human says they finished it in your browser. Do NOT assume it "
        "worked: reload the page and re-check the exact condition that "
        "blocked you, then read the result before carrying on. If it still "
        "blocks, raise a new handoff describing what you see — do not loop."
    ),
    "skipped": (
        "That step will NOT happen. Abandon that path entirely and do not "
        "retry it, now or later. Report plainly what you can no longer "
        "finish, then continue with whatever does not depend on it."
    ),
    "taken_over": (
        "A human has the keyboard on this display right now. Hands off: send "
        "nothing to the browser, do not navigate, do not click, and do not "
        "retry. Wait to be told it is done or skipped, and meanwhile do only "
        "work that does not touch this browser."
    ),
}


def resume_brief(sess: BrowserSession, outcome: str) -> str:
    """What is injected back into the blocked agent's session. PURE.

    The last sentence is the most valuable fact about this whole design and it
    is repeated in all three cases: the human's sign-in is already the agent's,
    because it is the same Chrome and the same profile directory. An agent that
    does not know that walks straight back into the login it was just rescued
    from.
    """
    if outcome not in handoff.OUTCOMES:
        raise ValueError(
            f"outcome must be one of {handoff.OUTCOMES}, got {outcome!r}")
    return (
        f"Browser handoff resolved: {outcome}. Your screen is display "
        f"{sess.display}. {_RESUME[outcome]} Any sign-in, cookie or session "
        f"the human created is already yours — it is the same browser and the "
        f"same profile — so do not repeat a login you now have."
    )


_WANTS = {
    "payment": "confirm a payment",
    "login": "sign in",
    "2fa": "enter a two-factor code",
    "captcha": "get past a CAPTCHA",
    "device_code": "enter a device code",
    "other": "continue past a browser warning",
}


def approve_needs(sess: BrowserSession, kind: str, origin: str) -> str:
    """The card's one sentence: who wants what, where, and the three answers."""
    return (f"{sess.desk.capitalize()} wants to {_WANTS[_check_kind(kind)]} "
            f"on {origin}. Allow (it carries on by itself), Allow always for "
            f"this site, or No. You can also take over the screen.")


# ── raising it ─────────────────────────────────────────────────────────────


def raise_browser_handoff(path: Path, sess: BrowserSession, *, kind: str,
                          url: str, passkey: bool = False,
                          vault_path: Path | None = None) -> handoff.Handoff:
    """Put a browser block on the existing handoff queue.

    Reuses `handoff.raise_handoff` rather than inventing a second store, so a
    browser block appears on the board, expires on the same clock, goes stale
    when its desk dies, and is answered by the same `take over / done / skip`
    replies as everything else.

    The surface and the origin ride in `evidence`, because `Handoff` has no
    column for either and this module is not allowed to add one. That is a
    workaround, not a design: the rule engine cannot key on a field buried in
    free text. Named in docs/the-browser.md.
    """
    scrub = redactor_for(vault_path) if vault_path else handoff.redact
    scope = rule_scope(sess, kind, url)

    return handoff.raise_handoff(
        path,
        agent=sess.desk,
        kind=scope["kind"],
        needs=(passkey_needs(sess) if passkey else
               approve_needs(sess, scope["kind"], scope["origin"])),
        state=("Nothing on this page has been submitted and no payment has "
               "been made at this step. The agent has stopped and is not "
               "touching the browser."),
        where=scrub(url),
        evidence=(f"surface={scope['surface']} desk={scope['desk']} "
                  f"origin={scope['origin']} display={sess.display}"),
    )
