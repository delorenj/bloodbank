"""Agent Hook Telemetry — did the hooks actually fire?

Replaces `agent-hook-health` and `agent-hook-tests`, which between them asked
only "does the config look right?" Both failure modes below are ones neither
could report correctly:

  * A config parser can only catch the failure modes it was TAUGHT to parse.
    Two Hermes PMs (infra, ssbnk) have every hook present in config and emit
    nothing at all, because the hooks are absent from a shell-hooks allowlist.
    That was caught only because someone thought to write that specific check.
  * A config parser goes stale against the system it inspects. The attention
    bindings (Notification / PermissionRequest / TeammateIdle) gained
    `publish: false` in 2026-08 and route to deckard.evt.attention instead of
    the event map. The old check still tested them AGAINST the event map and
    reported five permanent false failures — most of the red on that card.

Absence of traffic catches every reason at once. But absence alone is not
evidence either: antigravity emitted 0 events in 24h and 1334 in 7 days -- idle,
not broken. So this reads THREE sources and only calls something broken when
they disagree:

  1. hooks.master.json  — what SHOULD fire (the SSOT, including publish:false)
  2. asm:seen           — what DID fire, recorded by the hook itself
  3. /proc              — which agents are alive RIGHT NOW

An agent that is alive and silent is broken. An agent that is absent and silent
is just not running. Nothing else can tell those apart.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from core import agents as agent_discovery      # noqa: E402
from core import sweep                          # noqa: E402
from core.resp import Connection                # noqa: E402

MASTER = SERVICE_DIR / "hooks.master.json"
SEEN_KEY = "asm:seen"
STAT_KEY = "holocene:tooling:stat:agent-hook-telemetry"
TTL_SECONDS = 600            # > 2 timer periods (5 min), so a dead timer shows

# A role that has not fired in this long, while an agent of that CLI is alive,
# is treated as broken rather than quiet. Generous on purpose: session_start
# fires once per session, not per turn.
SILENT_AFTER_MS = 6 * 3600 * 1000


def _redis_url() -> str:
    return (os.environ.get("TOOLING_REDIS_URL")
            or os.environ.get("REDIS_URL") or "redis://127.0.0.1:6379")


def expected_bindings(master: dict) -> list[dict]:
    """Every (cli, role) pair the SSOT says should produce traffic.

    Reads publish:false straight from the SSOT rather than hardcoding a list, so
    this cannot drift the way the old checker did: an attention binding is
    EXPECTED to appear as `<cli>|attention`, never in the event map.
    """
    lifecycle = master.get("lifecycle") or {}
    out = []
    for cli, agent in (master.get("agents") or {}).items():
        for binding in agent.get("bindings") or []:
            role = binding.get("role") or binding.get("native") or "?"
            if binding.get("publish") is False:
                alert = binding.get("alert")
                if not alert:
                    continue          # dispatched only; produces no telemetry
                out.append({"cli": cli, "role": role, "native": binding.get("native"),
                            "key": f"{cli}|{alert}", "channel": "alert"})
                continue
            name = binding.get("lifecycle")
            spec = lifecycle.get(name) or {}
            ce_type = spec.get("type")
            if not ce_type or spec.get("emitted") is False:
                continue              # defined for fidelity, never bound
            out.append({"cli": cli, "role": role, "native": binding.get("native"),
                        "key": f"{cli}|{ce_type}", "channel": "event"})
    return out


def build_report(now_ms: float | None = None) -> dict:
    now = now_ms if now_ms is not None else time.time() * 1000
    master = json.loads(MASTER.read_text())
    bindings = expected_bindings(master)

    with Connection(_redis_url(), timeout=5.0) as conn:
        raw = conn.command("HGETALL", SEEN_KEY) or []
        seen = {raw[i]: raw[i + 1] for i in range(0, len(raw), 2)}
        if "__tracking_since" not in seen:
            conn.command("HSET", SEEN_KEY, "__tracking_since", str(int(now)))
            seen["__tracking_since"] = str(int(now))
    tracking_since = float(seen.get("__tracking_since") or now)

    alive: dict[str, int] = {}
    for agent in agent_discovery.discover():
        alive[agent["cli"]] = alive.get(agent["cli"], 0) + 1

    items, counts = [], {"live": 0, "quiet": 0, "silent": 0, "unknown": 0}
    for binding in bindings:
        cli = binding["cli"]
        last = float(seen.get(binding["key"]) or 0)
        age_ms = now - last if last else None
        running = alive.get(cli, 0)

        if last and age_ms is not None and age_ms < SILENT_AFTER_MS:
            verdict, severity = "live", "ok"
        elif not last and (now - tracking_since) < SILENT_AFTER_MS:
            # Tracking only just began; silence is not yet meaningful.
            verdict, severity = "unknown", "unknown"
        elif running and not last:
            verdict, severity = "silent", "critical"
        elif running:
            verdict, severity = "silent", "critical"
        elif last:
            verdict, severity = "quiet", "ok"
        else:
            # Never fired and nothing of this CLI is running: cannot tell a
            # broken hook from a CLI the operator simply does not use.
            verdict, severity = "unknown", "unknown"
        counts[verdict] += 1

        if age_ms is None:
            ago = "never"
        elif age_ms < 90_000:
            ago = f"{age_ms / 1000:.0f}s ago"
        elif age_ms < 5_400_000:
            ago = f"{age_ms / 60000:.0f}m ago"
        else:
            ago = f"{age_ms / 3600000:.1f}h ago"

        items.append({
            "id": binding["key"],
            "label": f"{cli} · {binding['role']}",
            "severity": severity,
            "statusLabel": verdict,
            "summary": (f"{binding['channel']} · last {ago}"
                        + (f" · {running} alive" if running else " · none running")),
            "detail": {
                "cli": cli, "role": binding["role"], "native": binding["native"],
                "channel": binding["channel"], "telemetryKey": binding["key"],
                "lastSeenAgo": ago, "agentsAlive": running, "verdict": verdict,
            },
        })

    # --- per-profile Hermes liveness -------------------------------------
    # The per-CLI matrix above cannot see one broken PM: `hermes` fires
    # constantly because 21 other profiles work. A profile whose gateway unit is
    # UP while it has never emitted a single event is unambiguously broken --
    # which is exactly the shape of the infra and ssbnk failures.
    try:
        gateways = sweep.gateway_pids()
    except Exception:                              # noqa: BLE001
        gateways = {}
    for profile, (pid, _start) in sorted(gateways.items()):
        if pid <= 0:
            continue                               # unit stopped; not a hook fault
        last = float(seen.get(f"profile|{profile}") or 0)
        if last:
            continue                               # has emitted at some point
        counts["silent"] += 1
        items.append({
            "id": f"profile|{profile}",
            "label": f"hermes · {profile}",
            "severity": "critical",
            "statusLabel": "silent",
            "summary": "gateway is up but this profile has never emitted an event",
            "detail": {"cli": "hermes", "profile": profile, "gatewayPid": pid,
                       "verdict": "silent", "agentsAlive": 1,
                       "hint": "hooks present in config but absent from the "
                               "shell-hooks allowlist will never fire"},
        })

    items.sort(key=lambda i: ({"critical": 0, "warning": 1, "unknown": 2,
                               "ok": 3}[i["severity"]], i["label"]))

    if counts["silent"]:
        status = "critical"
    elif counts["live"]:
        status = "warning" if counts["unknown"] > counts["live"] else "healthy"
    else:
        status = "unknown"

    return {
        "id": "agent-hook-telemetry",
        "status": status,
        "observedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "value": {
            "view": {"kind": "collection", "layout": "list",
                     "title": "Agent Hook Telemetry"},
            "items": items,
        },
        "meta": {
            "source": "bloodbank/services/agent-hooks/health/hook_telemetry.py",
            "ttlSeconds": TTL_SECONDS,
            "trackingSince": datetime.fromtimestamp(
                tracking_since / 1000, timezone.utc).isoformat(timespec="seconds"),
            "counts": counts,
        },
    }


def main() -> int:
    try:
        report = build_report()
    except Exception as exc:                      # noqa: BLE001
        print(f"hook-telemetry: FAILED {exc!r}", file=sys.stderr)
        return 1
    body = json.dumps(report, separators=(",", ":"))
    try:
        with Connection(_redis_url(), timeout=5.0) as conn:
            conn.command("SET", STAT_KEY, body, "EX", str(TTL_SECONDS))
    except Exception as exc:                      # noqa: BLE001
        print(f"hook-telemetry: redis write failed {exc!r}", file=sys.stderr)
        return 1
    c = report["meta"]["counts"]
    print(f"hook-telemetry: {report['status']} — "
          f"{c['live']} live, {c['silent']} silent, {c['quiet']} quiet, "
          f"{c['unknown']} unknown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
