#!/usr/bin/env bash
# Link the Bloodbank gateway plugin into the Hermes user plugin dir and let PM
# sync its dependencies. Idempotent. Restart hermes-gateway.service afterwards.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${HERMES_HOME:-$HOME/.hermes}/plugins/bloodbank-platform"
mkdir -p "$(dirname "$dest")"
if [ -e "$dest" ] && [ ! -L "$dest" ]; then
  echo "replacing existing plugin dir $dest" >&2
  rm -rf "$dest"
fi
ln -sfn "$here" "$dest"
echo "linked $dest -> $here"
hermes_bin="${HERMES_BIN:-$HOME/.hermes/hermes-agent/.hermes/bin/hermes}"
"$hermes_bin" pm install 2>&1 | tail -30
