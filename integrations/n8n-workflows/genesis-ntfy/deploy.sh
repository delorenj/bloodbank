#!/usr/bin/env bash
# Upsert "Genesis -> ntfy" into n8n through the public API and activate it.
#   ./deploy.sh            build, create-or-update by name, activate, read back
# The API key is read from 1Password into process memory only. Re-running is safe:
# the workflow is matched by name and PUT in place, so its id and execution history stay.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
API="${N8N_API:-http://127.0.0.1:5678/api/v1}"
KEY="$(op read 'op://DeLoSecrets/5pyy6ct5pi7eaz436g2qkn6pjq/api key')"
NAME="Genesis -> ntfy"

node build.mjs >/dev/null
node --test test/format.test.mjs >/dev/null

call() { curl -fsS -H "X-N8N-API-KEY: $KEY" -H 'Content-Type: application/json' "$@"; }

BODY="$(python3 -c 'import json;w=json.load(open("workflow.json"));print(json.dumps({k:w[k] for k in ("name","nodes","connections","settings")}))')"
ID="$(call "$API/workflows?limit=250" | python3 -c 'import json,sys;n=sys.argv[1];print(next((w["id"] for w in json.load(sys.stdin)["data"] if w["name"]==n),""))' "$NAME")"

if [[ -n "$ID" ]]; then
  echo "[genesis-ntfy] updating $ID"
  call -X PUT "$API/workflows/$ID" -d "$BODY" >/dev/null
else
  echo "[genesis-ntfy] creating"
  ID="$(call -X POST "$API/workflows" -d "$BODY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')"
fi
call -X POST "$API/workflows/$ID/activate" -d '{}' >/dev/null
call "$API/workflows/$ID" | python3 -c 'import json,sys;w=json.load(sys.stdin);print("[genesis-ntfy] id=%s active=%s nodes=%d"%(w["id"],w["active"],len(w["nodes"])));sys.exit(0 if w["active"] else 1)'
