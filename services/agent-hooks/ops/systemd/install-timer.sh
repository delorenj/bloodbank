#!/usr/bin/env bash
# Install the systemd USER units for the agent-hooks health check. Idempotent.
#
# Scheduling is owned by candybar (33GOD/candybar deploy/migration/ledger.yaml,
# id systemd:bloodbank-agent-hooks-health.timer): the .service stays for
# candybar's systemd runner, the .timer is RETIRED — never re-enable it here.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$DEST"

for unit in bloodbank-agent-hooks-health.service bloodbank-agent-hooks-health.timer; do
  cp "$SRC/$unit" "$DEST/$unit"
  echo "installed $DEST/$unit"
done

systemctl --user daemon-reload
if systemctl --user is-enabled bloodbank-agent-hooks-health.timer >/dev/null 2>&1; then
  echo "WARNING: bloodbank-agent-hooks-health.timer is enabled but retired — disable it:" >&2
  echo "  systemctl --user disable --now bloodbank-agent-hooks-health.timer" >&2
fi
echo "installed; scheduling owned by candybar (do NOT enable the .timer)"
