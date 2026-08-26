"""Stdlib-only NATS text-protocol publisher.

Justified by the one-shot fire-and-forget shape of agent CLI hooks:
open one TCP connection, PUB, drain with PING/PONG, close. No nats-py,
no virtualenv, no extra install steps. The NATS wire protocol is simple
enough that ~40 lines of socket code is fine.
"""
from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4222
MAX_CONTROL_LINE = 16 * 1024


def _parse_endpoint(value: str) -> tuple[str, int]:
    endpoint = value.strip()
    if endpoint.startswith("nats://"):
        endpoint = endpoint[7:]
    if not endpoint:
        return DEFAULT_HOST, DEFAULT_PORT
    if endpoint.startswith("["):
        close = endpoint.find("]")
        if close < 0:
            raise ValueError(f"invalid bracketed NATS endpoint: {value!r}")
        host = endpoint[1:close]
        rest = endpoint[close + 1 :]
        port = DEFAULT_PORT if not rest else int(rest.removeprefix(":"))
    elif endpoint.count(":") == 1:
        host, port_text = endpoint.rsplit(":", 1)
        port = int(port_text) if port_text else DEFAULT_PORT
    else:
        host, port = endpoint, DEFAULT_PORT
    if not host or not 1 <= port <= 65535:
        raise ValueError(f"invalid NATS endpoint: {value!r}")
    return host, port


def _config() -> tuple[str, int, float]:
    explicit_host = os.environ.get("BLOODBANK_NATS_HOST")
    explicit_port = os.environ.get("BLOODBANK_NATS_PORT")
    if explicit_host and explicit_port:
        compat_host, compat_port = DEFAULT_HOST, DEFAULT_PORT
    else:
        compat_host, compat_port = _parse_endpoint(
            os.environ.get("DECKARD_NATS", f"{DEFAULT_HOST}:{DEFAULT_PORT}")
        )
    host = explicit_host or compat_host
    port = int(explicit_port or compat_port)
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid BLOODBANK_NATS_PORT: {port}")
    timeout = float(os.environ.get("BLOODBANK_NATS_TIMEOUT", "3.0"))
    if timeout <= 0:
        raise ValueError("BLOODBANK_NATS_TIMEOUT must be positive")
    return host, port, timeout


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("NATS publish deadline expired before PONG")
    return remaining


def _resolve(host: str, port: int, deadline: float) -> list[tuple]:
    result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            result.put((True, addresses))
        except BaseException as exc:  # propagate resolver failures in caller thread
            result.put((False, exc))

    threading.Thread(target=resolve, name="bloodbank-nats-dns", daemon=True).start()
    try:
        ok, value = result.get(timeout=_remaining(deadline))
    except queue.Empty as exc:
        raise TimeoutError("NATS publish deadline expired during DNS") from exc
    if not ok:
        raise value  # type: ignore[misc]
    return value  # type: ignore[return-value]


def _connect(host: str, port: int, deadline: float) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, proto, _, sockaddr in _resolve(host, port, deadline):
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(_remaining(deadline))
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"no addresses resolved for NATS host {host!r}")


def _send(sock: socket.socket, data: bytes, deadline: float) -> None:
    sock.settimeout(_remaining(deadline))
    sock.sendall(data)


def _read_line(
    sock: socket.socket, buffered: bytearray, deadline: float
) -> tuple[bytes, bytearray]:
    while True:
        boundary = buffered.find(b"\r\n")
        if boundary >= 0:
            if boundary > MAX_CONTROL_LINE:
                raise RuntimeError("NATS control line exceeds bounded input")
            line = bytes(buffered[:boundary])
            return line, bytearray(buffered[boundary + 2 :])
        if len(buffered) > MAX_CONTROL_LINE:
            raise RuntimeError("NATS control line exceeds bounded input")
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("NATS connection reached EOF before PONG")
        buffered.extend(chunk)


def publish(
    subject: str,
    body: bytes,
    *,
    client_name: str = "bloodbank-agent-hook",
    timeout: float | None = None,
) -> None:
    """Publish with one DNS-through-PONG deadline; raise unless PONG arrives."""
    host, port, configured_timeout = _config()
    timeout = configured_timeout if timeout is None else timeout
    if timeout <= 0:
        raise ValueError("publish timeout must be positive")
    deadline = time.monotonic() + timeout
    with _connect(host, port, deadline) as sock:
        line, buffered = _read_line(sock, bytearray(), deadline)
        if not line.startswith(b"INFO"):
            raise RuntimeError(f"NATS greeting was not INFO: {line!r}")
        connect_opts = {
            "verbose": False,
            "pedantic": False,
            "tls_required": False,
            "name": client_name,
            "lang": "python-stdlib",
            "version": "1.0",
            "protocol": 1,
        }
        _send(
            sock,
            b"CONNECT " + json.dumps(connect_opts).encode("utf-8") + b"\r\n",
            deadline,
        )
        _send(
            sock,
            b"PUB " + subject.encode("ascii") + b" "
            + str(len(body)).encode("ascii") + b"\r\n" + body + b"\r\n"
            + b"PING\r\n",
            deadline,
        )
        while True:
            line, buffered = _read_line(sock, buffered, deadline)
            if line.startswith(b"PONG"):
                return
            if line.startswith(b"PING"):
                _send(sock, b"PONG\r\n", deadline)
            if line.startswith(b"-ERR"):
                raise RuntimeError(f"NATS rejected publish: {line!r}")
