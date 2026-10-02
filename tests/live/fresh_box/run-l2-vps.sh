#!/usr/bin/env bash
# L2 -- the real thing: fresh public VPS, the literal one-liner (~25 min, < $1).
# NOT run by the slice that wrote it. Provider CLI is Owner decision D4; this script drives
# whatever VPS you hand it, so it stays provider-neutral:
#   VPS_HOST=<ip> VPS_SSH='ssh root@<ip>' INSTALL_URL=<url of install.sh for 0.9.0-rc1> \
#   TEST_CLAUDE_TOKEN_FILE=... TEST_OPENAI_KEY_FILE=... bash run-l2-vps.sh
# Teardown (destroy the VPS, revoke the test Claude token) is the caller's; do it afterwards.
set -euo pipefail
: "${VPS_HOST:?}" "${VPS_SSH:?}" "${INSTALL_URL:?}" "${TEST_CLAUDE_TOKEN_FILE:?}" "${TEST_OPENAI_KEY_FILE:?}"
fail() { echo "L2 FAIL: $*" >&2; exit 1; }
HOST=$(echo "$VPS_HOST" | tr . -).sslip.io

scp -q "$TEST_CLAUDE_TOKEN_FILE" "root@$VPS_HOST:/root/test-token"
scp -q "$TEST_OPENAI_KEY_FILE" "root@$VPS_HOST:/root/test-openai"
$VPS_SSH "curl -fsSL $INSTALL_URL | sudo bash -s -- --yes --claude-token-file /root/test-token --openai-key-file /root/test-openai" \
  || fail "the one-liner failed"

curl -sf "https://$HOST/healthz" | jq -e '.ok == true' >/dev/null || fail "healthz over a publicly valid cert"
[ "$(curl -s -o /dev/null -w '%{http_code}' "https://$HOST/api/state")" = "404" ] || fail "/api/state must be 404"
nc -z -w 3 "$VPS_HOST" 7789 && fail "7789 reachable from the internet"
open=$(nmap -Pn --top-ports 1000 "$VPS_HOST" | awk '/\/tcp *open/{print $1}' | sort | tr '\n' ' ')
[ "$open" = "22/tcp 443/tcp " ] || fail "expected only 22 and 443 open, got: $open"

last=000
for i in $(seq 11); do
  last=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer wrong-$i" "https://$HOST/v1/agents")
done
[ "$last" = "429" ] || fail "11th wrong bearer must answer 429, got $last"
$VPS_SSH "journalctl -u agentdeck --no-pager | grep -c deck-auth-fail" | grep -qv '^0$' || fail "no deck-auth-fail lines"
$VPS_SSH "journalctl -u agentdeck --no-pager | grep -q 'wrong-1'" && fail "journal contains token text"

$VPS_SSH "sudo deckctl doctor" | grep -q FAIL && fail "doctor has a FAIL"
$VPS_SSH "sudo -u agentdeck claude -p 'reply ok'" | grep -qi ok || fail "claude login/desk did not answer"
$VPS_SSH "sudo deckctl pair --json" | jq -r .code > /tmp/l2-pair-code
echo "L2 server asserts PASS. Pair code in /tmp/l2-pair-code; now run ConnectFlowUITests (click-path)."
