"""Minimal stdlib Redis writer (RESP over a socket) — no redis-py dependency.

Mirrors the philosophy of core/nats_publish.py: a tiny, dependency-free client
sufficient for fire-and-forget writes from a hook/healthcheck context. Used to
publish the agent-hook-tests health snapshot to the Redis key Holocene reads
(holocene:tooling:stat:agent-hook-tests).

Only what we need: connect, optional AUTH, `SET key value EX ttl`.
"""
from __future__ import annotations

import os
import socket
from urllib.parse import urlparse


def _redis_url() -> str:
    return (
        os.environ.get("TOOLING_REDIS_URL")
        or os.environ.get("REDIS_URL")
        or "redis://127.0.0.1:6379"
    )


def _encode(*parts: str) -> bytes:
    """Encode a RESP array of bulk strings."""
    out = [f"*{len(parts)}\r\n".encode()]
    for p in parts:
        b = p.encode("utf-8")
        out.append(f"${len(b)}\r\n".encode())
        out.append(b)
        out.append(b"\r\n")
    return b"".join(out)


def _read_reply(sock: socket.socket) -> bytes:
    """Read a single RESP line (enough for +OK / -ERR / simple replies)."""
    buf = b""
    while not buf.endswith(b"\r\n"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def set_key(key: str, value: str, ttl_seconds: int, *, url: str | None = None, timeout: float = 3.0) -> None:
    """`SET key value EX ttl` against REDIS_URL. Raises on connection or -ERR reply."""
    parsed = urlparse(url or _redis_url())
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6379
    password = parsed.password
    db = parsed.path.lstrip("/") if parsed.path else ""

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        if password:
            sock.sendall(_encode("AUTH", password))
            reply = _read_reply(sock)
            if not reply.startswith(b"+OK"):
                raise RuntimeError(f"redis AUTH failed: {reply!r}")
        if db and db != "0":
            sock.sendall(_encode("SELECT", db))
            reply = _read_reply(sock)
            if not reply.startswith(b"+OK"):
                raise RuntimeError(f"redis SELECT {db} failed: {reply!r}")
        sock.sendall(_encode("SET", key, value, "EX", str(int(ttl_seconds))))
        reply = _read_reply(sock)
        if not reply.startswith(b"+OK"):
            raise RuntimeError(f"redis SET failed: {reply!r}")
