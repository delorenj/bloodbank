#!/usr/bin/env bash
# deploy — the self-contained-node deploy hook.
# codegen (regenerate event list from bloodbank/schemas) -> build -> install into
# ~/.n8n/nodes -> restart n8n. Run whenever the bloodbank event schemas change.
set -euo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODES_DIR="${N8N_NODES_DIR:-$HOME/.n8n/nodes}"
# The mise `pm2`/`node` shims are unreliable (no pinned global); use the node dir directly.
NODE_BIN_DIR="${N8N_NODE_BIN_DIR:-/home/delorenj/.local/share/mise/installs/node/24.6.0/bin}"
export PATH="$NODE_BIN_DIR:$PATH"

echo "[deploy] building n8n-nodes-bloodbank (codegen + tsc)…"
cd "$PKG_DIR"
npm run build

echo "[deploy] installing into $NODES_DIR…"
mkdir -p "$NODES_DIR"
( cd "$NODES_DIR" && npm install "$PKG_DIR" )

echo "[deploy] restarting n8n (PM2_HOME=/home/delorenj/.pm2)…"
PM2_HOME=/home/delorenj/.pm2 "$NODE_BIN_DIR/pm2" restart n8n

echo "[deploy] done — n8n-nodes-bloodbank deployed."
