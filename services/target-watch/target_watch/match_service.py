"""`target-watch match`: inbound messaging/telephony events in, watch.target.detected out."""
from __future__ import annotations

from . import envelope as env
from .bus import Permanent
from .log import log
from .matcher import detection_envelope, epoch, match, text_hash
from .state import State
from .targets import TargetRegistry
from .xsender import XSenderIndex

SUBJECTS = ["bloodbank.evt.messaging.>", "bloodbank.evt.telephony.>"]
DURABLE = "target-watch-matcher"
NEAR_DUPLICATE_S = 2.0


class MatchService:
    def __init__(self, index: XSenderIndex, registry: TargetRegistry, state: State, publisher, *, validate=env.validate):
        self.index = index
        self.registry = registry
        self.state = state
        self.publisher = publisher
        self.validate = validate

    async def handle(self, event: dict) -> list[dict]:
        """Publish one detection per matching target. Raises to nak; Permanent to term."""
        ce_type = event.get("type")
        if ce_type in (env.DETECTED, env.ROUTED):
            return []
        xs = self.index.get(ce_type)
        if xs is None:
            return []
        try:
            detections = match(event, xs, self.registry.current())
        except (KeyError, TypeError, ValueError) as exc:
            raise Permanent(f"cannot read sender: {type(exc).__name__}") from exc
        published = []
        for det in detections:
            envelope = detection_envelope(det)
            try:
                self.validate(envelope)
            except Exception as exc:  # noqa: BLE001 - contract refusals are permanent for this input
                raise Permanent(f"detection refused by the contract: {exc}") from exc
            ex = det.extracted
            if self.state.near_duplicate(
                envelope["id"], det.target.id, ex.provider, text_hash(ex.text), epoch(ex.received_at), NEAR_DUPLICATE_S,
            ):
                log("match_near_duplicate", watch_id=det.target.id, provider=ex.provider, source_event_id=ex.source_event_id)
                continue
            await self.publisher.publish(envelope)
            log("match_detected", watch_id=det.target.id, provider=ex.provider, matched_on=det.matched_kind,
                detection_id=envelope["id"], source_event_id=ex.source_event_id)
            published.append(envelope)
        return published
