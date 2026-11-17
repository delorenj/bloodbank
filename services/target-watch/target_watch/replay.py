"""`target-watch replay`: what `match` would have detected, from Candystore's projection. Publishes nothing."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from .matcher import epoch, match, text_hash
from .state import State
from .targets import Target
from .xsender import XSenderIndex

DEFAULT_CANDYSTORE = "http://127.0.0.1:8683"
DOMAINS = ("messaging", "telephony")
PAGE = 500
MAX_EVENTS = 50000


def fetch_events(since: str, *, base_url: str | None = None, domains=DOMAINS, opener=None) -> list[dict]:
    """Every event of the given domains from `since` on, oldest first."""
    base = (base_url or os.environ.get("CANDYSTORE_URL", DEFAULT_CANDYSTORE)).rstrip("/")
    open_url = opener or (lambda url: urllib.request.urlopen(url, timeout=30))
    seen: dict[str, dict] = {}
    for domain in domains:
        offset = 0
        while offset < MAX_EVENTS:
            query = urllib.parse.urlencode({"domain": domain, "from": since, "limit": PAGE, "offset": offset})
            with open_url(f"{base}/events?{query}") as response:
                page = json.load(response)
            events = page.get("events") if isinstance(page, dict) else page
            events = [e for e in (events or []) if isinstance(e, dict) and e.get("id")]
            for event in events:
                seen.setdefault(event["id"], event)
            if len(events) < PAGE:
                break
            offset += PAGE
    return sorted(seen.values(), key=lambda e: str(e.get("time") or ""))


def replay(events: list[dict], index: XSenderIndex, targets: list[Target], *, target_id: str | None = None) -> list[dict]:
    """Detections `match` would publish (near-duplicates folded the same way), as rows."""
    chosen = [t for t in targets if target_id is None or t.id == target_id]
    state = State(":memory:")
    rows = []
    for event in events:
        xs = index.get(event.get("type", ""))
        if xs is None:
            continue
        for det in match(event, xs, chosen):
            ex = det.extracted
            duplicate = state.near_duplicate(det.event_id, det.target.id, ex.provider, text_hash(ex.text), epoch(ex.received_at))
            rows.append({
                "received_at": ex.received_at,
                "watch_id": det.target.id,
                "provider": ex.provider,
                "source_type": ex.source_type,
                "matched_on": det.matched_kind,
                "group_label": det.group_label,
                "detection_id": det.event_id,
                "source_event_id": ex.source_event_id,
                "near_duplicate": duplicate,
                "text": ex.text,
            })
    return rows
