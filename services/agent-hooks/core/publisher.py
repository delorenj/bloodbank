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

from core.envelope import build_envelope, now_iso
from core.event_map import resolve_alert_map, resolve_map
from core.nats_publish import publish as nats_publish
from core.session import SessionState

from clients.base import MAX_DIAGNOSTIC_BYTES, ClientAdapter, bounded_scalar

DECKARD_ATTENTION_SUBJECT = "deckard.evt.attention"
DECKARD_ATTENTION_TYPE = "deckard.v1.agent.attention"
# Leave headroom beneath every supported hook runner's three-second ceiling.
DECKARD_PUBLISH_TIMEOUT = 0.75
MAX_ATTENTION_ENVELOPE_BYTES = 64 * 1024
MAX_ATTENTION_SESSION_BYTES = 1024


def _bounded_actor(actor: Any) -> dict[str, Any]:
    if not isinstance(actor, dict):
        raise ValueError("attention actor must be an object")
    out: dict[str, Any] = {}
    for key, value in list(actor.items())[:16]:
        if not isinstance(key, str):
            continue
        bounded = bounded_scalar(value)
        if bounded is not None:
            out[key[:MAX_DIAGNOSTIC_BYTES]] = bounded
    return out


def zellij_origin() -> dict[str, Any]:
    """Which zellij pane produced this event, when there is one.

    Attribution on the agent stream was `working_directory` + `actor.cli` only,
    which cannot separate two tabs sitting in the same repo -- the normal case
    on this machine. The dedicated Deckard attention envelope already carried an
    exact pane id; every OTHER lifecycle event (session.started, tool.requested,
    ...) did not, so nothing downstream could build a per-tab state machine.

    Injected here, at the one seam between `shape_data` and `build_envelope`,
    rather than in any adapter: five clients override `shape_data`, so patching
    the base class would miss claude and codex. And deliberately NOT in
    `core/envelope.py`, which is the generic CloudEvents builder shared with
    producers that have no terminal at all.

    Absent outside zellij, and silently absent on a malformed pane id -- a
    telemetry field must never be able to fail a hook.
    """
    pane = os.environ.get("ZELLIJ_PANE_ID", "")
    session_name = os.environ.get("ZELLIJ_SESSION_NAME", "")
    if not pane or not session_name:
        return {}
    try:
        pane_number = int(pane)
    except (TypeError, ValueError):
        return {}
    if pane_number < 0 or pane_number > 0xFFFF_FFFF:
        return {}
    if len(session_name.encode("utf-8")) > MAX_ATTENTION_SESSION_BYTES:
        return {}
    return {
        "zellij_pane_id": pane_number,
        "zellij_session_name": session_name,
    }


def build_attention_envelope(
    adapter: ClientAdapter, hook_name: str, payload: Any
) -> dict[str, Any]:
    pane = os.environ.get("ZELLIJ_PANE_ID", "")
    session = os.environ.get("ZELLIJ_SESSION_NAME", "")
    pane_number = int(pane)
    if (
        not session
        or len(session.encode("utf-8")) > MAX_ATTENTION_SESSION_BYTES
        or pane_number < 0
        or pane_number > 0xFFFF_FFFF
    ):
        raise ValueError("missing/oversized session or pane outside u32")
    envelope = {
        "specversion": "1.0",
        "id": adapter.attention_event_id(payload),
        "source": adapter.source,
        "type": DECKARD_ATTENTION_TYPE,
        "subject": DECKARD_ATTENTION_SUBJECT,
        "time": now_iso(),
        "datacontenttype": "application/json",
        "actor": _bounded_actor(adapter.get_actor(payload)),
        "data": adapter.shape_attention_alert(hook_name, payload),
    }
    return envelope


def serialize_attention_envelope(envelope: dict[str, Any]) -> bytes:
    body = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_ATTENTION_ENVELOPE_BYTES:
        raise ValueError(
            f"attention envelope exceeds {MAX_ATTENTION_ENVELOPE_BYTES} bytes"
        )
    return body


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

    alert_kind = resolve_alert_map(adapter.agent_dir).get(hook_name)
    if alert_kind is not None:
        return _fanout_alert(adapter, hook_name, alert_kind, payload)

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
        # Stamp the originating pane so consumers can attribute per TAB, not
        # merely per working directory. Never overwrites what an adapter set.
        if isinstance(data, dict):
            for key, value in zellij_origin().items():
                data.setdefault(key, value)
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
    except (OSError, RuntimeError, ValueError) as exc:
        adapter.log(f"publish failed ({subject}): {exc}")
        adapter.after_publish_attempt(session, ce_type, payload, argv, published=False)
        return 1 if os.environ.get("BLOODBANK_HOOK_STRICT") == "1" else 0

    session.record_event(envelope["id"])
    adapter.post_publish(session, ce_type, payload, argv)
    adapter.after_publish_attempt(session, ce_type, payload, argv, published=True)
    adapter.log(f"published {subject}")
    return 0


def _fanout_alert(
    adapter: ClientAdapter, hook_name: str, alert_kind: str, payload: Any
) -> int:
    """Publish one normalized Deckard alert, always bounded and fail-open."""
    if alert_kind != "attention":
        adapter.log(f"unsupported alert kind (ignored): {alert_kind}")
        return 0
    if os.environ.get("BLOODBANK_ENABLED", "true") != "true":
        return 0

    try:
        envelope = build_attention_envelope(adapter, hook_name, payload)
        body = serialize_attention_envelope(envelope)
        nats_publish(
            DECKARD_ATTENTION_SUBJECT,
            body,
            client_name=f"{adapter.nats_client_name}-deckard",
            timeout=DECKARD_PUBLISH_TIMEOUT,
        )
    except Exception as exc:  # Hooks must never block or fail the agent.
        adapter.log(f"attention alert refused/failed hook={hook_name}: {exc}")
        return 0

    adapter.log(f"published {DECKARD_ATTENTION_SUBJECT}")
    return 0
