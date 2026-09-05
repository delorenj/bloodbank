"""Stdlib-only NATS subscriber.

The sibling of `agent-hooks/core/nats_publish.py`, which justifies its own
hand-rolled client the same way: the NATS text protocol is small enough that a
socket and a parser beat adding nats-py, a virtualenv and an install step to a
host service.

Core NATS only -- no JetStream, matching what the publisher speaks. That means
at-most-once: a message published while this subscriber is reconnecting is gone
for good. The projector is built on the assumption that this WILL happen; see
`state.reconcile`.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Iterator

MAX_CONTROL_LINE = 16 * 1024
MAX_PAYLOAD = 1 * 1024 * 1024


class NatsSubscriber:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4222,
        subjects: tuple[str, ...] = (),
        client_name: str = "bloodbank-agent-state",
        connect_timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.subjects = subjects
        self.client_name = client_name
        self.connect_timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._buf = bytearray()

    # -- connection -----------------------------------------------------------
    def connect(self) -> None:
        self.close()
        sock = socket.create_connection((self.host, self.port), self.connect_timeout)
        # A read timeout is what lets the caller interleave reconciliation with
        # waiting for messages: recv gives up, we return, the loop does its
        # periodic work, and comes back. Without it a quiet bus would block
        # reconciliation forever -- and reconciliation is the half that heals.
        sock.settimeout(1.0)
        self._sock = sock
        self._buf = bytearray()

        line = self._read_line()
        if not line.startswith(b"INFO"):
            raise RuntimeError(f"NATS greeting was not INFO: {line[:64]!r}")
        opts = {
            "verbose": False,
            "pedantic": False,
            "tls_required": False,
            "name": self.client_name,
            "lang": "python-stdlib",
            "version": "1.0",
            "protocol": 1,
        }
        self._send(b"CONNECT " + json.dumps(opts).encode() + b"\r\n")
        for i, subject in enumerate(self.subjects, start=1):
            self._send(f"SUB {subject} {i}\r\n".encode())
        self._send(b"PING\r\n")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- wire -----------------------------------------------------------------
    def _send(self, data: bytes) -> None:
        if self._sock is None:
            raise RuntimeError("not connected")
        self._sock.sendall(data)

    def _read_line(self) -> bytes:
        while True:
            boundary = self._buf.find(b"\r\n")
            if boundary >= 0:
                line = bytes(self._buf[:boundary])
                del self._buf[: boundary + 2]
                return line
            if len(self._buf) > MAX_CONTROL_LINE:
                raise RuntimeError("NATS control line exceeds bounded input")
            self._fill()

    def _fill(self) -> None:
        if self._sock is None:
            raise RuntimeError("not connected")
        chunk = self._sock.recv(65536)
        if not chunk:
            raise RuntimeError("NATS connection closed")
        self._buf.extend(chunk)

    def _read_exact(self, size: int) -> bytes:
        while len(self._buf) < size:
            self._fill()
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    # -- messages -------------------------------------------------------------
    def messages(self) -> Iterator[tuple[str, bytes] | None]:
        """Yield (subject, payload), or None each time the read times out.

        The None is not noise -- it is the caller's cue to run periodic work. A
        generator that only yielded on traffic would stall reconciliation on a
        quiet bus, which is precisely when drift goes uncorrected.
        """
        while True:
            try:
                line = self._read_line()
            except socket.timeout:
                yield None
                continue

            if line.startswith(b"MSG "):
                parts = line.split()
                # MSG <subject> <sid> [reply-to] <#bytes>
                if len(parts) < 4:
                    continue
                subject = parts[1].decode("utf-8", "replace")
                try:
                    size = int(parts[-1])
                except ValueError:
                    continue
                if size < 0 or size > MAX_PAYLOAD:
                    raise RuntimeError(f"NATS payload out of bounds: {size}")
                payload = self._read_exact(size)
                self._read_exact(2)  # trailing CRLF
                yield subject, payload
            elif line.startswith(b"PING"):
                self._send(b"PONG\r\n")
            elif line.startswith(b"-ERR"):
                raise RuntimeError(f"NATS error: {line[:128]!r}")
            # +OK / PONG / anything else: nothing to do


def stream(
    host: str,
    port: int,
    subjects: tuple[str, ...],
    *,
    client_name: str = "bloodbank-agent-state",
    on_reconnect=None,
    backoff_max: float = 30.0,
) -> Iterator[tuple[str, bytes] | None]:
    """Messages across reconnects, forever. Never raises for a broker problem.

    A disconnect is expected operation, not an error: NATS restarts, the box
    sleeps. Yields None while disconnected too, so the caller keeps reconciling
    even when the bus is unreachable -- observation still works when events do
    not, which is the entire point of having two legs.
    """
    backoff = 0.5
    sub = NatsSubscriber(host, port, subjects, client_name)
    while True:
        try:
            sub.connect()
            backoff = 0.5
            if on_reconnect is not None:
                on_reconnect()
            for item in sub.messages():
                yield item
        except (OSError, RuntimeError):
            sub.close()
            # Yield once so the caller's periodic work still runs while the bus
            # is down, then wait.
            yield None
            time.sleep(backoff)
            backoff = min(backoff * 2, backoff_max)
