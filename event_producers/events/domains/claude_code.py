"""
Claude Code session and tool event definitions.

Events for tracking Claude Code CLI interactions, tool usage, and session lifecycle.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime

from event_producers.events.core.abstraction import BaseEvent


class ToolUseMetadata(BaseModel):
    """Metadata about a tool invocation."""

    tool_name: str
    tool_input: Dict[str, Any]
    execution_time_ms: Optional[int] = None
    success: bool = True
    error_message: Optional[str] = None
    output_preview: Optional[str] = None  # First 500 chars of output


class SessionAgentToolAction(BaseEvent):
    """
    Claude Code tool was invoked.

    Published when: Any tool is called in Claude Code
    Consumed by: Analytics, observability, debugging
    Routing Key: session.thread.agent.action

    Tracks all tool invocations with full metadata for replay and analysis.
    """

    session_id: str
    thread_id: Optional[str] = None
    conversation_id: Optional[str] = None
    tool_metadata: ToolUseMetadata
    working_directory: Optional[str] = None
    git_branch: Optional[str] = None
    git_status: Optional[str] = None  # Clean, modified, etc.
    files_in_context: List[str] = Field(default_factory=list)
    turn_number: Optional[int] = None  # Which turn in conversation
    model: Optional[str] = None  # e.g., "claude-sonnet-4-5"
    tags: List[str] = Field(default_factory=list)


class ThinkingEvent(BaseEvent):
    """
    Claude Code thinking/reasoning event.

    Published when: Claude emits thinking tokens (if available)
    Consumed by: Research, prompt engineering analysis
    Routing Key: session.thread.agent.thinking

    Captures reasoning process for analysis and improvement.
    """

    session_id: str
    thread_id: Optional[str] = None
    thinking_text: str
    thinking_duration_ms: Optional[int] = None
    turn_number: Optional[int] = None
    triggered_by_tool: Optional[str] = None  # Tool that preceded thinking


class SessionThreadEnd(BaseEvent):
    """
    Claude Code session ended.

    Published when: Session stops, times out, or user exits
    Consumed by: Analytics, session summaries, cost tracking
    Routing Key: session.thread.end

    Final event for a session with full statistics.
    """

    session_id: str
    thread_id: Optional[str] = None
    end_reason: str  # "user_stop", "timeout", "error", "completion", "waiting_for_input"
    duration_seconds: Optional[int] = None
    total_turns: int = 0
    total_tokens: Optional[int] = None
    total_cost_usd: Optional[float] = None
    tools_used: Dict[str, int] = Field(default_factory=dict)  # Tool name -> count
    files_modified: List[str] = Field(default_factory=list)
    git_commits: List[str] = Field(default_factory=list)  # Commit SHAs created
    final_status: str  # "success", "error", "partial", "abandoned"
    summary: Optional[str] = None
    working_directory: Optional[str] = None
    git_branch: Optional[str] = None


class SessionThreadStart(BaseEvent):
    """
    Claude Code session started.

    Published when: New session begins
    Consumed by: Session tracking, analytics
    Routing Key: session.thread.start

    Initial event marking session creation.
    """

    session_id: str
    thread_id: Optional[str] = None
    working_directory: str
    git_branch: Optional[str] = None
    git_remote: Optional[str] = None
    model: str
    user_prompt: Optional[str] = None  # Initial prompt if available
    context_files: List[str] = Field(default_factory=list)
    mcp_servers: List[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=datetime.utcnow)


class SessionThreadMessage(BaseEvent):
    """
    User message or assistant response in session.

    Published when: User sends message or Claude responds
    Consumed by: Conversation history, prompt analysis
    Routing Key: session.thread.message
    """

    session_id: str
    thread_id: Optional[str] = None
    role: str  # "user" or "assistant"
    content: str
    turn_number: int
    tokens: Optional[int] = None
    model: Optional[str] = None
    thinking_included: bool = False
    tool_calls: List[str] = Field(default_factory=list)  # Tool names called in this turn


class SessionThreadError(BaseEvent):
    """
    Error occurred during session.

    Published when: Any error/exception in Claude Code
    Consumed by: Error tracking, alerting
    Routing Key: session.thread.error
    """

    session_id: str
    thread_id: Optional[str] = None
    error_type: str
    error_message: str
    stack_trace: Optional[str] = None
    tool_name: Optional[str] = None  # Tool that caused error
    recoverable: bool = False
    turn_number: Optional[int] = None


# Routing key mapping for registry
ROUTING_KEYS = {
    "SessionAgentToolAction": "session.thread.agent.action",
    "ThinkingEvent": "session.thread.agent.thinking",
    "SessionThreadEnd": "session.thread.end",
    "SessionThreadStart": "session.thread.start",
    "SessionThreadMessage": "session.thread.message",
    "SessionThreadError": "session.thread.error",
}
