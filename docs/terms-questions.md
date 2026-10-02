# Open terms-of-service questions

These are questions, not conclusions. Nobody on the project has a written answer
to any of them yet. Until someone does, treat each one as unresolved. Each
question comes with what the code does today, so a reviewer can check it.

If you know the answer to one, with a source, please open an issue.

## Anthropic / Claude Code

1. Do the consumer (Pro/Max) plan terms allow Claude Code to run always-on and unattended on a server, driven by a third-party orchestrator, signed in with the Claude CLI's own login?
2. If they do, does anything change when several such sessions run at the same time on one subscription, as Shaliach does when it starts several agents?
3. Is reading the subscription's OAuth token to call `api.anthropic.com/api/oauth/usage` (an endpoint we have not found documented) within the terms, and should a user expect it to change or disappear without notice?
4. Is it acceptable for an independent source-available project to describe itself as working "with Claude Code" and to show the name "Claude" in its interface, and what wording does Anthropic ask third parties to use?
5. Should the documentation recommend an Anthropic API key (Commercial Terms) instead of a subscription for server installs, or for any use with more than one person?

What the code does today:

- Server installs sign in with Claude's own `claude auth login`, run by
  `sudo deckctl login` for the service user. Shaliach stores no Claude token;
  the CLI keeps and refreshes its own login.
- The usage meter (on by default, labelled unofficial) reads the CLI's login and
  calls the usage endpoint (`server/sources/usage.py`). Turning it off with
  `deckctl config set claude.usage_meter false` does not affect anything else.
- Agents are started with the `claude` command itself. The project does not
  reimplement or modify Claude Code.

## OpenAI (voice calls, optional)

6. With a user-supplied OpenAI API key, are there any terms beyond OpenAI's standard API terms for relaying live microphone audio from the macOS app through the user's own server to the Realtime API?

## Apple (the macOS and iPhone apps)

7. Do Apple's terms for a free personal team allow a project to ship a script that builds the app from source, signs it with the user's own Apple ID, and installs it on the user's own iPhone?
8. Is it consistent with Apple's terms for the documentation to mention third-party re-signing tools (SideStore, AltStore) as an option, and should it say more about them?
9. Is an ad-hoc-signed, non-notarized macOS app that a user installs with a script that clears its quarantine flag acceptable to distribute this way, or should releases wait for notarization?

What the code does today:

- `scripts/install-iphone.sh` runs Xcode's own build with the user's team and
  installs with `devicectl`. Nothing is uploaded anywhere.
- `scripts/install-mac.sh` checks the release's published `.sha256` before
  installing, then runs `xattr -dr com.apple.quarantine` on the installed app.

## Naming

10. Is the product name "Shaliach" clear of conflicting trademarks in the software category where we plan to publish it?
11. Should the README and store listings avoid naming other AI companies' products entirely, apart from the factual compatibility table?

## How these get closed

A question is closed when there is a quote from the current published terms, or a
written answer from the provider, linked here with its date. Until then, the
public documentation says only what is true of the code. It does not say that
any particular use is allowed.
