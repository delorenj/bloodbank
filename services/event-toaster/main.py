"""bloodbank event-toaster.

Subscribes to *every* `bloodbank.evt.v1.>` subject on the bloodbank NATS bus
and forwards each envelope as a desktop notification via ntfy.delo.sh.

Why direct NATS (no Dapr): the toaster's job is wildcard pass-through.
Dapr's pub/sub model requires per-topic subscriptions, which makes wildcard
fan-in clumsy. NATS core subscribe on `bloodbank.evt.v1.>` is one line and
gets everything that flows through the broker, with auto-reconnect from
nats-py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Any

import httpx
import nats
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("event-toaster")

NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
SUBJECT = os.environ.get("SUBJECT_FILTER", "bloodbank.evt.v1.>")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.delo.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "bloodbank")
NTFY_PRIORITY = os.environ.get("NTFY_PRIORITY", "5")  # 1=min, 5=max (loud)
NTFY_TAGS = os.environ.get("NTFY_TAGS", "drop_of_blood,zap")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
MAX_BODY_CHARS = int(os.environ.get("MAX_BODY_CHARS", "400"))


def format_toast(envelope: dict[str, Any], raw_subject: str) -> tuple[str, str, dict[str, str]]:
    """Return (title, body, headers) for one envelope."""
    event_type = envelope.get("type") or raw_subject or "unknown"
    source = envelope.get("source") or "unknown"
    data = envelope.get("data") or {}

    # Title: ASCII only (HTTP headers can't carry raw UTF-8 in httpx; emoji
    # comes from the Tags header instead, rendered client-side by ntfy).
    title = event_type

    # Body: try to surface the most useful 1-2 fields from `data`, fall back
    # to a truncated JSON dump.
    line: str
    if isinstance(data, dict):
        for key in ("tool_name", "command", "prompt", "summary", "message"):
            if key in data and data[key]:
                line = f"{key}: {str(data[key])[:200]}"
                break
        else:
            line = json.dumps(data, default=str)[:MAX_BODY_CHARS]
    else:
        line = str(data)[:MAX_BODY_CHARS]

    body = f"src: {source}\n{line}"

    headers = {
        "Title": title,
        "Priority": NTFY_PRIORITY,
        "Tags": NTFY_TAGS,
    }
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"

    return title, body, headers


async def publish_toast(
    client: httpx.AsyncClient, title: str, body: str, headers: dict[str, str]
) -> None:
    url = f"{NTFY_URL}/{NTFY_TOPIC}"
    try:
        resp = await client.post(url, headers=headers, content=body.encode("utf-8"))
        if resp.status_code >= 300:
            log.warning("ntfy non-2xx", extra={"status": resp.status_code, "body": resp.text[:200]})
        else:
            log.info("toasted: %s", title)
    except httpx.HTTPError as exc:
        log.error("ntfy http error: %s", exc)


async def on_message(client: httpx.AsyncClient, msg: Msg) -> None:
    try:
        envelope = json.loads(msg.data.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        log.warning("bad json on %s: %s", msg.subject, exc)
        return
    title, body, headers = format_toast(envelope, msg.subject)
    await publish_toast(client, title, body, headers)


async def run() -> None:
    log.info(
        "event-toaster starting",
        extra={"nats": NATS_URL, "subject": SUBJECT, "ntfy_topic": NTFY_TOPIC},
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with httpx.AsyncClient(timeout=5.0) as http_client:
        nc: NatsClient = await nats.connect(
            NATS_URL,
            name="bloodbank-event-toaster",
            max_reconnect_attempts=-1,  # forever
            reconnect_time_wait=2,
        )
        log.info("nats connected to %s", NATS_URL)

        async def cb(msg: Msg) -> None:
            await on_message(http_client, msg)

        sub = await nc.subscribe(SUBJECT, cb=cb)
        log.info("subscribed: %s", SUBJECT)

        try:
            await stop.wait()
        finally:
            log.info("draining subscription")
            await sub.unsubscribe()
            await nc.drain()
            log.info("done")


if __name__ == "__main__":
    asyncio.run(run())
