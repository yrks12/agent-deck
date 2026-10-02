# Notifications: when a desk needs you, or says something to you

## What notifies you

Two layers, both on the deck. Every channel delivers the same pushes.

**What is about you** (`server/owner_alerts.py`):

- A card that needs you: a decision, a pending approval, or a handoff.
- The first answer from a desk after you wrote to it.
- A line a desk marked urgent.

These never notify: a desk's plain replies, progress lines, desk-to-desk
messages, the deck's notices, and test desks. A test desk is one with the
roster `test` flag, or "test" or "probe" in its name or label.

**When it buzzes** (`server/push_policy.py`):

1. **Settle.** A card waits 30 seconds first. Approvals that are cleared in
   seconds never buzz.
2. **Presence.** Nothing is pushed while you are looking. That means a read,
   a message from you, or an app in front (the phone in the foreground, or
   the Mac app frontmost and in use) within the last 2 minutes. When you
   leave, anything still waiting goes out once.
3. **Coalescing.** Anything that arrives within 2 minutes of a push goes out
   together as one summary, for example "3 agents need you".
4. **Cap.** At most 6 pushes an hour.
5. **Quiet hours.** Off by default. Set `[notify] quiet_hours = "22:00-07:00"`
   in deck.toml (box-local time) or `DECK_QUIET_HOURS`.

Each card is pushed once, and the state survives a restart
(`~/.claude/agent-bus/pushes.json`). The hour that sent 81 pushes replays to
6 under this policy (`tests/test_push_replay.py`).

When ntfy is configured, the iPhone app does not post its own copy.

## Channels

**iPhone app (local notifications).** While the app is open, or running in the
background during a call, it checks every 12 seconds. When iOS suspends it,
iOS wakes it for a background refresh when it chooses, usually every 15
minutes to a few hours. That is not real time. Allow notifications the first
time the app asks. Tapping a notification opens that desk's thread.

**Mac app.** The same alerts, as Mac notifications, while the app is running.
Clicking one opens the thread.

**ntfy (real time, free).** The app is signed with a free Apple team, and a
free team cannot receive push (APNs). The free ntfy iOS app can. The deck posts
each alert to a private topic on a self-hosted ntfy server on the box. That
server sends ntfy.sh a *poll request* carrying only a message id and a hashed
topic, never the text. ntfy.sh wakes the ntfy app through APNs, and the app
then fetches the message from the box over WireGuard. The text of what a desk
said only goes from your box to your phone.

## One-time ntfy setup

On the box (already done 2026-09-30):

```
# ntfy server, WireGuard address only
docker run -d --name ntfy --restart unless-stopped \
  -p 10.99.0.1:8090:80 -v /etc/ntfy:/etc/ntfy -v /var/cache/ntfy:/var/cache/ntfy \
  binwiederhier/ntfy serve
# /etc/ntfy/server.yml
base-url: "http://10.99.0.1:8090"
upstream-base-url: "https://ntfy.sh"
cache-file: "/var/cache/ntfy/cache.db"
# /etc/agent-deck/agentdeck.env  (the topic name is in /etc/ntfy/deck-topic)
DECK_NTFY_URL=http://10.99.0.1:8090/<topic>
```

On the iPhone (you do this once):

1. Install **ntfy** from the App Store (free, by Philipp Heckel).
2. In ntfy, open Settings, then Default server, and enter `http://10.99.0.1:8090`.
3. Tap **+**, enter the topic name, and subscribe. Allow notifications.
4. Keep WireGuard on, or set it to on-demand. The ntfy app fetches the message
   from the box, so the box has to be reachable when the notification arrives.

Tapping an ntfy notification opens the Shaliach app on that desk's thread
(`agentdeck://open?thread=...`).

To turn ntfy off, remove `DECK_NTFY_URL` from the env file and restart
`agentdeck`. The deck's cursor is kept in `~/.claude/agent-bus/ntfy.json`.

## Real push (APNs), prepared but off

Optional, and off by default: Apple issues push credentials only to paid
developer accounts, so the supported push channel is ntfy (above), which needs
no Apple account. If you do have an account:

1. In `ios/project.yml`, target settings, add
   `CODE_SIGN_ENTITLEMENTS: AgentDeckPhone/APNs.entitlements`, and set
   `DeckAPNsEnabled: true` in the Info properties.
2. Rebuild. `PushRegistration` registers with APNs and stores the device token
   under `deck.apns.token`.
3. Still to build: a deck-side APNs sender, plus a route that collects the
   token. The sender reads the same `owner_alerts` feed as ntfy does.
