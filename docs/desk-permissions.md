# Desk permissions: bypass everything

**Date:** 2026-09-30 · **Module:** `server/deskperms.py` · **Tests:** `tests/test_desk_can_work.py`

## The ruling

After the auto-mode classifier blocked atlas from changing DNS in GoDaddy
(`[Unauthorized Persistence]`), the owner said, verbatim: *"this has to be
fixed! we should allow it anything"*, and then: *"any type of permission it
should has or ask for approval, this is not helping if its says im block"*.

Every desk runs with **all permission checks bypassed**. A desk can do anything
the box user can, including irreversible actions (delete, push, DNS, crontab,
credential reads). No classifier, no `permissions.ask` floor, no `autoMode`.

## Mechanism (measured, claude 2.1.285)

Desks are `--bg` sessions. There, `permissions.defaultMode` in settings alone is
**ignored** (throwaway desk stayed `permissionMode: "default"` and raised
cards). Three things together work, each verified by removing it:

1. argv `--permission-mode bypassPermissions` (`spawn.build_argv`);
2. `skipDangerousModePermissionPrompt: true` in the per-hire `--settings`;
3. `bypassPermissionsModeAccepted: true` in `~/.claude.json`
   (`pretrust.accept_bypass`, called from `spawn._vouch`); without it `--bg`
   refuses ("requires accepting the disclaimer first").

`--dangerously-skip-permissions` stays in `spawn.BANNED_FLAGS`.

In bypass mode a `permissions.ask` rule and a hook `ask` both still stop the
call (measured under `-p`). So `ALWAYS_ASK` is empty, and the handoff hooks
(`HANDOFF_PATTERNS`, the PermissionRequest hook) still raise the owner's card.

## What still reaches the owner

Needing his hands, not a permission: login, 2FA, captcha, payment (the
"Take over the screen" handoff), plus the hook floor in
`autoreview.HANDOFF_PATTERNS`, which raises a one-tap card. "Always allow" on
that card stops that desk (by name) being asked again for that class, in that
folder and below — listed at `GET /v1/permissions`, removable with `DELETE
/v1/permissions/{id}` (docs/client-api.md §12.2). The brief
(`hire.NEVER_BLOCKED`) forbids ending on "I am blocked": go round, or raise a
tappable card.

## Rollout

A resumed (woken) desk keeps its old flags (`docs/wake.md`). Desks must be
FRESH-started after deploy to pick this up.
