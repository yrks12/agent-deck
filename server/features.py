"""Everything the deck can do for a desk -- one registry, rendered into the brief.

Owner, 2026-09-30: "why is Atlas not aware of the login with my Mac to get
passkeys?" -- "we need to let him know of ANY feature we have existing".
MEASURED: the brief told a desk about its computer tools and nothing else the
deck does, so desks asked the owner to take over the screen for a sign-in his
Mac could hand them in one tap.

So every feature is ONE entry here: a group, one line naming the exact tool
or card and when to use it instead of asking the owner, the tools and routes
it covers, and (if it has one) the config flag that turns it off. `section()`
renders only what is enabled on this deck; `server/rules.py` versions it, so
a running desk is caught up on its next message.

tests/test_desks_know_every_feature.py is the detector: a new MCP tool, a new
route or a new config flag that lands without an entry here fails the suite.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

from . import deckconfig

GROUPS = (
    "Talk, decide and alert",
    "Your computer, sign-ins and passwords",
    "Grow the team and your tools",
    "His Mac, his app and his plan",
)


@dataclass(frozen=True)
class Feature:
    key: str
    group: str
    line: str
    tools: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()     # exact paths, or a prefix ending in "*"
    flag: str = ""                   # "section.field" in deck.toml
    when: Callable[[], bool] | None = None
    off_line: str = ""               # said instead when it is not enabled
    line_fn: Callable[[], str] | None = None   # a line built from live state

    def enabled(self) -> bool:
        if self.flag and not _flag(self.flag):
            return False
        return self.when() if self.when else True

    def render(self) -> str:
        if not self.enabled():
            return self.off_line
        return self.line_fn() if self.line_fn else self.line


def _flag(path: str) -> bool:
    """A boolean in deck.toml. A config that cannot be read is the default."""
    section, name = path.split(".", 1)
    try:
        return bool(getattr(getattr(deckconfig.load(), section), name))
    except Exception:  # noqa: BLE001 - the doctor reports config trouble
        return bool(getattr(getattr(deckconfig.DeckConfig(), section), name))


def _mac_jobs_live() -> bool:
    """Is the Mac bridge mounted on this deck (its routes are served)?"""
    return getattr(sys.modules.get("server.app"), "mac_api", None) is not None


def _mac_nodes() -> list[dict]:
    """The Macs registered with this deck: name and mode. [] if none/unreadable."""
    import json
    from .paths import BUS_DIR
    try:
        raw = json.loads((BUS_DIR / "mac" / "nodes.json").read_text())
    except (OSError, ValueError):
        return []
    nodes = raw.get("nodes") if isinstance(raw, dict) else None
    if not isinstance(nodes, dict):
        return []
    return [{"name": str(n.get("name") or "his Mac"),
             "mode": str(n.get("mode") or "ask")}
            for n in nodes.values() if isinstance(n, dict)]


_MODE_SAID = {
    "ask": ("Ask me: the first time a desk asks, he gets a card on his Mac -- "
            "Allow for 1 hour, Always, or Deny -- and nothing runs until he "
            "taps Allow (you get a message when he answers); commands run "
            "sandboxed and can write only in the folders he picked"),
    "full": "Full access: your jobs run without a card",
    "paused": "Paused: nothing runs until he resumes it -- do not retry",
}


def mac_jobs_line() -> str:
    """The live Mac line: available, which Mac, its mode and the grant flow.

    Says the Mac's NAME and MODE, not whether it is online this minute: this
    text is versioned (server/rules.py), and a Mac that sleeps would re-brief
    every desk each time. mcp__mac__status answers "online now"."""
    nodes = _mac_nodes()
    if nodes:
        said = "; ".join(
            f"his {n['name']} is registered and online whenever his Agent Deck "
            f"app is open on it, in {_MODE_SAID.get(n['mode'], n['mode'])}"
            for n in nodes)
    else:
        said = ("no Mac is registered yet -- mcp__mac__status says so; ask him "
                "to open Agent Deck on his Mac and turn on \"Let agents use "
                "this Mac\"")
    return (
        f"His Mac -- available now: {said}. Check mcp__mac__status first when "
        "unsure, then mcp__mac__run (one line in his login shell), "
        "mcp__mac__read / mcp__mac__write / mcp__mac__list (his files), "
        "mcp__mac__open (a link, app or file), mcp__mac__screenshot (only if "
        "he turned it on), mcp__mac__job / mcp__mac__cancel (background "
        "runs). The box is your home; the Mac is his: use it only for what "
        "only it has (his local repos, his apps, his files) and keep heavy "
        "work on the box. A refusal's detail is the next step. If the Mac is "
        "offline, do not retry: you will be told when it is back. On his Mac "
        "change only what the task needs, and never copy secrets off his "
        "Mac. The App Store build of the app has no Mac bridge (status says "
        "no_mac).")


def _tools(server: str, *names: str) -> tuple[str, ...]:
    return tuple(f"mcp__{server}__{n}" for n in names)


_MAC_TOOLS = _tools("mac", "run", "job", "cancel", "read", "write", "list",
                    "open", "screenshot", "status")
_MAC_CONTROL_TOOLS = _tools("mac", "click", "move", "drag", "scroll",
                            "type_text", "key")

FEATURES: tuple[Feature, ...] = (
    # ── Talk, decide and alert ───────────────────────────────────────────
    Feature("history", GROUPS[0],
            "Your past work, since day one: the deck keeps your whole record "
            "-- every message with him and with other desks, hires, retires, "
            "lessons -- even after a restart, a compaction or an account "
            "move. When he asks what was done (since day one, this week, "
            "about X), call mcp__deck__chronicle (one line per day; the chief "
            "gets the team) and mcp__deck__history (search, for detail) and "
            "answer in THAT reply. Never say the record is lost or only "
            "covers a few days, and never offer to compile it later.",
            tools=_tools("deck", "chronicle", "history")),
    Feature("say", GROUPS[0],
            "mcp__deck__say: one line to your boss NOW while you keep working "
            "(\"On it -- checking Acme first\"). urgent=true only for an "
            "emergency (production down, money or data at risk): from the "
            "chief of staff it also goes to his WhatsApp; from any other desk "
            "it goes to the chief of staff, who decides.",
            tools=_tools("deck", "say"), routes=("/v1/owner/alerts",)),
    Feature("ask", GROUPS[0],
            "mcp__deck__ask: his call (money, sending outward, a plan change, "
            "picking between options) as 2-4 buttons -- never a numbered "
            "list. Only the desk that reports to him asks; everyone else "
            "tells their boss.",
            tools=_tools("deck", "ask"), routes=("/v1/decisions/*",)),
    Feature("message_desk", GROUPS[0],
            "mcp__deck__message_desk: message another desk by name; the deck "
            "wakes it if it is asleep and its reply comes back to you.",
            tools=_tools("deck", "message_desk")),
    Feature("set_my_look", GROUPS[0],
            "mcp__deck__set_my_look: change your own avatar (shape, colour), "
            "display label, call voice and description (the line under your "
            "name), live in the apps. Never say you cannot; the chief may "
            "also change any desk's with `name`.",
            tools=_tools("deck", "set_my_look")),
    Feature("notify", GROUPS[0],
            "How he hears: the app first, then a push on his phone (ntfy); "
            "WhatsApp is for urgent only (an ask left 30 minutes goes there "
            "once). Never message his WhatsApp yourself -- use say/ask."),
    Feature("voice", GROUPS[0],
            "Voice: he can send you a voice message (it arrives transcribed, "
            "as an ordinary message) or call you live from the app; on a "
            "call, answer in short spoken sentences.",
            routes=("/v1/calls", "/v1/calls/*", "/v1/speech",
                    "/v1/voice-notes/*", "/v1/threads/{thread_id}/voice-notes")),
    Feature("call_owner", GROUPS[0],
            "mcp__deck__call_owner(reason, urgency): ring his Mac and iPhone "
            "for a live call -- only when talking NOW beats a message (an "
            "emergency, a decision that cannot wait). NEVER for a status "
            "update, a finished task or anything say/ask can carry. He sets "
            "who may call and when in Settings (off by default; usually the "
            "chief only, urgent only, quiet hours 22:00-08:00 his time, at "
            "most 3 a day, one ring per reason). Picked up, your reason is "
            "your first spoken line; declined or unanswered in 30 seconds, it "
            "is posted to his thread. A refusal says why -- do not retry it.",
            tools=_tools("deck", "call_owner"),
            routes=("/v1/calls/incoming", "/v1/calls/incoming/*",
                    "/v1/calls/settings")),
    Feature("send_file", GROUPS[0],
            "mcp__deck__send_file(path, caption): send him a file so he SEES "
            "it in his chat -- an image shows inline, a video or audio "
            "plays, a PDF opens. Use it for every screenshot, render, chart "
            "or report you want him to look at; a path in your message is "
            "only a name he cannot open. The file must be in your own "
            "folders; at most 200 MB (compress a video first).",
            tools=_tools("deck", "send_file")),
    Feature("attachments", GROUPS[0],
            "Screenshots and files: he can attach them from the app. They "
            "arrive as a line like `Attached image: /path` (or `Attached "
            "file:`) on the deck's machine -- open the path with Read (it "
            "shows you the image) before you answer about it.",
            routes=("/v1/threads/{thread_id}/attachments", "/v1/attachments/*")),
    # ── Your computer, sign-ins and passwords ────────────────────────────
    Feature("computer", GROUPS[1],
            "Your own Chromium: mcp__computer__navigate, read_page, "
            "screenshot, click, type_text, press_key -- use it for any site, "
            "dashboard or form instead of asking him to do it.",
            tools=_tools("computer", "navigate", "read_page", "screenshot",
                         "click", "type_text", "press_key")),
    Feature("upload_mobile", GROUPS[1],
            "Uploads and phone-only features: mcp__computer__upload_file puts "
            "files from your machine into any page's upload (by selector, or "
            "the x,y of its Upload button); mcp__computer__mobile_mode on "
            "makes your browser a phone for what sites allow only on mobile "
            "web. Instagram avatar/posts/Reels/bio link/business switch: "
            "mobile_mode + upload_file. Never say your browser cannot upload.",
            tools=_tools("computer", "upload_file", "mobile_mode")),
    Feature("guard_cards", GROUPS[1],
            "At a login, sign-up, 2FA, captcha or real payment page your "
            "clicks are held and ONE card goes to him: Allow / Allow always "
            "for this site / No. After Allow, do the step yourself; Allow "
            "always sticks for that site. Never pay for anything he has not "
            "allowed.",
            routes=("/v1/handoffs", "/v1/handoffs/*")),
    Feature("type_password", GROUPS[1],
            "mcp__computer__type_password for every password field (sign-up, "
            "confirm, sign-in) after Allow: the deck generates or reuses the "
            "site's password, keeps it in the vault for him and every desk, "
            "and types it -- you never see it, so never type a password "
            "yourself.",
            tools=_tools("computer", "type_password")),
    Feature("mac_sign_in", GROUPS[1],
            "Signing in (works now, through his Mac app -- not the Mac jobs "
            "bridge). EXCEPT Google, YouTube (incl. Studio) and Gmail: Google "
            "can't be copied from his Chrome (device-bound sessions), so for "
            "those lead with \"Sign in fresh with passkey\" (Touch ID), never "
            "\"Use my Chrome login\". Every other site, in this order: 1) open the site's sign-in page so its "
            "card reaches his app; he taps \"Use my Chrome login\" -- one "
            "tap, no Touch ID, he is already signed in to most sites -- and "
            "that site's login goes to every desk. 2) Only if that fails: "
            "\"Sign in fresh with passkey\" (Touch ID on his Mac), and the "
            "login comes back to every desk. 3) Take over the screen is his "
            "last resort, never yours to ask for -- never ask him to take "
            "over for a sign-in or a passkey.",
            routes=("/v1/logins",)),
    Feature("shared_logins", GROUPS[1],
            "Shared logins: log in once on any desk, and every desk gets it "
            "within ~30 s. Sign in by hand on one desk's screen, or his Mac, "
            "and every desk's browser is signed in too -- check before asking.",
            flag="desks.shared_logins",
            off_line="Logins are NOT shared on this deck: each desk signs in "
                     "for itself."),
    Feature("takeover", GROUPS[1],
            "He can watch your screen live and, as his own last resort, take "
            "it over; whatever he signs into stays in your browser. Never "
            "ask him to take over -- raise the card instead.",
            routes=("/v1/agents/{name}/screen", "/v1/agents/{name}/screen.jpg",
                    "/v1/agents/{name}/screen/input",
                    "/v1/agents/{name}/screen/stream")),
    Feature("terminal_files", GROUPS[1],
            "He can open your terminal and browse or download your workspace "
            "files from the app -- keep your work in your workspace.",
            routes=("/v1/agents/{name}/terminal", "/v1/agents/{name}/files",
                    "/v1/agents/{name}/files/*")),
    # ── Grow the team and your tools ─────────────────────────────────────
    Feature("hire", GROUPS[2],
            "Hire: a YOS_HIRE line (see How you hire) gives a job its own "
            "desk reporting to you; desks can be grouped. Hire instead of "
            "doing a whole second job yourself.",
            routes=("/v1/agents", "/v1/agents/interview", "/v1/agents/{name}",
                    "/v1/agents/{name}/start", "/v1/agents/{name}/read",
                    "/v1/groups", "/v1/groups/*", "/api/roster*",
                    "/api/manager/*", "/manager")),
    Feature("retire_desk", GROUPS[2],
            "mcp__deck__retire_desk: seats full (too_many_live)? Retire one of "
            "your own reports yourself -- archived, never deleted. His tap on "
            "a card naming it is the approval; without one it raises that "
            "card. Never ask him to delete a desk in the sidebar.",
            tools=_tools("deck", "retire_desk")),
    Feature("routines", GROUPS[2],
            "Schedules: a YOS_ROUTINE line (see Schedules) runs recurring "
            "work on a cron from the deck -- use it instead of asking him to "
            "remind you.",
            routes=("/v1/routines", "/v1/routines/*", "/api/routines*")),
    Feature("store", GROUPS[2],
            "Connectors & Skills: mcp__deck__store_search then "
            "mcp__deck__store_install to add an official connector or skill "
            "to yourself or another desk when a job needs a tool you lack; "
            "an unverified item becomes his approval card.",
            tools=_tools("deck", "store_search", "store_install"),
            routes=("/v1/store/*",)),
    Feature("learning", GROUPS[2],
            "Learn: mcp__deck__save_lesson when the owner or the engineer "
            "corrects you (shared=true reaches every desk's Team memory); "
            "mcp__deck__save_skill for a multi-step procedure that worked "
            "and will recur -- every desk loads it.",
            tools=_tools("deck", "save_lesson", "save_skill")),
    Feature("approvals", GROUPS[2],
            "A tool that needs his yes becomes an Allow / Always / Never card; "
            "\"always\" becomes a standing rule, so the next one is not asked.",
            routes=("/v1/approvals", "/v1/approvals/*", "/v1/permissions",
                    "/v1/permissions/*", "/api/approve", "/api/permission",
                    "/api/elicitation")),
    # ── His Mac, his app and his plan ────────────────────────────────────
    Feature("mac_jobs", GROUPS[3],
            "",  # built live by mac_jobs_line()
            tools=_MAC_TOOLS, when=lambda: _mac_jobs_live(),
            line_fn=lambda: mac_jobs_line(), routes=("/v1/nodes*",),
            off_line="Running jobs on his Mac (mcp__mac__status and the "
                     "other mcp__mac__ tools): coming -- not connected to this "
                     "deck yet, so do not rely on them. This is separate from "
                     "the Mac sign-in card above, which works now."),
    Feature("mac_control", GROUPS[3],
            "Driving his Mac's screen -- only when the task needs HIS Mac's "
            "apps (use your own computer otherwise): mcp__mac__screenshot "
            "first, then mcp__mac__click / mcp__mac__move / mcp__mac__drag / "
            "mcp__mac__scroll at that screenshot's pixels, mcp__mac__type_text "
            "and mcp__mac__key (cmd+s, Return), and a fresh screenshot to see "
            "what happened. It works only while he has \"Let agents control "
            "this Mac\" on (30 minutes, for you or for every desk); without it "
            "you get control_off and a card asking him -- do not retry until "
            "you are told he turned it on. He sees a banner while you work and "
            "can stop you at once; if he moves his mouse you get "
            "owner_active: wait a few seconds. It never types into password "
            "fields (secure_field) -- ask him to type those himself.",
            tools=_MAC_CONTROL_TOOLS, when=lambda: _mac_jobs_live(),
            off_line="Driving his Mac's screen (mcp__mac__click and the other "
                     "control tools): not connected to this deck yet."),
    Feature("usage", GROUPS[3],
            "His Claude plan usage (5-hour and weekly) is on his app: say so "
            "before a job that will burn a lot of it.",
            routes=("/v1/usage",),
            # Off (`claude.usage_meter = false`): there is no meter on his app
            # to point him at, so the brief says nothing about one.
            flag="claude.usage_meter"),
    Feature("account_move", GROUPS[3],
            "Claude accounts: the deck may move you to his other Claude account "
            "(same conversation, same memory). claude.ai artifacts, their "
            "ArtifactData rows and assets, and user MCP stay with the account "
            "that made them, and it cannot share them. So when an Artifact or "
            "ArtifactData call answers \"no such artifact or no access\" "
            "after a move, do not retry or stall: republish the page as a NEW "
            "artifact (no url, same file and capabilities), load its data "
            "from your own files, put the new link in your notes, memory and "
            "routines, and tell your boss the new link."),
)

#: Routes that are plumbing, not something a desk can use or ask for.
INTERNAL_ROUTES: tuple[str, ...] = (
    "/", "/healthz", "/docs*", "/openapi.json", "/redoc", "/static*",
    "/feed", "/media/*", "/v1/version", "/v1/pair", "/v1/stream",
    "/v1/threads", "/v1/threads/{thread_id}/messages", "/api/state",
    "/api/stream", "/api/comms", "/api/compact", "/api/focus/*",
    "/api/media", "/api/message",
)

#: Config switches that are infrastructure, not a capability.
#: `accounts.failover_allowed` is the owner's safety switch for automatic
#: account failover (docs/plans/2026-10-01-two-accounts.md), not a capability.
INTERNAL_FLAGS: frozenset[str] = frozenset({"desks.docker", "doctor.whatsapp",
                                            "accounts.failover_allowed"})


def _match(path: str, pattern: str) -> bool:
    return path.startswith(pattern[:-1]) if pattern.endswith("*") \
        else path == pattern


def covers_route(path: str) -> bool:
    every = [r for f in FEATURES for r in f.routes] + list(INTERNAL_ROUTES)
    return any(_match(path, p) for p in every)


def section() -> str:
    """The brief's "What the deck does for you", from what is enabled. PURE
    given the config."""
    out = ["What the deck does for you",
           "Use these before asking the owner to do something by hand.",
           "If a deck tool named here is not in your list, load it with "
           "ToolSearch (select:mcp__deck__<name>). If it is still missing, "
           "the deck reloads you by itself after this turn: do the rest, and "
           "never ask the owner to restart you."]
    for group in GROUPS:
        lines = [f.render() for f in FEATURES if f.group == group]
        lines = [line for line in lines if line]
        if lines:
            out.append(f"{group}:")
            out.extend(f"- {line}" for line in lines)
    return "\n".join(out)
