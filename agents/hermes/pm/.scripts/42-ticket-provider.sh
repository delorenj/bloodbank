#!/usr/bin/env bash
# Bind this agent to the repo's ONE ticket board.
#
# Source of truth is the repo-root .project.json `ticket_provider` block
# (written by the CommonProject base template). The model is:
#   ONE board per repo — the PM owns it, and its heartbeat reconciliation pass
# watches it. So we never mint a per-agent, role-suffixed board ("Foo PM" /
# "Foo Sentinel"). Instead:
#   1. If .project.json already names a board  -> BIND to it (no creation).
#   2. Otherwise (hermes run on a repo with no CommonProject board yet)
#      -> create ONE repo-named board and write it back into .project.json so
#         it becomes the SOT for every agent in this repo.
# Either way we register this agent under .project.json `agents`. role.yaml is
# identity/personality only; it must not carry board UUIDs or derived URLs.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env
resolve_plane_api_key "$PLANE_WORKSPACE"
# shellcheck source=lib/ticket-provider.sh
source "$(dirname "$0")/lib/ticket-provider.sh"

already_done 42-ticket-provider && { log "[42] ticket provider already set up — skipping"; exit 0; }

# Locate the repo-root .project.json (the SOT).
REPO_ROOT="$(project_repo_path 2>/dev/null || true)"
[ -n "$REPO_ROOT" ] || REPO_ROOT="$(cd "$ROLE_DIR/../../.." 2>/dev/null && pwd)"
PROJECT_JSON="$REPO_ROOT/.project.json"
ROLE_DIR_REL="${ROLE_DIR#"$REPO_ROOT"/}"

# pj <dotted.key> — read a string value from .project.json (empty if absent).
pj() {
  [ -f "$PROJECT_JSON" ] || { printf ''; return 0; }
  python3 - "$PROJECT_JSON" "$1" <<'PY'
import sys, json, pathlib
try:
    d = json.loads(pathlib.Path(sys.argv[1]).read_text())
except Exception:
    print(""); raise SystemExit(0)
cur = d
for k in sys.argv[2].split("."):
    if isinstance(cur, dict) and k in cur:
        cur = cur[k]
    else:
        print(""); raise SystemExit(0)
print(cur if isinstance(cur, str) else "")
PY
}

# pj_write — merge board binding (optional) + this agent into .project.json.
# args: set_provider(0|1) provider board_id workspace identifier
pj_write() {
  REPO="$REPO" REPO_ROOT="$REPO_ROOT" AGENT_ID="$AGENT_ID" ROLE="$ROLE" \
  ROLE_DIR_REL="$ROLE_DIR_REL" PROJECT_DESC="${PROJECT_DESC:-}" \
  python3 - "$PROJECT_JSON" "$@" <<'PY'
import sys, os, json, pathlib
(path, set_provider, provider, board_id, workspace, identifier) = sys.argv[1:7]
p = pathlib.Path(path)
try:
    d = json.loads(p.read_text())
    if not isinstance(d, dict):
        d = {}
except Exception:
    d = {}
repo = os.environ.get("REPO", "")
d.setdefault("project_name", repo)
d.setdefault("project_slug", repo)
if not d.get("repo_path"):
    d["repo_path"] = os.environ.get("REPO_ROOT", "")
if set_provider == "1":
    tp = d.setdefault("ticket_provider", {})
    tp["type"] = provider
    if workspace:  tp["workspace"] = workspace
    if identifier: tp["identifier"] = identifier
    if board_id:   tp["board_id"] = board_id
    tp.pop("board_url", None)
    tp["state"] = "linked" if board_id else tp.get("state", "planned")
ag = d.setdefault("agents", {})
ag[os.environ["AGENT_ID"]] = {"role": os.environ["ROLE"], "role_dir": os.environ["ROLE_DIR_REL"]}
automation = d.setdefault("automation", {})
automation.setdefault("reconcile", {"enabled": False, "grace_hours": 0, "auto_review": True})
p.write_text(json.dumps(d, indent=2) + "\n")
PY
  log "    .project.json updated (agent=$AGENT_ID)"
}

