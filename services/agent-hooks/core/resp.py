"""Length-aware RESP2 client — stdlib only, no redis-py.

Supersedes the ad-hoc reader in ``redis_publish.py``, which read until the
buffer *ended with* CRLF. That is correct only for a single-line reply such as
``+OK``. Given ``$5\r\n`` arriving alone from ``recv()`` it returned early and
left ``hello\r\n`` in the socket, desynchronizing every subsequent command on
that connection — an intermittent, load-dependent failure that only shows up
once something issues more than one command per connection. The ASM issues
EVALSHA and reads a bulk string back, so the bug had to die first.

Scope is still deliberately small: connect, AUTH, SELECT, one command at a
time, full RESP2 reply parsing. Nothing here retries or pools; a hook is a
one-shot process and the caller owns the fail-open policy.
"""
from __future__ import annotations

import socket
from urllib.parse import urlparse


class RedisError(RuntimeError):
    """Redis replied with a RESP error (``-ERR ...``)."""


def encode(*parts: object) -> bytes:
    """Encode a RESP array of bulk strings."""
    out = [b"*%d\r\n" % len(parts)]
    for part in parts:
        raw = part if isinstance(part, bytes) else str(part).encode("utf-8")
        out.append(b"$%d\r\n" % len(raw))
        out.append(raw)
        out.append(b"\r\n")
    return b"".join(out)


class Connection:
    """One short-lived Redis connection with a correct reply parser."""

    def __init__(self, url: str, timeout: float = 3.0):
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 6379
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = b""
        try:
            if parsed.password:
                self.command("AUTH", parsed.password)
            db = parsed.path.lstrip("/") if parsed.path else ""
            if db and db != "0":
                self.command("SELECT", db)
        except BaseException:
            self.close()
            raise

    # -- wire ---------------------------------------------------------------

    def _fill(self) -> None:
        chunk = self._sock.recv(65536)
        if not chunk:
            raise ConnectionError("redis closed the connection")
        self._buf += chunk

    def _read_line(self) -> bytes:
        while b"\r\n" not in self._buf:
            self._fill()
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

    def _read_exactly(self, count: int) -> bytes:
        # count excludes the trailing CRLF, which we consume and discard.
        while len(self._buf) < count + 2:
            self._fill()
        data, self._buf = self._buf[:count], self._buf[count + 2 :]
        return data

    def _read_reply(self) -> object:
        line = self._read_line()
        kind, rest = line[:1], line[1:]
        if kind == b"+":
            return rest.decode("utf-8", "replace")
        if kind == b"-":
            raise RedisError(rest.decode("utf-8", "replace"))
        if kind == b":":
            return int(rest)
        if kind == b"$":
            length = int(rest)
            if length < 0:
                return None          # RESP2 null bulk string
            return self._read_exactly(length).decode("utf-8", "replace")
        if kind == b"*":
            count = int(rest)
            if count < 0:
                return None          # RESP2 null array
            return [self._read_reply() for _ in range(count)]
        raise RedisError(f"unparseable RESP reply: {line!r}")

    # -- api ----------------------------------------------------------------

    def command(self, *parts: object) -> object:
        self._sock.sendall(encode(*parts))
        return self._read_reply()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
