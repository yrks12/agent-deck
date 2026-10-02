# Changelog

All notable production releases are recorded here. Dates use ISO 8601.

## 0.9.2 - 2026-10-02

### Shaliach

- Agent Deck is now **Shaliach** (Hebrew: emissary, one sent to act on your behalf). Your AI emissaries: a whole team that works for you.
- The repository moved to github.com/yrks12/shaliach. The old address redirects, so existing clones and one-liners keep working.
- The Mac app is `Shaliach.app`; the Mac installer replaces an existing `Agent Deck.app` with it. The iPhone app shows as Shaliach.
- Nothing to migrate: the apps keep their bundle ids, so settings, keychain items and pairings carry over. Server paths, the service name and `DECK_*` settings are unchanged.

## 0.9.1 - 2026-10-02

### Team

- Every agent can carry a short description of what it does.
- Agents remember their full history back to day one: the history and a daily chronicle.
- New tools reach running agents without a restart.

### Talk to them

- Agents send you images, video, PDFs and audio right in the chat.
- Agents can call you, using your settings, quiet hours and a daily cap.
- Links in messages are clickable.
- Older messages load as you scroll back.
- Messages no longer vanish.

### Your Mac

- Live view and control of your Mac from the iPhone.
- Agent control of your Mac needs a per-session grant, shows a banner, has a Stop hotkey and a Full-access gate.

### Computers

- A clean Screen / Terminal / Both view.
- Watch mode.
- Browsers heal themselves when they stall or crash.
- Smarter browser slots.

### Notifications

- "Needs you" alerts always reach you, through ntfy on the iPhone.

### Fixes

- The Mac app no longer crashes when you press play on a video in chat.

## Unreleased

### Added

- Unified the FastAPI server and native macOS client in one repository.
- Added repository status, contributor guidance, CI, ownership, security,
  issue/PR templates, dependency automation, and repeatable commands.

### Security

- Documented private vulnerability reporting and credential-handling rules.

## Historical Work

Development before the monorepo consolidation is preserved in Git history.
It was not released under a consistent versioning scheme; consult `STATUS.md`
for what is implemented and what remains unproven.
