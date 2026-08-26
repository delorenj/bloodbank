#!/usr/bin/env bash
# deploy — build an isolated production package, install it into n8n's community
# node directory, then restart n8n.
set -euo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODES_DIR="${N8N_NODES_DIR:-$HOME/.n8n/nodes}"
# The mise `pm2`/`node` shims are unreliable (no pinned global); use the node dir directly.
NODE_BIN_DIR="${N8N_NODE_BIN_DIR:-/home/delorenj/.local/share/mise/installs/node/24.6.0/bin}"
export PATH="$NODE_BIN_DIR:$PATH"

echo "[deploy] building n8n-nodes-bloodbank (codegen + tsc)…"
cd "$PKG_DIR"
npm run build

echo "[deploy] staging an isolated production package…"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/n8n-bloodbank-deploy.XXXXXX")"
cleanup() {
  find "$STAGE_DIR" -depth -delete 2>/dev/null || true
}
trap cleanup EXIT
npm pack --pack-destination "$STAGE_DIR" >/dev/null
TARBALL="$(find "$STAGE_DIR" -maxdepth 1 -type f -name 'n8n-nodes-bloodbank-*.tgz' -print -quit)"
[[ -n "$TARBALL" ]] || { echo "[deploy] package tarball was not created" >&2; exit 1; }
tar -xzf "$TARBALL" -C "$STAGE_DIR"
(
  cd "$STAGE_DIR/package"
  npm install --omit=dev --ignore-scripts --package-lock=false
)

DEST_DIR="$NODES_DIR/node_modules/n8n-nodes-bloodbank"
mkdir -p "$NODES_DIR/node_modules"
if [[ -L "$DEST_DIR" ]]; then
  CURRENT_TARGET="$(readlink -f "$DEST_DIR")"
  if [[ "$CURRENT_TARGET" != "$PKG_DIR" ]]; then
    echo "[deploy] refusing to replace unexpected symlink: $DEST_DIR -> $CURRENT_TARGET" >&2
    exit 1
  fi
  unlink "$DEST_DIR"
fi
mkdir -p "$DEST_DIR"
rsync -a --delete "$STAGE_DIR/package/" "$DEST_DIR/"

echo "[deploy] verifying isolated runtime dependencies…"
EXPECTED_VERSION="$(node -p "require('$PKG_DIR/package.json').version")"
node - "$DEST_DIR" "$EXPECTED_VERSION" <<'NODE'
const destination = process.argv[2];
const expectedVersion = process.argv[3];
for (const dependency of ['@nats-io/transport-node', 'yaml']) {
  require.resolve(dependency, { paths: [destination] });
}
const pkg = require(destination + '/package.json');
if (pkg.version !== expectedVersion) {
  throw new Error(`unexpected deployed version: ${pkg.version}; expected ${expectedVersion}`);
}
console.log(`[deploy] installed ${pkg.name}@${pkg.version}`);
NODE

echo "[deploy] restarting n8n (PM2_HOME=/home/delorenj/.pm2)…"
PM2_HOME=/home/delorenj/.pm2 "$NODE_BIN_DIR/pm2" restart n8n

echo "[deploy] done — n8n-nodes-bloodbank deployed."
