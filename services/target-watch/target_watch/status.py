"""`target-watch status`: what the sink did about the last few detections, from its sqlite ledger.

journald keeps almost nothing for the sink, so the question "did it reach him" is answered from the
`routes` ledger and the `notifications` log. Channels are shown masked (a ring number or SIP user is
personal data); times are America/New_York, where the operator lives.

A detection routed `awaiting_button` (a target with `ticket = "button"`) shows what became of its Create Ticket
button: not tapped yet, or the ticket the tap filed, or why it did not.
"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .sink import mask_channel

TZ = ZoneInfo("America/New_York")


def _stamp(epoch: float, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    return datetime.fromtimestamp(epoch, UTC).astimezone(TZ).strftime(fmt)


def _provider(conv_key: str) -> str:
    return conv_key.split(":", 1)[0] or "?"


def _button_line(button: dict) -> str:
    state = button["state"]
    if state == "done":
        return f"filed: {button['action']} {button['ticket_ref']}"
    return {"awaiting": "not tapped yet", "pending": "tapped; px started and has no outcome yet",
            "failed": "tapped; no ticket was filed (see the button row)"}.get(state, state)


def render(routes: list[dict]) -> str:
    """Newest detection first; under each, its channels in the order they were tried."""
    if not routes:
        return "no detections in the sink ledger"
    lines = []
    for route in routes:
        button = route.get("button")
        ticket = route["ticket_ref"] or (button or {}).get("ticket_ref") or "-"
        action = route["action"] or "pending"
        lines.append(f"{_stamp(route['received_epoch'])}  {route['watch_id']}  {_provider(route['conv_key']):<10} "
                     f"{ticket:<10} {action}")
        if route["reason"]:
            lines.append(f"    reason: {route['reason']}")
        if button:
            lines.append(f"    Create Ticket button: {_button_line(button)}")
        notes = route.get("notifications") or []
        if not notes:
            sent = ", ".join(mask_channel(c) for c in route["notify_channels"]) or "none"
            lines.append(f"    (no per-channel log for this detection; channels it was sent on: {sent})")
        for note in notes:
            label = note["channel"] + (f" (retry {note['attempt'] - 1})" if note["attempt"] > 1 else "")
            detail = f"  {note['detail']}" if note["detail"] else ""
            if note["ref"] and (note["channel"] == "button" or note["channel"].startswith("earshot:")):
                detail += f"  [{note['ref']}]"  # the ticket the tap filed, or the earshot object key
            lines.append(f"    {_stamp(note['at'], '%H:%M:%S')}  {label:<34} {note['outcome']}{detail}")
    return "\n".join(lines)
