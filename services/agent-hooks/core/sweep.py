"""The ASM sweeper — the two states no event can ever produce.

`stale` and `gone` have no triggering hook by definition, so something has to
ask. This is that something, and it is deliberately a systemd --user TIMER
rather than a daemon: there is no ack floor, no durable consumer and no
reconnect to get wrong, and a crash is repaired by the next tick 15 seconds
later.

  stale -- no signal for STALE_MS while the agent still has work outstanding,
           and /proc says it is alive. The agent is wedged, not finished. This
           is the headline new signal: nothing else on this box can produce it.

  gone  -- /proc/<pid> no longer exists. A DIRECT OBSERVATION of exit rather
           than a TTL guess, and the reason the state key is the process. No
           bus consumer can ever learn this: process exit is not an event, and
           `session.ended` means turn quiescence, not exit.

Verdicts are fired through the SAME asm.lua as every hook signal, so a
sweeper-produced edge is recorded, published and dispatched identically to a
hook-produced one. The sweeper decides; the arbiter still arbitrates.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import asm
from .resp import Connection

STALE_MS = int(os.environ.get("BLOODBANK_ASM_STALE_MS", "120000"))

# States the sweeper will never call stale. `awaiting_human` is the important
# one: an agent that has been waiting on a person for twenty minutes is
# blocked, not wedged, and reddening it would train you to ignore the signal.
NEVER_STALE = frozenset({"stale", "gone", "awaiting_human", "idle"})


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()[:8]
    except OSError:
        return "unknown"


def _alive(pid: int, starttime: int) -> bool:
    """Is this exact process still running?

    starttime is what makes the answer trustworthy: a recycled pid has a
    different start time, so a fresh process can never inherit a dead agent's
    row. That is the property that removes the need for an epoch counter.
    """
    st = asm.proc_stat(pid)
    if st is None:
        return False
    return starttime == 0 or st[2] == starttime


def _fire(conn: Connection, scope: str, signal: str, h: dict,
          live_key: str = "asm:live") -> dict | None:
    """Push a sweeper verdict through asm.lua and return the transition."""
    zsess, zpane = h.get("zellij_session", ""), h.get("zellij_pane", "")
    pane_idx = f"asm:idx:pane:{zsess}:{zpane}" if (zsess and zpane) else ""
    meta = {
        "cli": h.get("cli", ""), "pid": h.get("pid", ""),
        "starttime": h.get("starttime", ""), "cwd": h.get("cwd", ""),
        "basis": h.get("basis", ""), "zellij_session": zsess,
        "zellij_pane": zpane, "correlationid": h.get("correlationid", ""),
        "session_id": h.get("session_id", ""), "last_role": f"sweep:{signal}",
    }
    keys = [f"asm:a:{scope}", f"asm:t:{scope}", f"asm:lane:{scope}",
            live_key, pane_idx]
    args = [signal, asm.TTL_SECONDS, "main", json.dumps(meta, separators=(",", ":")),
            asm.LANE_GRACE_MS, asm.ERR_GRACE_MS, asm.ATTENTION_MS,
            asm.STREAM_MAXLEN, scope]
    result = asm._eval(conn, keys, args)
    if not result:
        return None
    try:
        return json.loads(result)
    except (TypeError, ValueError):
        return None


def sweep_once(conn: Connection, *, now_ms: float | None = None,
               live_key: str = "asm:live") -> dict:
    """One tick. Returns a small summary for logging and tests."""
    now = now_ms if now_ms is not None else time.time() * 1000
    seen = stale = gone = reaped = 0
    transitions: list[dict] = []

    for scope in conn.command("ZRANGE", live_key, "0", "-1") or []:
        raw = conn.command("HGETALL", f"asm:a:{scope}")
        if not raw:
            # The hash TTL'd out but the index entry survived it. Drop the
            # pointer rather than let asmctl render a ghost row.
            conn.command("ZREM", live_key, scope)
            reaped += 1
            continue
        h = {raw[i]: raw[i + 1] for i in range(0, len(raw), 2)}
        seen += 1

        try:
            pid = int(h.get("pid") or 0)
            starttime = int(h.get("starttime") or 0)
            last_ms = float(h.get("last_ms") or 0)
        except (TypeError, ValueError):
            continue
        state = h.get("state", "")
        busy = any(int(h.get(k) or 0) > 0 for k in ("turn", "tools", "subs"))

        # A scope resolved by native session id (the headless Hermes fleet) has
        # no pid to observe, so it can never be proven gone -- it ages out on
        # its TTL instead. Claiming `gone` for it would be a guess wearing an
        # observation's clothes.
        observable = pid > 0

        if observable and not _alive(pid, starttime):
            edge = _fire(conn, scope, "gone", h, live_key)
            gone += 1
            if edge:
                transitions.append(edge)
            continue

        if observable:
            # Proven alive: keep the row warm so a long-running agent does not
            # expire out of the table mid-task. Only a real state change goes
            # through the arbiter; this is just a TTL touch.
            for key in (f"asm:a:{scope}", f"asm:t:{scope}", f"asm:lane:{scope}"):
                conn.command("EXPIRE", key, asm.TTL_SECONDS)
            conn.command("ZADD", live_key, "XX", "CH", str(int(now)), scope)

        if busy and state not in NEVER_STALE and (now - last_ms) > STALE_MS:
            edge = _fire(conn, scope, "stale", h, live_key)
            stale += 1
            if edge:
                transitions.append(edge)

    # asm:live is the one shared key, so it gets trimmed here as well as in Lua.
    conn.command("ZREMRANGEBYSCORE", live_key, "-inf",
                 str(int(now - asm.TTL_SECONDS * 1000)))
    # Liveness. Absent => asmctl and Holocene must not believe any row, because
    # `stale` and `gone` are only true while something is asking.
    conn.command("SET", "asm:sweeper", f"{os.getpid()}:{boot_id()}", "EX", "60")

    return {"seen": seen, "stale": stale, "gone": gone, "reaped": reaped,
            "transitions": transitions}


def run(log=None) -> dict:
    """One sweep with dispatch. Never raises -- a timer must not fail loudly."""
    try:
        with Connection(asm._redis_url(), timeout=3.0) as conn:
            summary = sweep_once(conn)
    except Exception as exc:                       # noqa: BLE001
        if log:
            log(f"sweep failed: {exc!r}")
        return {"seen": 0, "stale": 0, "gone": 0, "reaped": 0,
                "transitions": [], "error": repr(exc)}

    # Sweeper edges must reach handlers too, or `->stale` and `->gone` -- the
    # only two states worth alerting on -- would silently fire nothing.
    for edge in summary["transitions"]:
        try:
            asm.dispatch(edge, edge.get("cli", ""), edge.get("cwd", "") or "/")
        except Exception as exc:                   # noqa: BLE001
            if log:
                log(f"dispatch failed for {edge.get('scope')}: {exc!r}")
    return summary
