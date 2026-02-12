"""
OpenClaw agent lifecycle event payload definitions.

Events for tracking agent activity across the 33GOD ecosystem:
message flow, tool usage, sub-agent delegation, sessions, tasks, heartbeats, errors.

Routing key pattern: agent.{agent_name}.{action}
  e.g. agent.cack.message.received, agent.tonny.tool.invoked

All events are wrapped in EventEnvelope[T] where T is your payload type.
"""

from typing import Literal, Optional

from event_producers.events.core.abstraction import BaseEvent


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
# Payload Models
# ============================================================================


class AgentMessageReceived(BaseEvent):
    """
    Inbound message received by an agent.

    Published when: Agent receives a message from any channel
    Consumed by: Analytics, dashboards, event store
    Routing Key: agent.{agent_name}.message.received
    """

    agent_name: str  # "cack", "rererere", "tonny"
    channel: str  # "telegram", "whatsapp", "cli"
    sender: str  # User name or ID
    message_preview: str  # First 200 chars
    message_length: int
    session_key: str  # e.g. "agent:main:main"


class AgentMessageSent(BaseEvent):
    """
    Outbound response sent by an agent.

    Published when: Agent sends a response on any channel
    Consumed by: Analytics, dashboards, cost tracking
    Routing Key: agent.{agent_name}.message.sent
    """

    agent_name: str
    channel: str
    message_preview: str
    message_length: int
    model: Optional[str] = None
    tokens_used: Optional[int] = None
    duration_ms: Optional[int] = None


class AgentToolInvoked(BaseEvent):
    """
    Agent invoked a tool.

    Published when: Tool call starts (exec, web_search, read, etc.)
    Consumed by: Analytics, observability, debugging
    Routing Key: agent.{agent_name}.tool.invoked
    """

    agent_name: str
    tool_name: str  # "exec", "web_search", "read", etc.
    tool_params_preview: str  # First 200 chars of params
    session_key: str


class AgentToolCompleted(BaseEvent):
    """
    Agent tool call finished.

    Published when: Tool call completes (success or failure)
    Consumed by: Analytics, performance tracking
    Routing Key: agent.{agent_name}.tool.completed
    """

    agent_name: str
    tool_name: str
    success: bool
    duration_ms: Optional[int] = None
    output_preview: Optional[str] = None  # First 200 chars


class AgentSubagentSpawned(BaseEvent):
    """
    Agent delegated work to a sub-agent.

    Published when: Sub-agent is spawned
    Consumed by: Orchestration dashboards, cost tracking
    Routing Key: agent.{agent_name}.subagent.spawned
    """

    agent_name: str  # Parent agent
    child_label: str  # Sub-agent label
    child_session_key: str
    task_preview: str  # First 200 chars of task
    model: Optional[str] = None


class AgentSubagentCompleted(BaseEvent):
    """
    Sub-agent finished its work.

    Published when: Sub-agent completes (success or failure)
    Consumed by: Orchestration dashboards, analytics
    Routing Key: agent.{agent_name}.subagent.completed
    """

    agent_name: str
    child_label: str
    child_session_key: str
    success: bool
    duration_ms: Optional[int] = None
    result_preview: Optional[str] = None


class AgentSessionStarted(BaseEvent):
    """
    New agent session began.

    Published when: Session is created
    Consumed by: Session tracking, analytics
    Routing Key: agent.{agent_name}.session.started
    """

    agent_name: str
    session_key: str
    channel: Optional[str] = None
    model: Optional[str] = None


class AgentSessionEnded(BaseEvent):
    """
    Agent session closed.

    Published when: Session ends (timeout, completion, error)
    Consumed by: Session tracking, analytics
    Routing Key: agent.{agent_name}.session.ended
    """

    agent_name: str
    session_key: str
    reason: Optional[str] = None  # "timeout", "completion", "error"
    duration_ms: Optional[int] = None
    total_messages: Optional[int] = None


class AgentTaskAssigned(BaseEvent):
    """
    External task routed to an agent.

    Published when: Task is assigned (from Plane, another agent, cron, etc.)
    Consumed by: Task dashboards, orchestration
    Routing Key: agent.{agent_name}.task.assigned
    """

    agent_name: str  # Target agent
    source: str  # Who assigned it ("plane", "cack", "jarad")
    task_type: str  # "ticket", "message", "cron"
    task_preview: str


class AgentTaskCompleted(BaseEvent):
    """
    Agent finished an assigned task.

    Published when: Task is completed
    Consumed by: Task dashboards, orchestration
    Routing Key: agent.{agent_name}.task.completed
    """

    agent_name: str
    task_type: str
    success: bool
    duration_ms: Optional[int] = None


class AgentHeartbeat(BaseEvent):
    """
    Periodic agent heartbeat.

    Published when: Agent emits periodic health signal
    Consumed by: Health monitoring, dashboards
    Routing Key: agent.{agent_name}.heartbeat
    """

    agent_name: str
    status: str  # "ok", "busy", "error"
    active_sessions: Optional[int] = None
    uptime_ms: Optional[int] = None


class AgentError(BaseEvent):
    """
    Error occurred in agent processing.

    Published when: Agent encounters an error
    Consumed by: Alerting, error tracking
    Routing Key: agent.{agent_name}.error
    """

    agent_name: str
    error_type: str
    error_message: str
    context: Optional[str] = None  # What was happening when error occurred


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
