#!/usr/bin/env python
"""
Example subscriber for Claude Code session events.

This demonstrates how to consume and process events published by the
Claude Code integration.

Usage:
    python examples/claude_code_subscriber.py

Events Consumed:
    - session.thread.agent.action (tool usage)
    - session.thread.start (session start)
    - session.thread.end (session end)
    - session.thread.error (errors)
"""

import asyncio
import logging
from datetime import datetime
from collections import defaultdict
from typing import Dict

from event_producers.rabbit import Consumer
from event_producers.events import EventEnvelope
from event_producers.events.domains.claude_code import (
    SessionAgentToolAction,
    SessionThreadStart,
    SessionThreadEnd,
    SessionThreadError,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# In-memory statistics
stats = {
    "sessions": {},
    "tool_usage": defaultdict(int),
    "total_events": 0,
}


async def handle_tool_action(envelope: EventEnvelope):
    """Process tool action events."""
    action = SessionAgentToolAction(**envelope.payload)

    tool_name = action.tool_metadata.tool_name
    session_id = action.session_id

    # Track tool usage
    stats["tool_usage"][tool_name] += 1
    stats["total_events"] += 1

    # Track session activity
    if session_id not in stats["sessions"]:
        stats["sessions"][session_id] = {
            "tools": defaultdict(int),
            "last_activity": datetime.utcnow(),
        }

    stats["sessions"][session_id]["tools"][tool_name] += 1
    stats["sessions"][session_id]["last_activity"] = datetime.utcnow()

    logger.info(
        f"Tool Used: {tool_name} | "
        f"Session: {session_id[:8]} | "
        f"Branch: {action.git_branch} | "
        f"Turn: {action.turn_number}"
    )

    # Example: Auto-trigger tests when files are written
    if tool_name in ["Write", "Edit", "MultiEdit"]:
        file_path = action.tool_metadata.tool_input.get("file_path", "")
        if file_path.endswith(".py"):
            logger.info(f"🧪 File modified: {file_path} - Consider running tests")


async def handle_session_start(envelope: EventEnvelope):
    """Process session start events."""
    start = SessionThreadStart(**envelope.payload)

    stats["sessions"][start.session_id] = {
        "started_at": start.started_at,
        "working_dir": start.working_directory,
        "git_branch": start.git_branch,
        "model": start.model,
        "tools": defaultdict(int),
    }

    logger.info(
        f"🚀 Session Started: {start.session_id[:8]} | "
        f"Branch: {start.git_branch} | "
        f"Dir: {start.working_directory}"
    )


async def handle_session_end(envelope: EventEnvelope):
    """Process session end events."""
    end = SessionThreadEnd(**envelope.payload)

    logger.info(
        f"🏁 Session Ended: {end.session_id[:8]} | "
        f"Duration: {end.duration_seconds}s | "
        f"Turns: {end.total_turns} | "
        f"Files Modified: {len(end.files_modified)} | "
        f"Commits: {len(end.git_commits)}"
    )

    # Example: Calculate productivity score
    if end.duration_seconds > 0:
        turns_per_minute = (end.total_turns / end.duration_seconds) * 60
        files_per_turn = len(end.files_modified) / max(end.total_turns, 1)

        logger.info(
            f"📊 Productivity: {turns_per_minute:.1f} turns/min | "
            f"{files_per_turn:.2f} files/turn"
        )

    # Example: Cost tracking
    if end.total_cost_usd:
        logger.info(f"💰 Session Cost: ${end.total_cost_usd:.4f}")


async def handle_session_error(envelope: EventEnvelope):
    """Process session error events."""
    error = SessionThreadError(**envelope.payload)

    logger.error(
        f"❌ Session Error: {error.session_id[:8]} | "
        f"Type: {error.error_type} | "
        f"Tool: {error.tool_name} | "
        f"Recoverable: {error.recoverable}"
    )

    if not error.recoverable:
        logger.error(f"⚠️  Fatal error in session {error.session_id[:8]}")


async def print_stats():
    """Periodically print statistics."""
    while True:
        await asyncio.sleep(30)  # Every 30 seconds

        logger.info("=" * 80)
        logger.info("📊 STATISTICS")
        logger.info(f"Total Events: {stats['total_events']}")
        logger.info(f"Active Sessions: {len(stats['sessions'])}")

        if stats["tool_usage"]:
            logger.info("\n🔧 Tool Usage:")
            for tool, count in sorted(stats["tool_usage"].items(), key=lambda x: -x[1]):
                logger.info(f"  {tool}: {count}")

        logger.info("=" * 80)


async def main():
    """Main subscriber loop."""
    logger.info("🎧 Starting Claude Code Event Subscriber")
    logger.info("Listening for events on pattern: session.thread.#")
    logger.info("")

    consumer = Consumer()

    # Subscribe to all session events
    @consumer.subscribe("session.thread.agent.action")
    async def on_tool_action(envelope: EventEnvelope):
        await handle_tool_action(envelope)

    @consumer.subscribe("session.thread.start")
    async def on_session_start(envelope: EventEnvelope):
        await handle_session_start(envelope)

    @consumer.subscribe("session.thread.end")
    async def on_session_end(envelope: EventEnvelope):
        await handle_session_end(envelope)

    @consumer.subscribe("session.thread.error")
    async def on_session_error(envelope: EventEnvelope):
        await handle_session_error(envelope)

    # Start stats printer in background
    asyncio.create_task(print_stats())

    # Run consumer
    try:
        await consumer.run()
    except KeyboardInterrupt:
        logger.info("\n👋 Shutting down subscriber")
        await consumer.close()


if __name__ == "__main__":
    asyncio.run(main())
