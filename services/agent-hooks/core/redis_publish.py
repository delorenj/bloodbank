"""Minimal Redis writer for hook/healthcheck contexts -- no redis-py.

Used to publish the agent-hook-tests health snapshot to the Redis key Holocene
reads (holocene:tooling:stat:agent-hook-tests).

The hand-rolled RESP reader that used to live here read until the buffer ENDED
with CRLF, which is correct only for a single-line reply like `+OK`. It has
been replaced by core/resp.py, which is length-aware; see that module for why
that mattered once anything issued more than one command per connection.
"""
from __future__ import annotations

import os

from .resp import Connection


def _redis_url() -> str:
    return (
        os.environ.get("TOOLING_REDIS_URL")
        or os.environ.get("REDIS_URL")
        or "redis://127.0.0.1:6379"
    )


def set_key(key: str, value: str, ttl_seconds: int, *, url: str | None = None,
            timeout: float = 3.0) -> None:
    """`SET key value EX ttl` against REDIS_URL. Raises on connection or -ERR."""
    with Connection(url or _redis_url(), timeout=timeout) as conn:
        conn.command("SET", key, value, "EX", str(int(ttl_seconds)))
