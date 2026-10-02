#!/usr/bin/env bash
# The README's server one-liner, word for word, in a fresh Ubuntu 24.04 with systemd.
#
#   tests/live/fresh_box/run-oneliner.sh
#
# The box starts with what a cloud image has (systemd, sudo, curl, openssl) and NOT
# git or python3: the one-liner has to bring those itself. It then:
#   1. pipes install.sh from GitHub's raw host into bash  (timed)
#   2. checks /healthz through the edge the installer built
#   3. runs the one-liner AGAIN (a re-run is an upgrade) and checks nothing broke (timed)
#   4. starts `deckctl login` on a terminal and stops at Claude's sign-in link
#
# Testing a PRIVATE repository: set GITHUB_TOKEN. It is sent to the raw host as a
# header and used in the clone URL inside the throwaway container only.
#   ONELINER_REPO   owner/repo   (default: the slug install.sh carries)
#   ONELINER_REF    branch       (default main)
#   ONELINER_FLAGS  extra flags  (default --no-docker: no docker-in-docker here)
set -euo pipefail
cd "$(dirname "$0")/../../.."
REPO_DIR=$PWD
IMG=deck-oneliner-box
C=deck-oneliner
SLUG="${ONELINER_REPO:-$(sed -n 's/^AGENT_DECK_REPO="\${AGENT_DECK_REPO:-\(.*\)}"$/\1/p' install.sh)}"
REF="${ONELINER_REF:-main}"
FLAGS="${ONELINER_FLAGS:---no-docker}"
RAW="https://raw.githubusercontent.com/${SLUG}/${REF}/install.sh"
fail() { echo "ONELINER FAIL: $*" >&2; exit 1; }
trap 'docker rm -f "$C" >/dev/null 2>&1 || true' EXIT

docker build -q -t "$IMG" - >/dev/null <<'DOCKERFILE'
FROM ubuntu:24.04
ENV container=docker
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus sudo curl ca-certificates iproute2 openssl \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /lib/systemd/system/multi-user.target.wants/* /etc/systemd/system/*.wants/* \
             /lib/systemd/system/local-fs.target.wants/* \
             /lib/systemd/system/sockets.target.wants/*udev* \
             /lib/systemd/system/sockets.target.wants/*initctl* \
             /lib/systemd/system/sysinit.target.wants/systemd-tmpfiles-setup*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
DOCKERFILE

docker rm -f "$C" >/dev/null 2>&1 || true
docker run -d --name "$C" --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw "$IMG" >/dev/null
for _ in $(seq 30); do
  docker exec "$C" systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && break
  sleep 1
done
docker exec "$C" sh -c 'command -v git || command -v python3' && fail "the box already has git/python3"

AUTH_HDR="" CLONE_URL=""
if [ -n "${GITHUB_TOKEN:-}" ]; then
  AUTH_HDR="Authorization: token ${GITHUB_TOKEN}"
  CLONE_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/${SLUG}.git"
fi

oneliner() {
  docker exec -e AUTH_HDR="$AUTH_HDR" -e AGENT_DECK_REPO_URL="$CLONE_URL" -e AGENT_DECK_REF="$REF" \
    -e RAW="$RAW" -e FLAGS="$FLAGS" "$C" bash -c '
      [ -n "$AGENT_DECK_REPO_URL" ] || unset AGENT_DECK_REPO_URL
      if [ -n "$AUTH_HDR" ]; then H=(-H "$AUTH_HDR"); else H=(); fi
      curl -fsSL "${H[@]}" "$RAW" | bash -s -- $FLAGS'
}

echo "== run 1: $RAW | bash -s -- $FLAGS"
t0=$(date +%s)
oneliner > /tmp/oneliner-run1.log 2>&1 || { tail -40 /tmp/oneliner-run1.log; fail "run 1 failed"; }
t1=$(date +%s)
tail -14 /tmp/oneliner-run1.log | sed -E 's/ADK1\.[A-Za-z0-9_-]+/ADK1.<code>/'

HOST="$(docker exec "$C" /opt/agent-deck/bin/deckctl config get network.hostname)"
# An IP is dialled as it is (no SNI: caddy picks the certificate by address); a
# name is pinned to loopback.
healthz() {
  if [[ "$HOST" =~ ^[0-9.]+$ ]]; then docker exec "$C" curl -sk "https://${HOST}/healthz"
  else docker exec "$C" curl -sk --resolve "${HOST}:443:127.0.0.1" "https://${HOST}/healthz"; fi
}
HZ="$(healthz)"
echo "$HZ" | grep -q '"ok": *true' || fail "/healthz via https://${HOST} said: ${HZ}"
t2=$(date +%s)
echo "== /healthz ok via https://${HOST}: ${HZ}"

echo "== run 2 (re-run = upgrade)"
oneliner > /tmp/oneliner-run2.log 2>&1 || { tail -40 /tmp/oneliner-run2.log; fail "run 2 failed"; }
t3=$(date +%s)
grep -q 'already at the latest' /tmp/oneliner-run2.log || fail "run 2 did not see the checkout as current"
HZ="$(healthz)"
echo "$HZ" | grep -q '"ok": *true' || fail "/healthz after run 2 said: ${HZ}"

echo "== the Claude sign-in (stops at the link)"
LINK="$(docker exec -t "$C" timeout 60 /opt/agent-deck/bin/deckctl login 2>&1 </dev/null \
  | tr -d '\r' | grep -oE 'https://[^ ]*(claude|anthropic)[^ ]*' | head -n 1 || true)"
[ -n "$LINK" ] || fail "deckctl login never printed a sign-in link"
t4=$(date +%s)
echo "sign-in link host: $(printf '%s' "$LINK" | sed -E 's#(https://[^/]+).*#\1#')"

echo "ONELINER PASS: install $((t1 - t0))s, to /healthz $((t2 - t0))s, re-run $((t3 - t2))s, sign-in link $((t4 - t3))s"
