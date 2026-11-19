#!/usr/bin/env bash
# Link the Bloodbank gateway plugin into the Hermes launch home AND into every
# profile that enables it, then let PM sync its dependencies. Idempotent.
# Restart hermes-gateway afterwards (hermes-gateway-restart --reason ...).
#
# Why every profile: in a multiplexed gateway each profile has its own plugin
# manager, and a turn fires its hooks on the routed profile's manager. The
# final-answer observer (pre_llm_call/post_llm_call/on_session_end) must be
# registered there, or prepare_capture rejects every command for that profile
# ("resolved profile lacks the final-answer observer"). A link only in the
# launch home is invisible to profile managers (BB-43, 2026-11-19: all 23
# queued commands were rejected on the first boot that ran the check).
#
#   --links-only   skip `hermes pm install` (use when only links changed)
#   --check        report missing links and exit 1 if any; change nothing
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
home="${HERMES_HOME:-$HOME/.hermes}"
mode="install"
case "${1:-}" in
  --links-only) mode="links" ;;
  --check) mode="check" ;;
  "") ;;
  *) echo "usage: $0 [--links-only|--check]" >&2; exit 2 ;;
esac

targets=("$home")
for cfg in "$home"/profiles/*/config.yaml; do
  [ -f "$cfg" ] || continue
  # Only profiles whose config enables the plugin; others never load it.
  if grep -Eq '^[[:space:]]*-[[:space:]]*bloodbank-platform[[:space:]]*$' "$cfg"; then
    targets+=("$(dirname "$cfg")")
  fi
done

missing=0
for t in "${targets[@]}"; do
  dest="$t/plugins/bloodbank-platform"
  if [ -L "$dest" ] && [ "$(readlink -f "$dest")" = "$(readlink -f "$here")" ]; then
    continue
  fi
  if [ "$mode" = "check" ]; then
    echo "missing: $dest" >&2
    missing=$((missing + 1))
    continue
  fi
  mkdir -p "$(dirname "$dest")"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    echo "replacing existing plugin dir $dest" >&2
    rm -rf "$dest"
  fi
  ln -sfn "$here" "$dest"
  echo "linked $dest -> $here"
done

if [ "$mode" = "check" ]; then
  [ "$missing" -eq 0 ] && echo "ok: ${#targets[@]} homes link bloodbank-platform"
  exit $((missing > 0))
fi
[ "$mode" = "links" ] && exit 0
hermes_bin="${HERMES_BIN:-$HOME/.hermes/hermes-agent/.hermes/bin/hermes}"
"$hermes_bin" pm install 2>&1 | tail -30
