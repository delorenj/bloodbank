"""statekit -- the shared kit for 33GOD Redis state machines (stdlib only).

Extracted from the ASM (consumer 1) so the next machine does not rediscover
its plumbing; crank (consumer 2, ~/.agents/tools/jev-clairic/bin/crank.lua) is
the first to be built on it. A machine is:

  * a Lua ARBITER, the only writer of its keys. Proposers (hooks, CLIs, UIs,
    sweepers) send signals; Redis runs the arbiter atomically, so N racing
    proposers need no owning process, no CAS retry and cannot lose an update;
  * a resolved HASH per subject, a capped transition STREAM per subject, one
    PUBSUB channel per machine, a TTL to GC subjects nobody re-asserts, and a
    HEARTBEAT for the observer that re-asserts them (statekit_prelude.lua has
    the Lua side of each convention, prepended to every arbiter body).

Two ways to ship an arbiter, both here:

  Script   -- EVAL/EVALSHA. The sha is DERIVED from the body every call (asm.py
              learned the hard way that a cached sha keeps running the previous
              script after an edit). Right for a hot path whose caller always
              has the body on disk (the ASM hooks).
  Library  -- Redis Functions (FUNCTION LOAD / FCALL). The code lives IN Redis
              (persisted with the dataset), so a caller in any language needs
              only the function name: pjangler (TypeScript) calls crank through
              `redis-cli FCALL` without shipping a copy of crank.lua. A
              `<lib>_version` function returns the body's sha; `ensure()`
              reloads when it differs, and `call()` loads on "Function not
              found" and retries once.

Usable from outside agent-hooks: put this directory's parent on sys.path and
`from core.statekit import Library, connect`. `python3 -m core.statekit load
<name> <file.lua>` (run from agent-hooks/) loads a library by hand.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

from .resp import Connection, RedisError

PRELUDE = Path(__file__).with_name("statekit_prelude.lua")

__all__ = ["Connection", "RedisError", "PRELUDE", "Script", "Library", "redis_url",
           "connect", "heartbeat", "heartbeat_age_ms", "stream_tail"]


def redis_url(*env_names: str, default: str = "redis://127.0.0.1:6379") -> str:
    """The first non-empty of `env_names`, then REDIS_URL, then `default`."""
    for name in (*env_names, "REDIS_URL"):
        value = os.environ.get(name)
        if value:
            return value
    return default


def connect(url: str | None = None, *env_names: str, timeout: float = 3.0) -> Connection:
    return Connection(url or redis_url(*env_names), timeout=timeout)


def _source(path: Path, prelude: bool) -> bytes:
    body = path.read_bytes()
    if not prelude:
        return body
    return PRELUDE.read_bytes() + b"\n" + body


class Script:
    """An EVAL arbiter: EVALSHA by the body's own sha, SCRIPT LOAD on NOSCRIPT."""

    def __init__(self, path: str | Path, *, prelude: bool = True):
        self.path = Path(path)
        self.prelude = prelude

    def body(self) -> bytes:
        return _source(self.path, self.prelude)

    def sha(self) -> str:
        return hashlib.sha1(self.body()).hexdigest()  # noqa: S324 -- Redis's own key

    def __call__(self, conn: Connection, keys: list[str], args: list[object]) -> object:
        body = self.body()
        sha = hashlib.sha1(body).hexdigest()  # noqa: S324
        try:
            return conn.command("EVALSHA", sha, len(keys), *keys, *args)
        except RedisError as exc:
            if "NOSCRIPT" not in str(exc):
                raise
        # First use on this Redis, or the cache was flushed / the server restarted.
        conn.command("SCRIPT", "LOAD", body)
        return conn.command("EVALSHA", sha, len(keys), *keys, *args)


class Library:
    """A Redis Functions arbiter named `name`, from a Lua body that registers its functions.

    The loaded code is `#!lua name=<name>`, the prelude, the body, and a
    `<name>_version` function (no-writes) returning the sha of prelude+body.
    """

    def __init__(self, name: str, path: str | Path, *, prelude: bool = True):
        self.name = name
        self.path = Path(path)
        self.prelude = prelude

    def version(self) -> str:
        return hashlib.sha1(_source(self.path, self.prelude)).hexdigest()  # noqa: S324

    def code(self) -> str:
        src = _source(self.path, self.prelude)
        sha = hashlib.sha1(src).hexdigest()  # noqa: S324
        return (f"#!lua name={self.name}\n" + src.decode("utf-8") +
                f"\nredis.register_function{{function_name='{self.name}_version', "
                f"callback=function() return '{sha}' end, flags={{'no-writes'}}}}\n")

    def loaded_version(self, conn: Connection) -> str | None:
        try:
            got = conn.command("FCALL_RO", f"{self.name}_version", 0)
        except RedisError:
            return None
        return got if isinstance(got, str) else None

    def load(self, conn: Connection) -> str:
        return conn.command("FUNCTION", "LOAD", "REPLACE", self.code())

    def ensure(self, conn: Connection) -> bool:
        """Load (REPLACE) when Redis holds no or another version. True when it (re)loaded."""
        if self.loaded_version(conn) == self.version():
            return False
        self.load(conn)
        return True

    def call(self, conn: Connection, fn: str, keys: list[str] = (), args: list[object] = (),
             *, ro: bool = False) -> object:
        verb = "FCALL_RO" if ro else "FCALL"
        try:
            return conn.command(verb, fn, len(keys), *keys, *args)
        except RedisError as exc:
            if "Function not found" not in str(exc) and "function not found" not in str(exc).lower():
                raise
        self.load(conn)
        return conn.command(verb, fn, len(keys), *keys, *args)


def heartbeat(conn: Connection, key: str, ttl_ms: int | None = None, **fields: object) -> int:
    """HSET key ts=<now ms> + fields; PEXPIRE when ttl_ms. Returns ts (client clock)."""
    now = int(time.time() * 1000)
    flat: list[object] = ["ts", now]
    for k, v in fields.items():
        flat += [k, v]
    conn.command("HSET", key, *flat)
    if ttl_ms:
        conn.command("PEXPIRE", key, int(ttl_ms))
    return now


def heartbeat_age_ms(conn: Connection, key: str, now_ms: int | None = None) -> int | None:
    """Age of a heartbeat, or None when it is absent (expired = dead, never "0")."""
    raw = conn.command("HGET", key, "ts")
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return None
    return (now_ms if now_ms is not None else int(time.time() * 1000)) - ts


def stream_tail(conn: Connection, key: str, count: int = 20) -> list[dict]:
    """The newest `count` stream entries, oldest first, as {"id": ..., **fields}."""
    rows = conn.command("XREVRANGE", key, "+", "-", "COUNT", count) or []
    out = []
    for entry_id, flat in reversed(rows):
        rec = {"id": entry_id}
        rec.update(dict(zip(flat[::2], flat[1::2])))
        out.append(rec)
    return out


def _main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "load":
        lib = Library(argv[1], argv[2])
        with connect() as conn:
            print(lib.load(conn), lib.version())
        return 0
    print("usage: python3 -m core.statekit load <name> <file.lua>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
