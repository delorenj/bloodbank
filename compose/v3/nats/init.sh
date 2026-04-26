#!/bin/sh
# Bloodbank v3 — NATS JetStream stream initialization.
#
# Reads /work/streams.json and applies each stream definition to the
# connected NATS server. Idempotent: if a stream already exists with the
# desired name, this script logs and continues; `nats stream add` returns
# a non-zero exit when the stream already exists, which we tolerate.
#
# Environment:
#   NATS_URL   -- NATS server URL. Default: nats://nats:4222
#
# Exit codes:
#   0 — all streams present after the run (new or pre-existing)
#   1 — NATS server unreachable, streams.json malformed, or apply failed
#       for an unexpected reason
#
# This script is invoked by the `nats-init` oneshot service in
# `compose/v3/docker-compose.yml`.

set -eu

NATS_URL="${NATS_URL:-nats://nats:4222}"
STREAMS_JSON="${STREAMS_JSON:-/work/streams.json}"

if [ ! -f "${STREAMS_JSON}" ]; then
  echo "nats-init: missing ${STREAMS_JSON}" >&2
  exit 1
fi

# Quick reachability check so we fail fast with a clear message.
# `nats rtt` is a pure client-side round-trip; it does not require system
# account privileges the way `nats server ping` does.
if ! nats --server="${NATS_URL}" rtt >/dev/null 2>&1; then
  echo "nats-init: cannot reach NATS at ${NATS_URL}" >&2
  exit 1
fi

apply_stream() {
  name="$1"
  subjects="$2"
  retention="$3"
  storage="$4"
  discard="$5"
  max_age="$6"
  max_msgs="$7"
  max_bytes="$8"
  replicas="$9"

  # If the stream already exists, we consider init a no-op.
  if nats --server="${NATS_URL}" stream info "${name}" >/dev/null 2>&1; then
    echo "nats-init: stream ${name} already exists, skipping"
    return 0
  fi

  echo "nats-init: creating stream ${name} (subjects=${subjects}, retention=${retention})"

  # The NATS JSM API uses `workqueue` for the work-queue retention policy;
  # the nats CLI enum is `workq` for the same concept. Translate here so
  # streams.json can stay canonical in API terms.
  cli_retention="${retention}"
  if [ "${cli_retention}" = "workqueue" ]; then
    cli_retention="workq"
  fi

  # `nats stream add --defaults` suppresses interactive prompts for fields
  # we don't configure explicitly.
  nats --server="${NATS_URL}" stream add "${name}" \
    --subjects="${subjects}" \
    --retention="${cli_retention}" \
    --storage="${storage}" \
    --discard="${discard}" \
    --max-age="${max_age}" \
    --max-msgs="${max_msgs}" \
    --max-bytes="${max_bytes}" \
    --replicas="${replicas}" \
    --defaults
}

# Extract each stream config and apply it. `jq` is included in nats-box.
streams_count=$(jq '.streams | length' "${STREAMS_JSON}")
echo "nats-init: applying ${streams_count} stream definition(s) from ${STREAMS_JSON}"

i=0
while [ "${i}" -lt "${streams_count}" ]; do
  name=$(jq -r ".streams[${i}].name" "${STREAMS_JSON}")
  subjects=$(jq -r ".streams[${i}].subjects | join(\",\")" "${STREAMS_JSON}")
  retention=$(jq -r ".streams[${i}].retention" "${STREAMS_JSON}")
  storage=$(jq -r ".streams[${i}].storage" "${STREAMS_JSON}")
  discard=$(jq -r ".streams[${i}].discard" "${STREAMS_JSON}")
  max_age=$(jq -r ".streams[${i}].max_age" "${STREAMS_JSON}")
  max_msgs=$(jq -r ".streams[${i}].max_msgs" "${STREAMS_JSON}")
  max_bytes=$(jq -r ".streams[${i}].max_bytes" "${STREAMS_JSON}")
  replicas=$(jq -r ".streams[${i}].num_replicas" "${STREAMS_JSON}")

  apply_stream \
    "${name}" \
    "${subjects}" \
    "${retention}" \
    "${storage}" \
    "${discard}" \
    "${max_age}" \
    "${max_msgs}" \
    "${max_bytes}" \
    "${replicas}"

  i=$((i + 1))
done

# Verification gate: confirm every stream is reachable from a FRESH
# connection before exiting. JetStream metadata can lag a few hundred ms
# behind a successful `stream add`; if nats-init exits while the
# metadata is still propagating, a downstream consumer (e.g. Dapr's
# pubsub.jetstream) that connects in that window can hit
# "nats: stream not found" on its initial subscribe binding. Verifying
# from a separate `nats stream info` call (different connection) forces
# the propagation to land.
echo "nats-init: verifying streams are queryable from a fresh connection"
verify_attempts=10
i=0
while [ "${i}" -lt "${streams_count}" ]; do
  name=$(jq -r ".streams[${i}].name" "${STREAMS_JSON}")
  attempt=0
  while [ "${attempt}" -lt "${verify_attempts}" ]; do
    if nats --server="${NATS_URL}" stream info "${name}" >/dev/null 2>&1; then
      echo "nats-init: ${name} OK (attempt $((attempt + 1)))"
      break
    fi
    attempt=$((attempt + 1))
    sleep 0.5
  done
  if [ "${attempt}" -ge "${verify_attempts}" ]; then
    echo "nats-init: ${name} FAILED to verify after ${verify_attempts} attempts" >&2
    exit 1
  fi
  i=$((i + 1))
done

echo "nats-init: done"
exit 0
