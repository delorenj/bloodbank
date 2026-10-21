#!/usr/bin/env bash
# Resolve .env.op (op:// references) into the gitignored, 0600 .env next to
# compose.yml. Compose reads .env automatically, so afterwards a plain
# `docker compose up -d --force-recreate` has the ntfy token with nothing in the
# shell. (op run masks stdout by default, hence --no-masking.) Run at boot by docker-stack-ntfy-router.service; rerun after rotating
# the token in 1Password.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
umask 077
tmp="$(mktemp .env.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
op run --no-masking --env-file=.env.op -- sh -c \
  'test -n "$BLOODBANK_NTFY_ROUTER_NTFY_TOKEN" && printf "BLOODBANK_NTFY_ROUTER_NTFY_TOKEN=%s\n" "$BLOODBANK_NTFY_ROUTER_NTFY_TOKEN"' >"$tmp"
test -s "$tmp" || { echo "render-env: token did not resolve" >&2; exit 1; }
mv -f "$tmp" .env
trap - EXIT
echo "render-env: wrote $(pwd)/.env"
