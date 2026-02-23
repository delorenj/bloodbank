"""
OpenClaw agent lifecycle event payload definitions.

GENERATED FROM HOLYFIELDS SCHEMAS — Do not edit manually.
To update: modify JSON schemas in holyfields/schemas/agent/, regenerate, re-export here.

Events for tracking agent activity across the 33GOD ecosystem:
message flow, tool usage, sub-agent delegation, sessions, tasks, heartbeats, errors.

Routing key pattern: agent.{agent_name}.{action}
  e.g. agent.cack.message.received, agent.tonny.tool.invoked

All events are wrapped in EventEnvelope[T] where T is your payload type.
"""

from typing import Literal

# ============================================================================
# Re-export from Holyfields generated models (schema-first)
# ============================================================================

from holyfields.compat import (
    AgentError,
    AgentHeartbeat,
    AgentMessageReceived,
    AgentMessageSent,
    AgentSessionEnded,
    AgentSessionStarted,
    AgentSubagentCompleted,
    AgentSubagentSpawned,
    AgentTaskAssigned,
    AgentTaskCompleted,
    AgentToolCompleted,
    AgentToolInvoked,
)


# ============================================================================
# Event Type Literal
# ============================================================================

AgentOpenClawEventType = Literal[
    "agent.message.received",
    "agent.message.sent",
    "agent.tool.invoked",
    "agent.tool.completed",
    "agent.subagent.spawned",
    "agent.subagent.completed",
    "agent.session.started",
    "agent.session.ended",
    "agent.task.assigned",
    "agent.task.completed",
    "agent.heartbeat",
    "agent.error",
]


# ============================================================================
# Routing Keys (for registry auto-discovery)
# ============================================================================

ROUTING_KEYS = {
    "AgentMessageReceived": "agent.message.received",
    "AgentMessageSent": "agent.message.sent",
    "AgentToolInvoked": "agent.tool.invoked",
    "AgentToolCompleted": "agent.tool.completed",
    "AgentSubagentSpawned": "agent.subagent.spawned",
    "AgentSubagentCompleted": "agent.subagent.completed",
    "AgentSessionStarted": "agent.session.started",
    "AgentSessionEnded": "agent.session.ended",
    "AgentTaskAssigned": "agent.task.assigned",
    "AgentTaskCompleted": "agent.task.completed",
    "AgentHeartbeat": "agent.heartbeat",
    "AgentError": "agent.error",
}
