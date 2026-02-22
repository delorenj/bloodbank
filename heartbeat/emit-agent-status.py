#!/usr/bin/env python3
"""Emit agent status events to Bloodbank for Holocene dashboard visibility.

Polls active OpenClaw agent sessions and publishes status events.
Runs every minute via cron or systemd timer.
"""
import json, os, subprocess, time, urllib.request
from uuid import uuid4

BLOODBANK_API = os.environ.get("BLOODBANK_API", "http://127.0.0.1:8682")

AGENTS = [
    "cack", "grolf", "lenoon", "rar", "rererere",
    "tonny", "dumpling", "svgme", "overworld", "cack-app", "wean"
]

def publish_event(event_type: str, payload: dict) -> bool:
    body = json.dumps({
        "event_type": event_type,
        "event_id": str(uuid4()),
        "payload": payload,
        "source": {"host": os.uname().nodename, "type": "scheduled", "app": "agent-status-emitter"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": "1.0.0"
    }).encode()
    try:
        req = urllib.request.Request(
            f"{BLOODBANK_API}/events/custom",
            data=body, method="POST",
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False

def get_agent_sessions() -> dict:
    """Try to read agent session status from OpenClaw."""
    result = {}
    try:
        out = subprocess.run(
            ["openclaw", "status", "--json"],
            capture_output=True, text=True, timeout=10
        )
        if out.returncode == 0:
            data = json.loads(out.stdout)
            for s in data.get("sessions", []):
                key = s.get("key", "")
                if key.startswith("agent:"):
                    parts = key.split(":")
                    if len(parts) >= 2:
                        name = parts[1]
                        result[name] = {
                            "status": "active" if s.get("totalTokens", 0) > 0 else "idle",
                            "tokens": s.get("totalTokens", 0),
                            "model": s.get("model", "unknown"),
                        }
    except Exception:
        pass
    return result

def main():
    sessions = get_agent_sessions()

    for agent in AGENTS:
        info = sessions.get(agent, {"status": "idle", "tokens": 0, "model": "unknown"})
        publish_event(
            f"agent.{agent}.status",
            {
                "agent": agent,
                "status": info["status"],
                "tokens": info["tokens"],
                "model": info["model"],
                "check_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )

    # Also emit a system heartbeat
    publish_event("system.heartbeat", {
        "agents_checked": len(AGENTS),
        "agents_active": sum(1 for a in AGENTS if sessions.get(a, {}).get("status") == "active"),
        "check_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

if __name__ == "__main__":
    main()
