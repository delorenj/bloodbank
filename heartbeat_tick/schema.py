"""heartbeat.json schema — per-agent check definitions.

Each agent workspace contains a `heartbeat.json` that declares what checks
should run and at what cadence. The per-agent consumer reads this file on
every tick and dispatches overdue checks.

Schema (heartbeat.json):
{
    "agent": "grolf",
    "version": "1.0.0",
    "checks": [
        {
            "id": "check-direct-reports",
            "description": "Verify all direct reports are active",
            "interval_minutes": 15,
            "enabled": true,
            "action": "system_event",
            "prompt": "HEARTBEAT CHECK — No Idle Agents Policy...",
            "conditions": {
                "day_of_week": ["Monday","Tuesday","Wednesday","Thursday","Friday"],
                "hour_range": [6, 23]
            }
        }
    ]
}

Actions:
    system_event    Inject prompt as a system event into the agent's session
    publish         Publish an event to Bloodbank
    command         Run a shell command on the host

Conditions (optional, all must match for check to fire):
    day_of_week     list[str]   Only fire on these days (Monday-Sunday)
    hour_range      [int, int]  Only fire between these hours (UTC, inclusive)
    quarters        list[str]   Only fire in these quarters (Q1-Q4)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CheckConditions:
    """Optional conditions that gate whether a check fires."""
    day_of_week: list[str] = field(default_factory=list)
    hour_range: list[int] = field(default_factory=list)  # [start, end] inclusive
    quarters: list[str] = field(default_factory=list)

    def matches(self, day: str, hour: int, quarter: str) -> bool:
        if self.day_of_week and day not in self.day_of_week:
            return False
        if self.hour_range and len(self.hour_range) == 2:
            if not (self.hour_range[0] <= hour <= self.hour_range[1]):
                return False
        if self.quarters and quarter not in self.quarters:
            return False
        return True


@dataclass
class HeartbeatCheck:
    """A single check definition from heartbeat.json."""
    id: str
    description: str
    interval_minutes: int
    action: str  # system_event | publish | command
    enabled: bool = True
    prompt: str = ""
    event_type: str = ""
    command: str = ""
    conditions: CheckConditions = field(default_factory=CheckConditions)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HeartbeatCheck:
        cond = d.get("conditions", {})
        return cls(
            id=d["id"],
            description=d.get("description", ""),
            interval_minutes=d.get("interval_minutes", 15),
            action=d.get("action", "system_event"),
            enabled=d.get("enabled", True),
            prompt=d.get("prompt", ""),
            event_type=d.get("event_type", ""),
            command=d.get("command", ""),
            conditions=CheckConditions(
                day_of_week=cond.get("day_of_week", []),
                hour_range=cond.get("hour_range", []),
                quarters=cond.get("quarters", []),
            ),
        )


@dataclass
class HeartbeatConfig:
    """Parsed heartbeat.json for an agent."""
    agent: str
    version: str = "1.0.0"
    checks: list[HeartbeatCheck] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HeartbeatConfig:
        return cls(
            agent=d["agent"],
            version=d.get("version", "1.0.0"),
            checks=[HeartbeatCheck.from_dict(c) for c in d.get("checks", [])],
        )

    @classmethod
    def from_json(cls, path: str) -> HeartbeatConfig:
        import json
        from pathlib import Path
        data = json.loads(Path(path).read_text())
        return cls.from_dict(data)
