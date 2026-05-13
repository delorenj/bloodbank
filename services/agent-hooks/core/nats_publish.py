"""Stdlib-only NATS text-protocol publisher.

Justified by the one-shot fire-and-forget shape of agent CLI hooks:
open one TCP connection, PUB, drain with PING/PONG, close. No nats-py,
no virtualenv, no extra install steps. The NATS wire protocol is simple
enough that ~40 lines of socket code is fine.
"""
from __future__ import annotations

import json
import os
import socket
import time


def _config() -> tuple[str, int, float]:
    host = os.environ.get("BLOODBANK_NATS_HOST", "127.0.0.1")
    port = int(os.environ.get("BLOODBANK_NATS_PORT", "4222"))
    timeout = float(os.environ.get("BLOODBANK_NATS_TIMEOUT", "3.0"))
    return host, port, timeout


def publish(subject: str, body: bytes, *, client_name: str = "bloodbank-agent-hook") -> None:
    """Publish to NATS at ``subject``. Raises OSError or RuntimeError on failure."""
    host, port, timeout = _config()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        f = sock.makefile("rwb", buffering=0)
        f.readline()  # discard server INFO greeting
        connect_opts = {
            "verbose": False,
            "pedantic": False,
            "tls_required": False,
            "name": client_name,
            "lang": "python-stdlib",
            "version": "1.0",
            "protocol": 1,
        }
        f.write(b"CONNECT " + json.dumps(connect_opts).encode("utf-8") + b"\r\n")
        f.write(
            b"PUB " + subject.encode("ascii") + b" "
            + str(len(body)).encode("ascii") + b"\r\n" + body + b"\r\n"
        )
        f.write(b"PING\r\n")
        f.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = f.readline()
            if not line:
                break
            if line.startswith(b"PONG"):
                return
            if line.startswith(b"-ERR"):
                raise RuntimeError(f"NATS rejected publish: {line!r}")
