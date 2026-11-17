"""NATS JetStream plumbing: durable pull consumers, PubAck-checked publishing, one-message handling."""
from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import datetime

from .log import log

STREAM = "BLOODBANK_EVENTS"
DEFAULT_NATS_URL = "nats://127.0.0.1:4222"


class Permanent(Exception):
    """This message can never succeed; terminate it instead of redelivering."""


class Publisher:
    def __init__(self, js):
        self.js = js

    async def publish(self, envelope: dict):
        from .envelope import subject_for

        body = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        receipt = await self.js.publish(
            subject_for(envelope["type"]), body,
            headers={"Nats-Msg-Id": envelope["id"]}, stream=STREAM, timeout=10,
        )
        if getattr(receipt, "stream", STREAM) != STREAM:
            raise RuntimeError(f"PubAck from unexpected stream {receipt.stream!r}")
        return receipt


class DryRunPublisher:
    def __init__(self, out=None):
        self.out = out
        self.published: list[dict] = []

    async def publish(self, envelope: dict):
        self.published.append(envelope)
        print(json.dumps({"dry_run_publish": envelope}, sort_keys=True), file=self.out, flush=True)
        return None


async def connect(name: str, url: str | None = None):
    import nats

    return await nats.connect(
        url or os.environ.get("NATS_URL", DEFAULT_NATS_URL), name=name,
        max_reconnect_attempts=-1, reconnect_time_wait=2,
    )


def consumer_config(durable: str | None, subjects: list[str], *, since: datetime | None, ack_wait: float, max_deliver: int):
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    kwargs = {"filter_subject": subjects[0]} if len(subjects) == 1 else {"filter_subjects": list(subjects)}
    if since is not None:
        kwargs["deliver_policy"] = DeliverPolicy.BY_START_TIME
        kwargs["opt_start_time"] = since.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    else:
        # A new durable starts at NEW: never file tickets for a week of retained history.
        kwargs["deliver_policy"] = DeliverPolicy.NEW
    return ConsumerConfig(
        durable_name=durable,
        ack_policy=AckPolicy.EXPLICIT,
        ack_wait=ack_wait,
        max_deliver=max_deliver,
        max_ack_pending=16,
        **kwargs,
    )


async def subscribe(js, durable: str | None, subjects: list[str], *, since=None, ack_wait=60.0, max_deliver=10):
    """Bind a durable pull consumer (created on first use); durable=None makes an ephemeral one."""
    config = consumer_config(durable, subjects, since=since, ack_wait=ack_wait, max_deliver=max_deliver)
    return await js.pull_subscribe(subjects[0], durable=durable, stream=STREAM, config=config)


def deliveries(message) -> int:
    try:
        return int(message.metadata.num_delivered)
    except Exception:  # noqa: BLE001 - metadata absent on fakes or plain messages
        return 1


async def handle_message(message, handler, *, name: str, max_attempts: int = 5, nak_delay: float = 30.0) -> str:
    """Run handler(event) for one message; ack on success, nak with backoff, term when hopeless.

    Returns the disposition ('ack', 'nak', 'term') for logging and tests.
    """
    try:
        event = json.loads(message.data)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise Permanent("not a CloudEvents object")
    except (ValueError, Permanent) as exc:
        log(f"{name}_bad_message", error=type(exc).__name__)
        await message.term()
        return "term"
    try:
        await handler(event)
    except Permanent as exc:
        log(f"{name}_terminated", event_id=event.get("id"), type=event.get("type"), error=str(exc)[:300])
        await message.term()
        return "term"
    except Exception as exc:  # noqa: BLE001 - one bad event never stops the loop
        attempt = deliveries(message)
        if attempt >= max_attempts:
            log(f"{name}_gave_up", event_id=event.get("id"), type=event.get("type"), attempts=attempt,
                error=f"{type(exc).__name__}: {str(exc)[:300]}")
            await message.term()
            return "term"
        log(f"{name}_retry", event_id=event.get("id"), type=event.get("type"), attempt=attempt,
            error=f"{type(exc).__name__}: {str(exc)[:300]}")
        await message.nak(delay=nak_delay * attempt)
        return "nak"
    await message.ack()
    return "ack"


async def consume(sub, handler, *, name: str, stop: asyncio.Event, once: bool = False, **kwargs) -> int:
    """Fetch one message at a time until stopped. once=True returns when the consumer is drained."""
    import nats.errors

    handled = 0
    while not stop.is_set():
        try:
            messages = await sub.fetch(1, timeout=5)
        except (asyncio.TimeoutError, nats.errors.TimeoutError):
            if once:
                return handled
            continue
        for message in messages:
            await handle_message(message, handler, name=name, **kwargs)
            handled += 1
    return handled


def install_stop(loop: asyncio.AbstractEventLoop) -> asyncio.Event:
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    return stop
