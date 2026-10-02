# Security Policy

## Reporting

Do not open a public issue for a vulnerability. Use GitHub's private
**Security Advisories** page for this repository and include affected
commits, reproduction steps, impact, and any known mitigation. Remove tokens,
transcripts, personal data, and live agent output from attachments.

The maintainer aims to acknowledge a report within two business days, provide
an initial severity assessment within five, and coordinate disclosure after a
fix or mitigation exists. These are response targets, not a warranty.

## Sensitive Surfaces

Treat the `/v1` API, agent transcripts, browser profiles, computer input,
terminal/file routes, permission rules, Keychain entries, OAuth material, and
`~/.claude/agent-bus/` as sensitive. Production must bind to the WireGuard
interface, require `AGENT_DECK_TOKEN`, and avoid public ingress.

Never commit `.env` files, bearer tokens, OAuth values, transcripts, database
state, browser profiles, or real approval records. A credential pasted into a
chat or log is compromised: revoke it, replace it, and audit recent use.

## Supported Version

Security fixes target the canonical `main` branch and the currently deployed
VPS revision. Historical feature branches, archived repositories, and locally
modified app bundles are unsupported.
