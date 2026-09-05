"""Stdlib-only Redis client, just the five commands this service needs.

Same justification as the hand-rolled NATS clients next door: RESP is a trivial
protocol, and a host service that needs SET/DEL/PUBLISH/SCAN should not drag in
redis-py plus a virtualenv. Reconnects on its own, because a Redis restart is
ordinary and must not take the projector down with it.
"""

from __future__ import annotations

import socket
from typing import Any

CRLF = b"\r\n"


class RespError(RuntimeError):
    pass


def _encode(*args: str) -> bytes:
    out = bytearray(b"*%d\r\n" % len(args))
    for arg in args:
        raw = arg.encode("utf-8")
        out += b"$%d\r\n" % len(raw) + raw + CRLF
    return bytes(out)


class Redis:
    def __init__(self, host: str = "127.0.0.1", port: int = 6379, timeout: float = 2.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = bytearray()

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        sock = socket.create_connection((self.host, self.port), self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        self._buf = bytearray()
        return sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._buf = bytearray()

    # -- wire -----------------------------------------------------------------
    def _read_line(self) -> bytes:
        while True:
            boundary = self._buf.find(CRLF)
            if boundary >= 0:
                line = bytes(self._buf[:boundary])
                del self._buf[: boundary + 2]
                return line
            sock = self._connect()
            chunk = sock.recv(65536)
            if not chunk:
                raise RespError("redis closed the connection")
            self._buf.extend(chunk)

    def _read_exact(self, size: int) -> bytes:
        while len(self._buf) < size:
            sock = self._connect()
            chunk = sock.recv(65536)
            if not chunk:
                raise RespError("redis closed the connection")
            self._buf.extend(chunk)
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def _read_reply(self) -> Any:
        line = self._read_line()
        if not line:
            raise RespError("empty redis reply")
        kind, rest = line[:1], line[1:]
        if kind in (b"+", b":"):
            return rest.decode("utf-8", "replace")
        if kind == b"-":
            raise RespError(rest.decode("utf-8", "replace"))
        if kind == b"$":
            length = int(rest)
            if length == -1:
                return None
            data = self._read_exact(length)
            self._read_exact(2)
            return data.decode("utf-8", "replace")
        if kind == b"*":
            count = int(rest)
            if count == -1:
                return None
            return [self._read_reply() for _ in range(count)]
        raise RespError(f"unknown RESP prefix {kind!r}")

    def command(self, *args: str) -> Any:
        """Run one command, retrying once through a fresh connection.

        One retry, not a loop: a dropped socket deserves a reconnect, a Redis
        that is actually down deserves to surface as an error rather than a
        silent stall inside the render path.
        """
        for attempt in (1, 2):
            try:
                sock = self._connect()
                sock.sendall(_encode(*args))
                return self._read_reply()
            except (OSError, RespError):
                self.close()
                if attempt == 2:
                    raise
        raise RespError("unreachable")

    # -- the five --------------------------------------------------------------
    def set_ex(self, key: str, value: str, ttl_seconds: int) -> None:
        self.command("SET", key, value, "EX", str(ttl_seconds))

    def delete(self, key: str) -> None:
        self.command("DEL", key)

    def publish(self, channel: str, message: str) -> None:
        self.command("PUBLISH", channel, message)

    def get(self, key: str) -> str | None:
        return self.command("GET", key)

    def scan(self, match: str) -> list[str]:
        """Full SCAN, never KEYS -- KEYS blocks the server for everyone."""
        cursor = "0"
        found: list[str] = []
        while True:
            reply = self.command("SCAN", cursor, "MATCH", match, "COUNT", "256")
            if not isinstance(reply, list) or len(reply) != 2:
                break
            cursor, batch = reply[0], reply[1]
            if isinstance(batch, list):
                found.extend(x for x in batch if isinstance(x, str))
            if cursor == "0":
                break
        return found
