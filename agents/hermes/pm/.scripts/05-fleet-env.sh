#!/usr/bin/env bash
# Ensure a single shared fleet source-of-truth file exists.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 05-fleet-env && log "[05] fleet env already checked — refreshing shared keys"

mkdir -p "$(dirname "$FLEET_ENV")"

if [[ ! -f "$FLEET_ENV" ]]; then
  log "[05] creating fleet source-of-truth: $FLEET_ENV"
  cat > "$FLEET_ENV" <<EOF
# Hermes fleet source of truth.
# All generated wrappers and provisioning scripts read this file.
HERMES_FLEET_BIN=${HERMES_BIN}
HERMES_FLEET_REPO=${HERMES_AGENT_REPO}
HERMES_FLEET_REGISTRY_FILE=${REGISTRY_FILE}
HERMES_FLEET_OAUTH_FILE=${HERMES_OAUTH_FILE}
HERMES_FLEET_CODEX_HOME=${CODEX_HOME}
EOF
  chmod 600 "$FLEET_ENV"
else
  log "[05] fleet env exists: $FLEET_ENV"
fi

upsert_fleet_env() {
  local key="$1" value="$2"
  python3 - "$FLEET_ENV" "$key" "$value" <<'PYEOF'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
line = f"{key}={value}"
lines = path.read_text().splitlines() if path.exists() else []
for idx, existing in enumerate(lines):
    if existing.startswith(f"{key}="):
        lines[idx] = line
        break
else:
    lines.append(line)
path.write_text("\n".join(lines) + "\n")
PYEOF
}

upsert_fleet_env HERMES_FLEET_OAUTH_FILE "$HERMES_OAUTH_FILE"
upsert_fleet_env HERMES_FLEET_CODEX_HOME "$CODEX_HOME"

PLANE_WORKSPACE_KEY="$(plane_workspace_key "$PLANE_WORKSPACE")"
PLANE_WORKSPACE_VALUE="${!PLANE_WORKSPACE_KEY:-}"
if [[ -z "$PLANE_WORKSPACE_VALUE" && -n "${PLANE_API_KEY:-}" ]]; then
  PLANE_WORKSPACE_VALUE="$PLANE_API_KEY"
fi
if [[ -n "$PLANE_WORKSPACE_VALUE" ]]; then
  upsert_fleet_env "$PLANE_WORKSPACE_KEY" "$PLANE_WORKSPACE_VALUE"
fi
chmod 600 "$FLEET_ENV"

mark_done 05-fleet-env
