"""Shared orchestration for the canonical Bloodbank hook publisher.

This module is the single publish pipeline used by the canonical entrypoint
(``publish.py``) and the legacy per-client wrappers.  It:

1. Reads the hook payload (delegated to the client adapter).
2. Resolves the hook/event name (delegated to the adapter).
3. Looks up the event map (generated SSOT merged over adapter default).
4. Manages session state (reset, correlation/causation chain).
5. Shapes the event data (delegated to the adapter).
6. Builds the CloudEvents envelope (shared ``core.envelope``).
7. Publishes to NATS (shared ``core.nats_publish``).
8. Records the event and runs post-publish side effects (adapter).

Fail-open by default: errors are logged and the agent is never blocked unless
``BLOODBANK_HOOK_STRICT=1``.

Stdlib-only.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from core.envelope import build_envelope
from core.event_map import resolve_map
from core.nats_publish import publish as nats_publish
from core.session import SessionState

from clients.base import ClientAdapter


def run(adapter: ClientAdapter, argv: list[str]) -> int:
    """Execute the full publish pipeline for *adapter* with *argv*.

    Returns 0 on success or fail-open skip, 1 in strict mode on error,
    2 on usage error (no hook name).
    """
    payload = adapter.read_payload(argv)
    hook_name = adapter.resolve_hook_name(argv, payload)
    if not hook_name:
        print(
            f"usage: publish.py [--client <name>] <hook-or-event> [payload-json|end-reason]",
            file=sys.stderr,
        )
        return 2

    event_map = resolve_map(adapter.agent_dir, adapter.default_map)
    mapping = event_map.get(hook_name)
    if mapping is None:
        adapter.log(f"unsupported hook name (ignored): {hook_name}")
        return 1 if os.environ.get("BLOODBANK_HOOK_STRICT") == "1" else 0

    ce_type, bucket_prefix = mapping
    session = SessionState(path=adapter.session_file)

    if adapter.should_reset_session(ce_type, hook_name):
        session.reset()

    correlation_id = adapter.get_correlation_id(session, payload)
    causation_id = adapter.get_causation_id(
        session, ce_type, hook_name, correlation_id
    )
    event_id = adapter.get_event_id(session, ce_type, correlation_id)
    actor = adapter.get_actor(payload)

    try:
        data = adapter.shape_data(session, ce_type, hook_name, payload, argv)
        envelope = build_envelope(
            ce_type=ce_type,
            kind="event",
            source=adapter.source,
            producer=adapter.producer,
            service=adapter.service,
            actor=actor,
            data=data,
            correlation_id=correlation_id,
            causation_id=causation_id,
            ordering_key=f"{bucket_prefix}:{correlation_id}",
            event_id=event_id,
        )
    except Exception as exc:
        adapter.log(f"handler failed hook={hook_name} type={ce_type} err={exc!r}")
        return 1 if os.environ.get("BLOODBANK_HOOK_STRICT") == "1" else 0

    adapter.before_publish(session, ce_type, payload, argv)

    if os.environ.get("BLOODBANK_ENABLED", "true") != "true":
        adapter.after_publish_attempt(session, ce_type, payload, argv, published=False)
        return 0

    subject = envelope["subject"]
    body = json.dumps(envelope).encode("utf-8")
    try:
        nats_publish(subject, body, client_name=adapter.nats_client_name)
    except (OSError, RuntimeError) as exc:
        adapter.log(f"publish failed ({subject}): {exc}")
        adapter.after_publish_attempt(session, ce_type, payload, argv, published=False)
        return 1 if os.environ.get("BLOODBANK_HOOK_STRICT") == "1" else 0

    session.record_event(envelope["id"])
    adapter.post_publish(session, ce_type, payload, argv)
    adapter.after_publish_attempt(session, ce_type, payload, argv, published=True)
    adapter.log(f"published {subject}")
    return 0