# Keep only the legacy adapter name in role.yaml. Stable board identity belongs
# in .project.json.
mirror_provider_name_to_role_yaml() {
  local provider="$1"
  yaml_set ticket_provider.name "$provider" 2>/dev/null || true
}

# ── Provider resolution ──────────────────────────────────────────────────
# An existing repo board (in .project.json) wins — every agent binds to it.
SOT_TYPE="$(pj ticket_provider.type)"
SOT_BOARD_ID="$(pj ticket_provider.board_id)"
SOT_WS="$(pj ticket_provider.workspace)"
SOT_IDENT="$(pj ticket_provider.identifier)"

# role.yaml provider comes from copier --data (the operator's pjangler choice).
ROLE_PROVIDER="$(yaml_get ticket_provider.name)"

if [ -n "$SOT_BOARD_ID" ]; then
  # ── BIND to the repo's existing board ──────────────────────────────────
  PROVIDER="${SOT_TYPE:-${ROLE_PROVIDER:-plane}}"
  if [ -n "$ROLE_PROVIDER" ] && [ "$ROLE_PROVIDER" != "$PROVIDER" ]; then
    warn "[42] requested provider '$ROLE_PROVIDER' but repo board is '$PROVIDER' (.project.json wins); binding to existing board"
  fi
  case "$PROVIDER" in plane|trello) ;; *) die "unsupported ticket provider in .project.json: $PROVIDER (expected plane|trello)" ;; esac
  log "[42] binding $AGENT_ID to existing repo board (provider=$PROVIDER, id=$SOT_BOARD_ID)"
  mirror_provider_name_to_role_yaml "$PROVIDER"
  pj_write 1 "$PROVIDER" "$SOT_BOARD_ID" "$SOT_WS" "$SOT_IDENT"
  mark_done 42-ticket-provider
  exit 0
fi

# ── No repo board yet: create ONE repo-named board (no role suffix) ───────
PROVIDER="${ROLE_PROVIDER:-${SOT_TYPE:-plane}}"
log "[42] no board in .project.json — bootstrapping a repo board (provider: $PROVIDER)"

# Repo-based identity, matching CommonProject's scheme (slug[:4] uppercased).
RAW=$(printf '%s' "$REPO" | tr -cd '[:alnum:]' | tr '[:lower:]' '[:upper:]')
while [ ${#RAW} -lt 2 ]; do RAW="${RAW}X"; done
IDENT="${SOT_IDENT:-${RAW:0:4}}"
# Board NAME = repo name, separators->space, title-cased. NOT display_name —
# display_name carries the role suffix and must never become the board name.
NAME="$(printf '%s' "$REPO" | tr '_-' '  ' | python3 -c 'import sys; print(" ".join(w[:1].upper()+w[1:] for w in sys.stdin.read().split()))')"
DESC="Ticket board for $REPO"

case "$PROVIDER" in
  plane|trello)
    KEYVAR=PLANE_API_KEY; [ "$PROVIDER" = trello ] && KEYVAR=TRELLO_KEY
    if [[ -z "${!KEYVAR:-}" ]]; then
      warn "[42] $KEYVAR not set; skipping board creation. Set creds and re-run ./.scripts/42-ticket-provider.sh"
      pj_write 1 "$PROVIDER" "" "${SOT_WS:-${PLANE_WORKSPACE:-}}" "$IDENT"
      mark_done 42-ticket-provider; exit 0
    fi
    OUT="$(tp create_board "$NAME" "$IDENT" "$DESC")" || die "create_board failed for $PROVIDER"
    BID="$(printf '%s' "$OUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("board_id",""))')"
    WS="${SOT_WS:-${PLANE_WORKSPACE:-}}"
    [ "$PROVIDER" = trello ] && WS=""
    mirror_provider_name_to_role_yaml "$PROVIDER"
    pj_write 1 "$PROVIDER" "$BID" "$WS" "$IDENT"
    ;;

  *) die "unknown ticket provider: $PROVIDER (expected plane|trello)" ;;
esac

mark_done 42-ticket-provider
