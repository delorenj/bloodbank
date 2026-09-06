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
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import agents as agent_discovery
from . import asm
from .resp import Connection

STALE_MS = int(os.environ.get("BLOODBANK_ASM_STALE_MS", "120000"))

# States the sweeper will never call stale. `awaiting_human` is the important
# one: an agent that has been waiting on a person for twenty minutes is
# blocked, not wedged, and reddening it would train you to ignore the signal.
NEVER_STALE = frozenset({"stale", "gone", "awaiting_human", "idle", "unknown"})


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()[:8]
    except OSError:
        return "unknown"


def gateway_pids() -> dict[str, tuple[int, int]]:
    """profile -> (pid, starttime) for every per-profile Hermes gateway unit.

    THIS IS THE ANSWER TO "can we treat the gateway as the pid" -- yes, but the
    PER-PROFILE gateway, never the fleet one. There are 25 per-profile gateway
    units, each a long-lived process; there is exactly ONE
    hermes-fleet-bloodbank-gateway.service for all of them, so keying liveness
    on the fleet router would mark all 25 PMs gone on a single restart and would
    say nothing about any individual agent.

    A profile ABSENT from this map is unobservable and ages out on its TTL. A
    profile PRESENT with MainPID 0 is an observation: its gateway is stopped or
    failed, so the agent is down. That distinction is the whole point -- never
    claim `gone` for something you merely cannot see.

    One systemctl call for the whole fleet, measured at 29ms. Cheap at a 15s
    tick, and deliberately not on the hook path.
    """
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "hermes-*-gateway.service",
             "-p", "Id", "-p", "MainPID"],
            capture_output=True, text=True, timeout=5.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}

    # Parse BLOCKS separated by blank lines, never a property ORDER: systemd
    # emits `MainPID=` BEFORE `Id=` here, and guarantees no ordering at all.
    # Assuming Id-then-MainPID paired every unit with the NEXT unit's pid --
    # an off-by-one across the whole fleet that reported live PMs as gone and
    # dead ones as alive, with entirely plausible-looking numbers.
    result: dict[str, tuple[int, int]] = {}
    for block in out.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key] = value
        unit = fields.get("Id", "")
        if not (unit.startswith("hermes-") and unit.endswith("-gateway.service")):
            continue
        profile = unit[len("hermes-"):-len("-gateway.service")]
        if profile in asm.NOT_AN_AGENT_PROFILE:
            continue
        try:
            pid = int(fields.get("MainPID", "0"))
        except ValueError:
            pid = 0
        st = asm.proc_stat(pid) if pid > 0 else None
        result[profile] = (pid, st[2] if st else 0)
    return result


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
    """Push a sweeper verdict through asm.lua and return the transition.

    Thin by design: the proposer lives in `asm.fire` so the sweeper and the
    surfaces that acknowledge a bell go through one code path into one
    arbiter.
    """
    return asm.fire(conn, scope, signal, h, live_key)


def sweep_once(conn: Connection, *, now_ms: float | None = None,
               live_key: str = "asm:live", discover: bool = True) -> dict:
    """One tick. Returns a small summary for logging and tests."""
    now = now_ms if now_ms is not None else time.time() * 1000
    seen = stale = gone = reaped = found = 0
    transitions: list[dict] = []
    gateways: dict[str, tuple[int, int]] | None = None

    # DISCOVERY FIRST. asm:live only ever knew agents that fired a hook inside
    # the TTL window -- measured at 2 scopes against 71 live agent processes --
    # so a quiet agent silently fell out of the table and stopped being
    # observable at all. Seeding from /proc makes the board answer "who is
    # alive" rather than "who was recently noisy", and it is what lets `gone`
    # fire for an agent that never emitted a single event.
    if discover:
        for found_agent in agent_discovery.discover():
            # The returned edge is DELIBERATELY discarded. A none->unknown
            # discovery is bookkeeping, not a state change: dispatching it would
            # spawn one handler per agent on every cold start (70 here) and
            # again whenever a row ages out and is re-seeded. Only edges driven
            # by real activity, or by a stale/gone verdict, reach handlers.
            _fire(conn, found_agent["scope"], "discover", {
                "cli": found_agent["cli"], "pid": found_agent["pid"],
                "starttime": found_agent["starttime"], "cwd": found_agent["cwd"],
                "basis": "agent-env" if found_agent["profile"] else "discovered",
                "zellij_session": found_agent["session"],
                "zellij_pane": found_agent["pane"],
                "profile": found_agent["profile"], "last_role": "sweep:discover",
            }, live_key)
            found += 1

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

        # An agent-env scope (a Hermes profile) is not a process, so its
        # liveness comes from its per-profile gateway unit rather than from a
        # pid in the row. Resolved lazily: one systemctl call per tick, and only
        # if such a scope actually exists.
        if h.get("basis") == "agent-env":
            if gateways is None:
                gateways = gateway_pids()
            entry = gateways.get(h.get("profile", ""))
            if entry is None:
                observable = False          # no gateway unit => cannot observe
            else:
                pid, starttime = entry
                observable = True           # present, even at MainPID 0
        else:
            # A scope resolved by native session id has no pid to observe, so it
            # can never be proven gone -- it ages out on its TTL instead.
            # Claiming `gone` for it would be a guess wearing an observation's
            # clothes.
            observable = pid > 0

        if observable and (pid <= 0 or not _alive(pid, starttime)):
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
            "found": found, "transitions": transitions}


