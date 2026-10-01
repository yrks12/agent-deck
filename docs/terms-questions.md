# Open terms-of-service questions

These are questions, not conclusions. Nobody on the project has a written answer
to any of them yet. Until someone does, treat each one as unresolved. Each
question comes with what the code does today, so a reviewer can check it.

If you know the answer to one, with a source, please open an issue.

## Anthropic / Claude Code

1. Do the consumer (Pro/Max) plan terms allow Claude Code to run always-on and unattended on a server, driven by a third-party orchestrator, using a long-lived token from `claude setup-token`?
2. If they do, does anything change when several such sessions run at the same time on one subscription, as Agent Deck does when it starts several agents?
3. Is reading the subscription's OAuth token to call `api.anthropic.com/api/oauth/usage` (an endpoint we have not found documented) within the terms, and should a user expect it to change or disappear without notice?
4. Is it acceptable for an independent open-source project to describe itself as working "with Claude Code" and to show the name "Claude" in its interface, and what wording does Anthropic ask third parties to use?
5. Should the documentation recommend an Anthropic API key (Commercial Terms) instead of a subscription for server installs, or for any use with more than one person?

What the code does today:

- Server installs sign in with `claude setup-token` (`bin/deckctl login`) and
  store the token for the service user (`claude.oauth_env` in `deck.toml`).
- The usage meter reads that token and calls the usage endpoint
  (`server/sources/usage.py`). Turning the meter off does not affect anything
  else.
- Agents are started with the `claude` command itself. The project does not
  reimplement or modify Claude Code.

## OpenAI (voice calls, optional)

6. With a user-supplied OpenAI API key, are there any terms beyond OpenAI's standard API terms for relaying live microphone audio from the macOS app through the user's own server to the Realtime API?

## Naming

7. Is the product name "Agent Deck" clear of conflicting trademarks in the software category where we plan to publish it?
8. Should the README and store listings avoid naming other AI companies' products entirely, apart from the factual compatibility table?

## How these get closed

A question is closed when there is a quote from the current published terms, or a
written answer from the provider, linked here with its date. Until then, the
public documentation says only what is true of the code. It does not say that
any particular use is allowed.
