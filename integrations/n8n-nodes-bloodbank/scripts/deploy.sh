#!/usr/bin/env bash
# deploy — build an isolated production package, install it into n8n's community
# node directory, then restart n8n.
#
# Identity is the CONTENT of dist/, not the version string: 0.4.0 shipped twice
# with different code, and a version check called the stale copy current. The
# deploy compares a content hash of the built package against the installed
# copy, skips the install and restart when they are identical, and verifies the
# installed hash after copying when they are not.
#
#   npm run deploy               build, install if changed, restart if installed
#   npm run deploy -- --force    install and restart even when the hash matches
#   npm run deploy -- --check    build and report drift only; exit 3 on drift
set -euo pipefail

FORCE=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --check) CHECK_ONLY=1 ;;
    *) echo "[deploy] unknown argument: $arg" >&2; exit 2 ;;
  esac
done

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODES_DIR="${N8N_NODES_DIR:-$HOME/.n8n/nodes}"
# The mise `pm2`/`node` shims are unreliable (no pinned global); use the node dir directly.
NODE_BIN_DIR="${N8N_NODE_BIN_DIR:-/home/delorenj/.local/share/mise/installs/node/24.6.0/bin}"
export PATH="$NODE_BIN_DIR:$PATH"

# Content hash of a package directory: every file under dist/ plus package.json,
# by relative path and bytes. node_modules is excluded: it is resolved per
# install and is verified separately below.
package_hash() {
  node - "$1" <<'NODE'
const { createHash } = require('node:crypto');
const { readdirSync, readFileSync, statSync, existsSync } = require('node:fs');
const { join, relative } = require('node:path');
const root = process.argv[2];
if (!existsSync(join(root, 'package.json')) || !existsSync(join(root, 'dist'))) {
  process.stdout.write('absent');
  process.exit(0);
}
const files = [];
const walk = (dir) => {
  for (const name of readdirSync(dir).sort()) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path);
    else files.push(path);
  }
};
walk(join(root, 'dist'));
files.push(join(root, 'package.json'));
const hash = createHash('sha256');
for (const file of files.sort()) {
  hash.update(relative(root, file)).update('\0').update(readFileSync(file)).update('\0');
}
process.stdout.write(hash.digest('hex'));
NODE
}

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

DEST_DIR="$NODES_DIR/node_modules/n8n-nodes-bloodbank"
BUILT_HASH="$(package_hash "$STAGE_DIR/package")"
INSTALLED_HASH="$(package_hash "$DEST_DIR")"
echo "[deploy] built     ${BUILT_HASH}"
echo "[deploy] installed ${INSTALLED_HASH}"

if [[ "$CHECK_ONLY" == 1 ]]; then
  if [[ "$BUILT_HASH" == "$INSTALLED_HASH" ]]; then
    echo "[deploy] installed copy matches the build"
    exit 0
  fi
  echo "[deploy] DRIFT: the installed copy is not this build" >&2
  exit 3
fi

if [[ "$BUILT_HASH" == "$INSTALLED_HASH" && "$FORCE" != 1 ]]; then
  echo "[deploy] installed copy already matches this build; nothing to install, n8n not restarted (use --force to restart anyway)"
  exit 0
fi

(
  cd "$STAGE_DIR/package"
  npm install --omit=dev --ignore-scripts --package-lock=false
)

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
# --checksum is load-bearing: npm pack stamps every file with the same fixed
# mtime (1985-10-26), so rsync's default size+mtime quick check silently skips
# any file whose size did not change — package.json going 0.4.0 -> 0.5.0, or a
# same-length edit in dist/. That is how a stale build kept reporting current.
rsync -a --checksum --delete "$STAGE_DIR/package/" "$DEST_DIR/"

echo "[deploy] verifying the installed copy…"
EXPECTED_VERSION="$(node -p "require('$PKG_DIR/package.json').version")"
node - "$DEST_DIR" "$EXPECTED_VERSION" <<'NODE'
const destination = process.argv[2];
const expectedVersion = process.argv[3];
for (const dependency of ['@nats-io/transport-node', 'yaml', 'ajv']) {
  require.resolve(dependency, { paths: [destination] });
}
const pkg = require(destination + '/package.json');
if (pkg.version !== expectedVersion) {
  throw new Error(`unexpected deployed version: ${pkg.version}; expected ${expectedVersion}`);
}
console.log(`[deploy] installed ${pkg.name}@${pkg.version}`);
NODE
FINAL_HASH="$(package_hash "$DEST_DIR")"
if [[ "$FINAL_HASH" != "$BUILT_HASH" ]]; then
  echo "[deploy] installed content hash ${FINAL_HASH} does not match the build ${BUILT_HASH}" >&2
  exit 1
fi
echo "[deploy] installed content hash matches the build"

echo "[deploy] restarting n8n (PM2_HOME=/home/delorenj/.pm2)…"
PM2_HOME=/home/delorenj/.pm2 "$NODE_BIN_DIR/pm2" restart n8n

echo "[deploy] done — n8n-nodes-bloodbank ${EXPECTED_VERSION} (${BUILT_HASH:0:12}) deployed."
