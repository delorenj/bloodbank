"""Match inbound events to watched targets through their schema's x-sender block."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from . import envelope as env
from .handles import KIND_PRIORITY, label_candidates, norm_e164, norm_email, norm_platform
from .paths import get_path
from .targets import Target
from .xsender import XSender

TEXT_LIMIT = 4000
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


@dataclass(frozen=True)
class Extracted:
    """The provider-neutral view of one inbound event."""
    source_type: str
    source_event_id: str
    correlationid: str | None
    provider: str
    handles: tuple[str, ...]
    names: tuple[str, ...]
    thread_id: str | None
    text: str
    subject: str | None
    received_at: str
    dedupe_key: str
    handle_kinds: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class Detection:
    target: Target
    extracted: Extracted
    matched_kind: str
    matched_value: str
    group_label: str | None

    @property
    def event_id(self) -> str:
        return env.detection_id(self.target.id, self.extracted.dedupe_key)

    @property
    def provider(self) -> str:
        return self.extracted.provider


def _strings(event: dict, paths) -> list[str]:
    out = []
    for path in paths:
        value = get_path(event, path)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return out


def provider_for(source_type: str, data: dict) -> str:
    """The source type's entity, except that telephony names the call, not the pipeline stage.

    call.ended, call.recorded and transcription.completed of one voicemail share
    the provider 'voicemail' (and the call id as thread), so they coalesce into
    one ticket instead of three; every other telephony event is a 'call'.
    """
    _, domain, entity, _ = source_type.split(".")
    if domain == "telephony":
        if data.get("kind") == "voicemail" or data.get("disposition") == "voicemail":
            return "voicemail"
        return "call"
    return entity


def describe(source_type: str, data: dict) -> str:
    """A one-line stand-in for a message with no text (a call, a recording)."""
    _, _, entity, action = source_type.split(".")
    detail = next((data[k] for k in ("disposition", "kind") if isinstance(data.get(k), str) and data[k]), None)
    return f"{entity} {action}" + (f" ({detail})" if detail else "")


def _received_at(event: dict, xs: XSender) -> str:
    for candidate in (get_path(event, xs.time_path), event.get("time")):
        if isinstance(candidate, str) and _RFC3339.match(candidate):
            try:
                datetime.fromisoformat(candidate.replace("Z", "+00:00"))
                return candidate
            except ValueError:
                pass
    return env.rfc3339()


def epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).timestamp()
    except ValueError:
        return datetime.now(UTC).timestamp()


def text_hash(text: str) -> str | None:
    folded = " ".join(text.casefold().split())
    return hashlib.sha256(folded.encode()).hexdigest() if folded else None


def extract(event: dict, xs: XSender) -> Extracted | None:
    """None when the event is not an inbound message or names no sender."""
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    if xs.inbound_path is not None:
        direction = get_path(event, xs.inbound_path)
        if direction not in xs.inbound_equals:
            return None
    handles = tuple(_strings(event, xs.handle_paths))
    if not handles:
        return None
    source_type = event["type"]
    source_event_id = str(event.get("id") or "")
    texts = _strings(event, xs.text_paths)
    text = texts[0] if texts else ""
    subject_values = _strings(event, [xs.subject_path]) if xs.subject_path else []
    thread = get_path(event, xs.thread_path)
    message_id = data.get("message_id")
    dedupe = f"{source_type}:{message_id}" if isinstance(message_id, str) and message_id else f"{source_type}:{source_event_id}"
    return Extracted(
        source_type=source_type,
        source_event_id=source_event_id,
        correlationid=event.get("correlationid"),
        provider=provider_for(source_type, data),
        handles=handles,
        names=tuple(_strings(event, xs.name_paths)),
        thread_id=str(thread) if isinstance(thread, (str, int)) and str(thread) else None,
        text=text[:TEXT_LIMIT],
        subject=subject_values[0] if subject_values else None,
        received_at=_received_at(event, xs),
        dedupe_key=dedupe,
        handle_kinds=xs.handle_kinds,
        description=describe(source_type, data),
    )


def candidates(ex: Extracted) -> list[tuple[str, str, str | None]]:
    """(kind, normalised value, group label) for every way the sender may be named.

    Display names only count when the type declares 'label' handles: a carrier
    text's name comes from the operator's own contact book; an email display
    name is whatever the sender typed, so it never identifies anyone.
    """
    out: list[tuple[str, str, str | None]] = []
    for raw in ex.handles:
        for kind in ex.handle_kinds:
            if kind == "email" and (v := norm_email(raw)):
                out.append(("email", v, None))
            elif kind == "e164" and (v := norm_e164(raw)):
                out.append(("e164", v, None))
            elif kind == "platform_user" and (v := norm_platform(raw, ex.provider)):
                out.append(("platform_user", v, None))
            elif kind == "label":
                out.extend(("label", label, group) for label, group in label_candidates(raw))
    if "label" in ex.handle_kinds:
        for raw in ex.names:
            out.extend(("label", label, group) for label, group in label_candidates(raw))
    return out


def match_extracted(ex: Extracted, targets: list[Target]) -> list[Detection]:
    found = candidates(ex)
    detections = []
    for target in targets:
        hits = [(kind, value, group) for kind, value, group in found if value in target.handles(kind)]
        if not hits:
            continue
        hits.sort(key=lambda h: KIND_PRIORITY[h[0]])
        kind, value, _ = hits[0]
        group = next((g for _, _, g in hits if g), None)
        detections.append(Detection(target=target, extracted=ex, matched_kind=kind, matched_value=value, group_label=group))
    return detections


def match(event: dict, xs: XSender, targets: list[Target]) -> list[Detection]:
    ex = extract(event, xs)
    return match_extracted(ex, targets) if ex else []


def detection_data(det: Detection) -> dict:
    ex = det.extracted
    return {
        "schema_version": 1,
        "watch_id": det.target.id,
        "target_display_name": det.target.display_name,
        "provider": ex.provider,
        "source_type": ex.source_type,
        "source_event_id": ex.source_event_id,
        "matched_on": {"kind": det.matched_kind, "value": det.matched_value},
        "sender": {"handle": ex.handles[0], "display_name": ex.names[0] if ex.names else None},
        "conversation": {"thread_id": ex.thread_id, "group_label": det.group_label},
        "content": {"text": ex.text, "subject": ex.subject},
        "received_at": ex.received_at,
        "dedupe_key": ex.dedupe_key,
    }


def detection_envelope(det: Detection, now: datetime | None = None) -> dict:
    ex = det.extracted
    return env.build(
        env.DETECTED,
        detection_data(det),
        event_id=det.event_id,
        ordering_key=f"watch:{det.target.id}",
        correlationid=ex.correlationid if env.is_uuid(ex.correlationid) else ex.source_event_id,
        causationid=ex.source_event_id,
        now=now,
    )
