#!/usr/bin/env bash
# Install + enable the systemd USER timer that runs the agent-hooks health check
# every 5 minutes (mirrors the hermes checkpoint-timer pattern). Idempotent.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$DEST"

for unit in bloodbank-agent-hooks-health.service bloodbank-agent-hooks-health.timer; do
  cp "$SRC/$unit" "$DEST/$unit"
  echo "installed $DEST/$unit"
done

systemctl --user daemon-reload
systemctl --user enable --now bloodbank-agent-hooks-health.timer
echo "enabled bloodbank-agent-hooks-health.timer (OnUnitActiveSec=5min)"
systemctl --user list-timers bloodbank-agent-hooks-health.timer --no-pager || true
