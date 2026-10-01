# Notifications: when a desk needs you, or says something to you

## What notifies you

One rule, decided on the deck (`server/owner_alerts.py`) and served at
`GET /v1/owner/alerts?since=<cursor>`. The iPhone app, the Mac app and ntfy all
read the same answer, so they cannot disagree.

| Notifies | Does not |
|---|---|
| A decision card a desk put to you (`ask`) | A desk's progress line (`say` without `urgent`) |
| A permission approval waiting on you | Desk-to-desk messages |
| A handoff only you can do (2FA, sign-in, payment) | A report's turn-final prose to its boss |
| The answer of a desk that reports to you | The deck's own notices (restarts) |
| The answer of any desk you wrote to | Anything you or a schedule wrote |
| A line a desk marked urgent | |

Each alert is sent once. A fresh install, or turning ntfy on, starts at "now"
and does not replay history. The thread you are reading does not banner.

WhatsApp stays urgent-only (owner ruling 2026-09-30). The channels on this page
are the everyday ones.

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

Tapping an ntfy notification opens the Agent Deck app on that desk's thread
(`agentdeck://open?thread=...`).

To turn ntfy off, remove `DECK_NTFY_URL` from the env file and restart
`agentdeck`. The deck's cursor is kept in `~/.claude/agent-bus/ntfy.json`.

## Real push (APNs), prepared but off

This needs the $99 Apple Developer account. Nothing is enrolled or bought. With
the account:

1. In `ios/project.yml`, target settings, add
   `CODE_SIGN_ENTITLEMENTS: AgentDeckPhone/APNs.entitlements`, and set
   `DeckAPNsEnabled: true` in the Info properties.
2. Rebuild. `PushRegistration` registers with APNs and stores the device token
   under `deck.apns.token`.
3. Still to build: a deck-side APNs sender, plus a route that collects the
   token. The sender reads the same `owner_alerts` feed as ntfy does.
