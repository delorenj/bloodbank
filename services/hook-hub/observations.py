"""Retry durable hook facts with a JetStream storage acknowledgement.

This worker is separate from native hook execution. A PONG is transport
progress, never evidence that JetStream stored an event. Stable envelope IDs
also travel as Nats-Msg-Id; consumers still deduplicate beyond the broker's
finite duplicate window.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Callable

from facts import MAX_FACT_BYTES
from core.nats_publish import _config, _connect, _read_line, _remaining, _send


def publish(fact: dict, body: str, *, timeout: float = 1.0) -> dict:
    host, port, _ = _config()
    deadline = time.monotonic() + timeout
    payload = body.encode()
    if len(payload) > MAX_FACT_BYTES:
        raise ValueError("hook_observation_exceeds_transport_limit")
    inbox = "_INBOX.hook_observation." + uuid.uuid4().hex
    headers = (f"NATS/1.0\r\nNats-Msg-Id: {fact['id']}\r\n"
               "Nats-Expected-Stream: BLOODBANK_EVENTS\r\n\r\n").encode()
    with _connect(host, port, deadline) as sock:
        line, buffered = _read_line(sock, bytearray(), deadline)
        if not line.startswith(b"INFO "):
            raise RuntimeError("invalid_nats_greeting")
        info = json.loads(line[5:])
        if len(payload) + len(headers) > info.get("max_payload", MAX_FACT_BYTES + 1024):
            raise ValueError("hook_observation_exceeds_broker_limit")
        options = {"verbose": False, "pedantic": False, "name": "hook-hub-observations",
                   "lang": "python-stdlib", "version": "1", "protocol": 1,
                   "headers": True, "no_responders": True}
        frame = (b"CONNECT " + json.dumps(options).encode() + b"\r\n"
                 + f"SUB {inbox} 1\r\nUNSUB 1 1\r\n".encode()
                 + f"HPUB {fact['subject']} {inbox} {len(headers)} {len(headers) + len(payload)}\r\n".encode()
                 + headers + payload + b"\r\n")
        _send(sock, frame, deadline)
        while True:
            line, buffered = _read_line(sock, buffered, deadline)
            if line == b"PING":
                _send(sock, b"PONG\r\n", deadline)
            elif line.startswith(b"-ERR") or line.startswith(b"HMSG"):
                raise RuntimeError("jetstream_publish_rejected")
            elif line.startswith(b"MSG "):
                parts = line.split()
                size = int(parts[-1])
                if len(parts) not in {4, 5} or parts[1].decode() != inbox or parts[2] != b"1" or not 0 < size <= 16384:
                    raise RuntimeError("invalid_jetstream_ack")
                while len(buffered) < size + 2:
                    sock.settimeout(_remaining(deadline))
                    chunk = sock.recv(min(16384, size + 2 - len(buffered)))
                    if not chunk:
                        raise RuntimeError("jetstream_ack_incomplete")
                    buffered.extend(chunk)
                if buffered[size:size + 2] != b"\r\n":
                    raise RuntimeError("invalid_jetstream_ack")
                ack = json.loads(bytes(buffered[:size]))
                if ack.get("error") or ack.get("stream") != "BLOODBANK_EVENTS" or not isinstance(ack.get("seq"), int) or ack["seq"] < 1:
                    raise RuntimeError("jetstream_ack_failed")
                return ack
            # In particular: ignore PONG. Only the correlated PubAck finishes.


class OutboxWorker:
    def __init__(self, store, *, publisher: Callable = publish, log: Callable = lambda _: None) -> None:
        self.store = store
        self.publisher = publisher
        self.log = log
        self.wake = asyncio.Event()
        # Backfill is a one-time migration: every live mutation writes its own
        # revision marker, so once a pass finds nothing it never will again in
        # this process. Its anti-join scans every invocation under the write
        # lock (~0.3s at 100k rows on a loaded host); repeating it on every
        # wake held the lock long enough to time out native hook writes.
        self.backfill_complete = False

    async def flush(self, limit: int = 20) -> bool:
        """One bounded batch of due rows; stop at the first failure to preserve order.

        Rows still inside their debounce window are not due: the store will
        either supersede them with a newer revision or release them later.
        """
        rows = await asyncio.to_thread(self.store.due_observations, limit)
        for row in rows:
            try:
                fact = json.loads(row["envelope"])
                await asyncio.to_thread(self.publisher, fact, row["envelope"])
            except Exception as exc:
                reason = type(exc).__name__
                await asyncio.to_thread(self.store.observation_failed, row["sequence"], reason)
                self.log(f"hook observation pending: {reason}")
                return False
            await asyncio.to_thread(self.store.observation_sent, row["sequence"])
        return True

    async def run(self) -> None:
        delay = 0.1
        while True:
            try:
                self.wake.clear()
                status = await asyncio.to_thread(self.store.observation_status)
                # Do not flood disk with a whole historical migration in one
                # transaction or starve live facts behind a large backfill.
                if not self.backfill_complete and status["pending"] < 100:
                    if not await asyncio.to_thread(self.store.backfill, 50):
                        self.backfill_complete = True
                if await self.flush():
                    delay = 0.1
                    if await asyncio.to_thread(self.store.due_observations, 1):
                        await asyncio.sleep(0)
                        continue
                else:
                    # Native hook traffic must not defeat outage backoff.
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"hook observation worker retry: {type(exc).__name__}")
                await asyncio.sleep(min(delay, 30))
                delay = min(delay * 2, 30)
            # Sleep until woken by a journal write or the next debounced row
            # comes due, whichever is first (and at least once a second, which
            # also paces backfill). Every hook write wakes this loop; a row that
            # is not yet due is left for its own deadline, not busy-polled.
            timeout = 1.0
            try:
                due_in = await asyncio.to_thread(self.store.next_due_in)
                if due_in is not None:
                    timeout = min(timeout, max(due_in, 0.02))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
