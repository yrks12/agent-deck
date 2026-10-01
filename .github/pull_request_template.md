## Outcome

Describe the user-visible or operational result and link the issue/spec.

## Detector

- Test added before implementation:
- RED witness (failure against old behavior):
- Green command and result:
- Would it fail if only the implementation were reverted?

## Class Sweep

List every route, state transition, client, renderer, or deployment surface
sharing this failure shape, including searches that found no other matches.

## Premise

State what is measured and what remains assumed. Attach commands, logs, or
screenshots for measurements.

## Risk and Rollback

Describe security, data, API, macOS, and VPS impact plus the rollback command.

## Untested

Name live, UI, hardware, credential, or production behavior not tested.

## Checklist

- [ ] Focused detector passes
- [ ] Backend suite passes
- [ ] macOS suite passes, or change cannot affect it
- [ ] Shared API contract updated when applicable
- [ ] No credentials, transcripts, or real agent state included
