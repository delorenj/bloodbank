#!/usr/bin/env bash
# Safe wrapper for GitHub issue/PR creation that enforces --body-file usage.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  gh_safe_create.sh issue --title "..." [--label bug] [--repo owner/repo] --body-file /path/body.md
  gh_safe_create.sh pr    --title "..." --base main --head branch [--repo owner/repo] --body-file /path/body.md

Notes:
  - This wrapper rejects inline --body usage.
  - Remaining arguments are passed through to gh <type> create.
EOF
}

if [ "$#" -lt 2 ]; then
  usage
  exit 2
fi

kind="$1"
shift

case "$kind" in
  issue|pr) ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    echo "ERROR: first argument must be 'issue' or 'pr'" >&2
    usage
    exit 2
    ;;
esac

args=("$@")
body_file=""

for ((i=0; i<${#args[@]}; i++)); do
  token="${args[$i]}"

  case "$token" in
    --body|--body=*)
      echo "ERROR: inline --body is not allowed. Use --body-file <path>." >&2
      exit 2
      ;;
    --body-file)
      if [ $((i+1)) -ge ${#args[@]} ]; then
        echo "ERROR: --body-file requires a value." >&2
        exit 2
      fi
      body_file="${args[$((i+1))]}"
      ;;
    --body-file=*)
      body_file="${token#--body-file=}"
      ;;
  esac
done

if [ -z "$body_file" ]; then
  echo "ERROR: --body-file is required." >&2
  exit 2
fi

if [ ! -f "$body_file" ]; then
  echo "ERROR: body file not found: $body_file" >&2
  exit 2
fi

if [ ! -s "$body_file" ]; then
  echo "ERROR: body file is empty: $body_file" >&2
  exit 2
fi

gh "$kind" create "${args[@]}"
