"""
Claude Code session and tool event definitions.

GENERATED FROM HOLYFIELDS SCHEMAS — Do not edit manually.
To update: modify JSON schemas in holyfields/schemas/session/thread/, regenerate.

Events for tracking Claude Code CLI interactions, tool usage, and session lifecycle.
"""

# ============================================================================
# Re-export from Holyfields generated models (schema-first)
# ============================================================================

from holyfields.compat import (
    SessionAgentToolAction,
    ThinkingEvent,
    SessionThreadEnd,
    SessionThreadStart,
    SessionThreadMessage,
    SessionThreadError,
)


# Routing key mapping for registry
ROUTING_KEYS = {
    "SessionAgentToolAction": "session.thread.agent.action",
    "ThinkingEvent": "session.thread.agent.thinking",
    "SessionThreadEnd": "session.thread.end",
    "SessionThreadStart": "session.thread.start",
    "SessionThreadMessage": "session.thread.message",
    "SessionThreadError": "session.thread.error",
}
