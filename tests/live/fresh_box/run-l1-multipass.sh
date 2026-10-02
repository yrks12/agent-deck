#!/usr/bin/env bash
# L1 -- local VM, full install incl. docker, pin path, update/rollback (~20 min, Mac + Multipass).
# NOT run by the slice that wrote it (needs a VM). Expected to fail until D2/D3/F1/R0 land.
set -euo pipefail
cd "$(dirname "$0")/../../.."
VM=${L1_VM:-deckvm}
BASE=${DECK_RELEASE_BASE:?set DECK_RELEASE_BASE to the local release dir URL (python -m http.server over dist/)}
BROKEN=${L1_BROKEN_TARBALL:?set L1_BROKEN_TARBALL to a deliberately broken server tarball URL under BASE}
fail() { echo "L1 FAIL: $*" >&2; exit 1; }
trap 'multipass delete --purge "$VM" >/dev/null 2>&1 || true' EXIT

multipass launch 24.04 --name "$VM" --cpus 2 --memory 4G --disk 20G
multipass transfer deploy/install.sh "$VM":/tmp/install.sh
multipass exec "$VM" -- sudo env DECK_RELEASE_BASE="$BASE" bash /tmp/install.sh --yes --tls=self-signed --skip-login \
  || fail "install failed"
multipass exec "$VM" -- sudo deckctl doctor --json | jq -e '[.checks[]|select(.ok!=true and .name!="claude_login")]|length==0' >/dev/null \
  || fail "doctor not green (docker ON path)"

# pin path: the real app client redeems a real code through the pinned session
code=$(multipass exec "$VM" -- sudo deckctl pair --json | jq -r .code)
ip=$(multipass info "$VM" --format json | jq -r ".info[\"$VM\"].ipv4[0]")
DECK_PAIR_CODE="$code" DECK_STATE_SUITE=livepair swift test --package-path macos --filter LivePairingTests \
  || fail "LivePairingTests failed (parse tls=pin, redeem, GET /v1/agents 200, 2nd redeem 409, wrong pin)"

# update with a broken tarball must roll back and leave /healthz ok
multipass exec "$VM" -- sudo deckctl update --to "$BROKEN" && fail "broken update was accepted"
curl -sk "https://$ip/healthz" | jq -e '.ok == true' >/dev/null || fail "/healthz down after rollback"

# uninstall keeps data, --purge removes it
multipass exec "$VM" -- sudo deckctl uninstall
curl -sk --max-time 5 "https://$ip/healthz" >/dev/null && fail "443 still answers after uninstall"
multipass exec "$VM" -- test -d /var/lib/agent-deck || fail "uninstall must keep /var/lib/agent-deck"
multipass exec "$VM" -- sudo deckctl uninstall --purge
multipass exec "$VM" -- test ! -e /var/lib/agent-deck || fail "--purge left /var/lib/agent-deck"
echo "L1 PASS"
