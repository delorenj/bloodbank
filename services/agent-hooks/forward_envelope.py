#!/usr/bin/env python3
"""Forward a pre-built Bloodbank CloudEvent from stdin to its NATS subject.

The canonical ``publish.py`` entrypoint *builds* envelopes from agent-CLI hook
payloads. Some producers already build and validate their own envelope and only
need transport -- pr-crusher, for one, constructs a complete CloudEvent, checks
it against its own schema, then hands it to an external publisher command on
stdin. This is that publisher.

Contract:
    stdin  -- one CloudEvents 1.0 JSON object with a ``subject`` field
    exit 0 -- published to NATS
    exit 1 -- not published (reason on stderr)

Deliberately does NOT re-validate the producer's ``data`` payload. The producer
owns its schema; duplicating that check here would drift. This forwards, and it
is strict only about the things transport actually depends on.

Runs under a stripped environment (no HOME, minimal PATH, no inherited vars) --
callers such as pr-crusher spawn it with ``env={"PATH": ..., "HOME": ...}``.
Every default must therefore be correct with no configuration present.

Stdlib-only.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from core.nats_publish import publish as nats_publish  # noqa: E402

MAX_INPUT_BYTES = 1024 * 1024
CLIENT_NAME = "bloodbank-envelope-forwarder"

# Mirrors candystore.db.REQUIRED_ENVELOPE_FIELDS plus the two fields transport
# itself depends on. Candystore DROPs an envelope missing any of these, so
# publishing one would return "published" while persisting nothing -- refuse
# instead, and let the producer record a real failure.
REQUIRED_FIELDS = (
    "specversion",
    "id",
    "source",
    "type",
    "subject",
    "time",
    "producer",
    "service",
    "domain",
    "kind",
)

# Bloodbank Event Naming Contract -- see bloodbank/docs/event-naming.md.
# The `vN.` segment is OPTIONAL here on purpose: this is the one transitional
# tolerance in the version-drop. Producers are being cut over one at a time, so
# for the duration of the flip this forwarder must accept both the old
# bloodbank.evt.v1.<domain>.<entity>.<action> and the new
# bloodbank.evt.<domain>.<entity>.<action>. Drop the optional group once no
# producer emits the versioned shape.
SUBJECT_PATTERN = re.compile(
    r"^bloodbank\.(?:evt|cmd|rpy)\.(?:v[1-9][0-9]*\.)?[a-z0-9]+(?:\.[a-z0-9_]+)+$"
)

# Prefixes the BLOODBANK_EVENTS stream binds (bloodbank.evt.> / cmd.> / rpy.>,
# from compose/nats/streams.json). A subject outside these is published to a
# topic no stream captures, so it is accepted by NATS and then silently dropped
# -- worth refusing rather than reporting a success that persists nothing.
STREAM_PREFIXES = ("bloodbank.evt.", "bloodbank.cmd.", "bloodbank.rpy.")


def fail(reason: str) -> int:
    print(f"forward_envelope: {reason}", file=sys.stderr)
    return 1


def subject_is_bound(subject: str) -> bool:
    return subject.startswith(STREAM_PREFIXES)


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return fail(f"envelope exceeds {MAX_INPUT_BYTES} bytes")
    if not raw.strip():
        return fail("empty stdin")

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        return fail(f"stdin is not valid JSON: {exc}")
    if not isinstance(envelope, dict):
        return fail("envelope must be a JSON object")

    subject = envelope.get("subject")
    if not isinstance(subject, str) or not subject:
        return fail("envelope has no subject")
    if not SUBJECT_PATTERN.match(subject):
        return fail(f"subject violates the naming contract: {subject}")
    if not subject_is_bound(subject):
        return fail(f"no stream binds subject {subject}; refusing to publish into the void")

    missing = [f for f in REQUIRED_FIELDS if not isinstance(envelope.get(f), str) or not envelope[f]]
    if missing:
        return fail(f"envelope missing required CloudEvents fields: {', '.join(missing)}")
    if not isinstance(envelope.get("data"), dict):
        return fail("envelope data must be an object")

    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    try:
        nats_publish(subject, body, client_name=CLIENT_NAME)
    except (OSError, RuntimeError) as exc:
        return fail(f"NATS publish failed for {subject}: {exc}")

    print(f"published {envelope['type']} -> {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
