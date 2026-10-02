# Contributing to Agent Deck

Thanks for helping. This page covers how to get set up, how we work, and what a
pull request needs.

## Before you start

- Read [README.md](README.md), [docs/quickstart.md](docs/quickstart.md) and
  [docs/security.md](docs/security.md). For anything that touches the API, also
  read [docs/client-api.md](docs/client-api.md), the contract between the server
  and every client.
- Open an issue before you start on a change to product direction, a security
  boundary (auth, network exposure, what agents may do), or an incompatible
  `/v1` change.
- Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md),
  never in a public issue.
- Never test against a live deck you care about, its token, or its server. Use a
  throwaway VPS or container, and a scratch `CLAUDE_CONFIG_DIR`.

## Set up

```sh
make setup          # .venv with Python 3.13 via uv, plus dev requirements
make test-backend   # the Python suite
make test-macos     # the Swift package tests (macOS only)
```

## Test before code

1. Write a test that fails against the current behavior (the detector) and
   commit it first. Put its failing output (the RED witness) in the pull request.
   A detector only counts if it would fail again when the fix alone is reverted.
2. Then fix it, and look for the same failure elsewhere: equivalent routes,
   clients, hooks, and install paths. Fix or list every match.
3. Say which facts you measured and which you assumed.

Tests marked `live` or `ui` are excluded by default. They can start real agents,
spend tokens, or drive a visible app. Run them deliberately (`pytest -m live`)
and state their side effects.

The installer has a container harness. It runs a clean Ubuntu 24.04 with systemd
as PID 1:

```sh
docker build -t deck-fresh-box -f tests/live/fresh_box/Dockerfile.systemd tests/live/fresh_box
docker run -d --name box --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw deck-fresh-box
# copy your checkout in, then:  docker exec box /opt/agent-deck/deploy/install-deck.sh --yes --no-docker --skip-login
```

## Verification

```sh
make check
make test
```

A change to the `/v1` contract needs both suites and an update to
`docs/client-api.md`. A visible macOS change needs a screenshot or an XCUITest
witness. Screenshots must show only made-up names and data.

## Pull requests

- Use the template. Keep each PR focused and under about 300 changed lines.
- Commit subjects look like `test(thread): …`, `feat(api): …`, `fix(install): …`.
- Include the RED witness, where else you looked for the same bug, what you
  measured, how to roll back, and anything you did not test. A green suite does
  not excuse hiding a gap in live testing.
- Be honest about runtimes. Claude Code is fully supported. OpenCode and Codex
  are partial. Do not claim support that the code does not have.
- Name other companies' products only to describe compatibility. Never imply an
  affiliation or endorsement.

## Licensing your contribution

Agent Deck is source-available under the PolyForm Noncommercial License 1.0.0
([LICENSE](LICENSE)). By contributing, you:

1. Certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/):
   you wrote the change, or otherwise have the right to submit it. Sign off every
   commit with `git commit -s`, which adds a `Signed-off-by:` line.
2. License your contribution under the same terms as the project (inbound = outbound).
3. Also grant the licensor named in [LICENSE](LICENSE) a perpetual, worldwide,
   royalty-free, irrevocable right to use, change and relicense your
   contribution under other terms, including commercial licenses.
