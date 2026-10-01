#!/usr/bin/env bash
# L0 -- systemd container: fresh install, idempotency, owner-box migration (~5 min, Mac + Docker).
# Plan: docs/plans/2026-09-30-easy-setup.md section 7. Written before the installer exists:
# it is EXPECTED TO FAIL until deploy/install.sh and dist/ (release manifest) exist.
set -euo pipefail
cd "$(dirname "$0")/../../.."
REPO=$PWD
IMG=deck-fresh-box
C1=deck-l0-fresh
C2=deck-l0-owner
PORT=${L0_DIST_PORT:-8000}
DIST=${L0_DIST_DIR:-$REPO/dist}
fail() { echo "L0 FAIL: $*" >&2; exit 1; }
cleanup() { docker rm -f "$C1" "$C2" >/dev/null 2>&1 || true; [ -z "${HTTP_PID:-}" ] || kill "$HTTP_PID" 2>/dev/null || true; }
trap cleanup EXIT

[ -f "$REPO/deploy/install.sh" ] || fail "deploy/install.sh does not exist yet (slice D2)"
[ -d "$DIST" ] || fail "no release dist/ at $DIST (slice R0: scripts/release.sh)"
command -v docker >/dev/null || fail "docker not found"

docker build -q -t "$IMG" -f tests/live/fresh_box/Dockerfile.systemd tests/live/fresh_box
(cd "$DIST" && python3 -m http.server "$PORT" >/dev/null 2>&1) & HTTP_PID=$!

start() { # name
  docker run -d --name "$1" --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    --add-host host.docker.internal:host-gateway "$IMG" >/dev/null
  for _ in $(seq 30); do
    docker exec "$1" systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && return
    sleep 1
  done
  fail "systemd never came up in $1"
}
install_cmd="DECK_RELEASE_BASE=http://host.docker.internal:$PORT bash /install.sh --yes \
  --tls=self-signed --hostname localhost --no-docker --skip-login"

# ---- run 1: fresh install
start "$C1"
docker cp deploy/install.sh "$C1":/install.sh
docker exec "$C1" bash -c "$install_cmd" || fail "run 1 of install.sh failed"
docker exec "$C1" deckctl doctor --json > /tmp/l0-doctor.json || true
bad=$(jq -r '[.checks[] | select(.ok != true and .name != "claude_login")] | length' /tmp/l0-doctor.json)
[ "$bad" = "0" ] || fail "doctor reports failing checks other than claude_login: $(cat /tmp/l0-doctor.json)"
jq -e '.checks[] | select(.name=="claude_login") | .message | length > 0' /tmp/l0-doctor.json >/dev/null \
  || fail "claude_login must say a plain sentence about logging in"
docker exec "$C1" curl -sk https://localhost/healthz | jq -e '.ok == true' >/dev/null || fail "/healthz not ok"
code=$(docker exec "$C1" curl -sk -o /dev/null -w '%{http_code}' https://localhost/api/state)
[ "$code" = "404" ] || fail "/api/state must be 404 through the edge, got $code"
docker exec "$C1" ss -ltnH | awk '{print $4}' | grep ':7789$' | grep -qv '^127\.0\.0\.1:' \
  && fail "7789 is listening on a non-loopback address"
docker exec "$C1" ss -ltnH | awk '{print $4}' | grep -q '^127\.0\.0\.1:7789$' || fail "7789 not on 127.0.0.1"

# ---- run 2: idempotent
docker exec "$C1" touch /tmp/marker
sha1=$(docker exec "$C1" bash -c 'sha256sum /var/lib/agent-deck/*token* 2>/dev/null || true')
sleep 1
docker exec "$C1" bash -c "$install_cmd" || fail "run 2 of install.sh failed"
changed=$(docker exec "$C1" find /etc /opt/agent-deck -newer /tmp/marker -type f 2>/dev/null || true)
[ -z "$changed" ] || fail "second run modified files: $changed"
sha2=$(docker exec "$C1" bash -c 'sha256sum /var/lib/agent-deck/*token* 2>/dev/null || true')
[ "$sha1" = "$sha2" ] || fail "token changed across runs"

# ---- migration: a box that looks like the owner's (units from the K6 fixture + a deckop user)
FIX=tests/fixtures/owner-box
[ -d "$FIX" ] || fail "missing $FIX (slice D2 captures it)"
start "$C2"
docker exec "$C2" useradd -m -s /bin/bash deckop
docker cp "$FIX/." "$C2":/fixture
docker exec "$C2" bash -c 'cp /fixture/*.service /fixture/*.timer /etc/systemd/system/ 2>/dev/null; true'
docker cp deploy/. "$C2":/deploy-src
docker exec "$C2" bash /deploy-src/install-deck.sh || fail "install-deck.sh (auto-migrate) failed"
for unit in agentdeck.service deckdoctor.service; do
  want=$(grep -v '^#' "$FIX/$unit" | sed '/^$/d')
  got=$(docker exec "$C2" systemctl cat "$unit" | grep -v '^#' | sed '/^$/d')
  [ "$want" = "$got" ] || fail "migrated $unit differs from the owner's seeded unit"
done
echo "L0 PASS"