HOLOCENE_STAT_KEY = "holocene:tooling:stat:agent-state-machine"
HOLOCENE_STAT_TTL = 90        # < 2 sweeper ticks, so a dead sweeper goes stale fast

# ASM state -> the dashboard's four-value severity vocabulary.
SEVERITY = {
    "stale": "critical", "failed": "critical",
    "awaiting_human": "warning",
    "working": "ok", "tool_running": "ok", "delegating": "ok",
    "idle": "ok", "starting": "ok", "gone": "unknown",
}


def _human(ms: float) -> str:
    seconds = ms / 1000.0
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def write_holocene_stat(conn: Connection, *, now_ms: float | None = None) -> int:
    """Publish the live agent board to the key Holocene reads.

    This is the payoff for an operator who is headless or away from the box: the
    same table `asmctl` prints, in the dashboard, without a terminal. Written by
    the sweeper because the sweeper is the only thing that runs on a cadence and
    already knows which rows are trustworthy.

    Only agents with an OBSERVED state get a row. The discovered-but-never-seen
    majority (70 of 71 on a cold start) is reported as one rollup line instead of
    seventy `unknown` cards -- a dashboard that shows everything shows nothing.
    """
    now = now_ms if now_ms is not None else time.time() * 1000
    items, discovered = [], 0

    for scope in conn.command("ZRANGE", "asm:live", "0", "-1") or []:
        raw = conn.command("HGETALL", f"asm:a:{scope}")
        if not raw:
            continue
        h = {raw[i]: raw[i + 1] for i in range(0, len(raw), 2)}
        state = h.get("state", "unknown")
        if state == "unknown":
            discovered += 1
            continue
        since = float(h.get("since") or 0)
        held = _human(now - since) if since else "-"
        where = os.path.basename((h.get("cwd") or "").rstrip("/")) or "-"
        pane = h.get("zellij_pane") or ""
        items.append({
            "id": scope,
            "label": f"{h.get('cli', '?')} · {where}",
            "severity": SEVERITY.get(state, "unknown"),
            "statusLabel": state,
            "summary": f"{state} for {held}" + (f" · pane {pane}" if pane else ""),
            "detail": {
                "scope": scope, "cli": h.get("cli", ""), "state": state,
                "heldFor": held, "cwd": h.get("cwd", ""),
                "pid": h.get("pid", ""), "basis": h.get("basis", ""),
                "profile": h.get("profile", ""),
                "zellijSession": h.get("zellij_session", ""), "zellijPane": pane,
                "tools": h.get("tools", "0"), "subs": h.get("subs", "0"),
                "turn": h.get("turn", "0"), "previous": h.get("prev", ""),
            },
        })

    items.sort(key=lambda i: ({"critical": 0, "warning": 1, "ok": 2,
                               "unknown": 3}[i["severity"]], i["label"]))
    if discovered:
        items.append({
            "id": "discovered-only",
            "label": f"{discovered} agent(s) alive, not yet observed",
            "severity": "unknown",
            "statusLabel": "discovered",
            "summary": "Found in /proc but has not emitted a hook event yet",
            "detail": {"count": discovered},
        })

    # Overall card health, derived from OBSERVED agents only -- the
    # discovered-only rollup must not drag the whole board to `unknown`, and an
    # agent we have merely found is not evidence of anything being wrong.
    # normalizeSnapshot() reads this and falls back to "unknown" when absent.
    severities = {i["severity"] for i in items if i["id"] != "discovered-only"}
    if "critical" in severities:
        status = "critical"          # something is stale or failed
    elif "warning" in severities:
        status = "warning"           # something is waiting on a human
    elif severities:
        status = "healthy"
    else:
        status = "unknown"           # nothing observed yet

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "id": "agent-state-machine",
        "status": status,
        "observedAt": now_iso,
        "value": {
            "view": {"kind": "collection", "layout": "list",
                     "title": "Agent State Machine"},
            "items": items,
        },
        "meta": {"source": "bloodbank-asm-sweeper",
                 "ttlSeconds": HOLOCENE_STAT_TTL},
    }
    conn.command("SET", HOLOCENE_STAT_KEY,
                 json.dumps(payload, separators=(",", ":")),
                 "EX", str(HOLOCENE_STAT_TTL))
    return len(items)


def run(log=None) -> dict:
    """One sweep with dispatch. Never raises -- a timer must not fail loudly."""
    try:
        with Connection(asm._redis_url(), timeout=3.0) as conn:
            summary = sweep_once(conn)
            # Best-effort: a dashboard write must never fail a sweep tick.
            try:
                summary["holocene_items"] = write_holocene_stat(conn)
            except Exception as exc:                   # noqa: BLE001
                if log:
                    log(f"holocene stat write failed: {exc!r}")
    except Exception as exc:                       # noqa: BLE001
        if log:
            log(f"sweep failed: {exc!r}")
        return {"seen": 0, "stale": 0, "gone": 0, "reaped": 0, "found": 0,
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
