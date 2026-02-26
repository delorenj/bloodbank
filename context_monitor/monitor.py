"""
Context Monitor (GOD-13) — Runtime automation for self-healing context.

Responsibilities:
1. Poll OpenClaw sessions for context usage > 90%.
2. Dispatch 'command.{agent}.context_overflow' if threshold exceeded.
3. Watch for 'memory/context-compaction-latest.md' updates.
4. On artifact update, TRUNCATE the active session to force a hard reset.
"""
import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

import aio_pika
from watchfiles import awatch

# Configuration
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://delorenj:YtTUffjIK3wDB7eB6IPCbSbu5r2XWiim@127.0.0.1:5673/")
EXCHANGE_NAME = os.environ.get("BLOODBANK_EXCHANGE", "bloodbank.events.v1")
OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", os.path.expanduser("~/.openclaw")))
CHECK_INTERVAL_S = 60
CONTEXT_THRESHOLD = 0.90

# Map agent_id -> workspace_dir_name
AGENT_WORKSPACE_MAP = {
    "main": "workspace",
    "family": "workspace-tonny",
    "work": "workspace-rererere",
    "infra": "workspace-lenoon",
    "eng": "workspace-grolf",
    "mobile": "workspace-rar",
    "svgme": "workspace-svgme",
    "wean": "workspace-wean",
    "overworld": "workspace-overworld",
    "cack-app": "workspace-cack-app",
    "dumpling": "workspace-dumpling",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("context-monitor")

class ContextMonitor:
    def __init__(self):
        self.connection: Optional[aio_pika.RobustConnection] = None
        self.channel: Optional[aio_pika.Channel] = None
        self.exchange: Optional[aio_pika.Exchange] = None
        self.pending_compaction: Dict[str, float] = {}  # agent_id -> timestamp sent

    async def connect(self):
        logger.info(f"Connecting to RabbitMQ: {RABBITMQ_URL}")
        self.connection = await aio_pika.connect_robust(RABBITMQ_URL)
        self.channel = await self.connection.channel()
        self.exchange = await self.channel.declare_exchange(EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True)

    async def send_command(self, agent_id: str, action: str, message: str, priority: str = "high"):
        if not self.exchange:
            await self.connect()
        
        routing_key = f"command.{agent_id}.{action}"
        payload = {
            "event_type": "command.envelope",
            "source": "context-monitor",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "payload": {
                "command_id": f"ctx-{int(time.time())}",
                "target_agent": agent_id,
                "action": action,
                "issued_by": "context-monitor",
                "priority": priority,
                "command_payload": {
                    "message": message
                }
            }
        }
        
        msg = aio_pika.Message(
            body=json.dumps(payload).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT
        )
        await self.exchange.publish(msg, routing_key=routing_key)
        logger.info(f"Sent command to {agent_id}: {action}")

    async def get_sessions(self):
        """Scan session files directly to avoid CLI dependency."""
        sessions = []
        agents_dir = OPENCLAW_HOME / "agents"
        if not agents_dir.exists():
            return []

        for agent_dir in agents_dir.iterdir():
            if not agent_dir.is_dir(): continue
            agent_id = agent_dir.name
            
            sessions_json = agent_dir / "sessions" / "sessions.json"
            if not sessions_json.exists(): continue
            
            try:
                # Read sessions.json to find active session
                with open(sessions_json, "r") as f:
                    data = json.load(f)
                
                # sessions.json is keyed by sessionKey "agent:id:channel..."
                # We want the one with latest updatedAt
                active_s = None
                for key, s in data.items():
                    if not active_s or s.get("updatedAt", 0) > active_s.get("updatedAt", 0):
                        active_s = s
                        active_s["agentId"] = agent_id # Ensure agentId is set
                
                if active_s:
                    # Now read the JSONL to get token usage
                    session_id = active_s.get("sessionId")
                    jsonl_path = agent_dir / "sessions" / f"{session_id}.jsonl"
                    
                    if jsonl_path.exists():
                        # Read last line efficiently
                        last_line = ""
                        with open(jsonl_path, "rb") as f:
                            try:
                                f.seek(-2, os.SEEK_END)
                                while f.read(1) != b'\n':
                                    f.seek(-2, os.SEEK_CUR)
                                last_line = f.readline().decode()
                            except OSError:
                                f.seek(0)
                                lines = f.readlines()
                                if lines: last_line = lines[-1].decode()
                        
                        if last_line:
                            try:
                                entry = json.loads(last_line)
                                if "message" in entry and "usage" in entry["message"]:
                                    usage = entry["message"]["usage"]
                                    active_s["totalTokens"] = usage.get("totalTokens", 0)
                                # Infer context limit from model
                                model = active_s.get("model", "")
                                if "gpt-5.3" in model or "opus" in model or "sonnet" in model:
                                    active_s["contextTokens"] = 200000
                                else:
                                    active_s["contextTokens"] = 128000
                            except Exception:
                                pass
                    
                    sessions.append(active_s)
            except Exception as e:
                logger.error(f"Error reading session for {agent_id}: {e}")
        
        return sessions

    async def check_context_usage(self):
        sessions = await self.get_sessions()
        
        # Group by agent, find active session
        agent_sessions = {}
        for s in sessions:
            aid = s.get("agentId")
            if not aid: continue
            # Prefer active sessions
            if aid not in agent_sessions or s["updatedAt"] > agent_sessions[aid]["updatedAt"]:
                agent_sessions[aid] = s
        
        for agent_id, session in agent_sessions.items():
            used = session.get("totalTokens", 0) or 0
            limit = session.get("contextTokens", 128000) or 128000
            
            if limit == 0: continue
            
            usage_pct = used / limit
            
            if usage_pct >= CONTEXT_THRESHOLD:
                # Check backoff
                last_sent = self.pending_compaction.get(agent_id, 0)
                if time.time() - last_sent > 300: # 5 min backoff
                    logger.warning(f"Agent {agent_id} context critical: {used}/{limit} ({usage_pct:.1%})")
                    
                    msg = (
                        f"CRITICAL: Context usage is at {usage_pct:.1%}. "
                        f"Limit: {limit}. Used: {used}. "
                        "POLICY ENFORCEMENT: You must COMPACT NOW. "
                        "1. Write summary to `memory/context-compaction-latest.md`. "
                        "2. Wait for system reset."
                    )
                    
                    await self.send_command(agent_id, "context_overflow", msg)
                    self.pending_compaction[agent_id] = time.time()

    async def reset_session(self, agent_id: str):
        """Hard reset: truncate the active session file."""
        # Find active session ID
        sessions = await self.get_sessions()
        # Find latest
        target_s = None
        for s in sessions:
            if s.get("agentId") == agent_id:
                if not target_s or s["updatedAt"] > target_s["updatedAt"]:
                    target_s = s
        
        if not target_s:
            logger.error(f"No session found for {agent_id} to reset")
            return

        session_id = target_s["sessionId"]
        # Construct path: ~/.openclaw/agents/{agent_id}/sessions/{session_id}.jsonl
        # Note: 'openclaw sessions' output doesn't give full path to jsonl, only store path.
        # Store path: ~/.openclaw/agents/{agent_id}/sessions/sessions.json
        # JSONL is in same dir.
        
        jsonl_path = OPENCLAW_HOME / "agents" / agent_id / "sessions" / f"{session_id}.jsonl"
        
        if jsonl_path.exists():
            logger.info(f"RESETTING SESSION for {agent_id}: {jsonl_path}")
            # Truncate
            try:
                # Backup first
                backup_path = jsonl_path.with_suffix(f".jsonl.bak.{int(time.time())}")
                
                # Get stats for ownership preservation
                stat = jsonl_path.stat()
                uid, gid = stat.st_uid, stat.st_gid
                
                os.rename(jsonl_path, backup_path)
                
                # Create empty and restore ownership
                jsonl_path.touch()
                os.chown(jsonl_path, uid, gid)
                
                logger.info(f"Session truncated. Backup at {backup_path}")
                
                # Notify agent
                await self.send_command(
                    agent_id, 
                    "session_reset", 
                    "SESSION RESET COMPLETE. Resume immediately from `memory/context-compaction-latest.md`."
                )
                
                # Clear pending flag
                if agent_id in self.pending_compaction:
                    del self.pending_compaction[agent_id]
                    
            except Exception as e:
                logger.error(f"Failed to reset session: {e}")
        else:
            logger.error(f"Session file not found: {jsonl_path}")

    async def watch_artifacts(self):
        """Poll for compaction artifacts updates."""
        # watchfiles failed with 'Too many open files' in Docker.
        # Fallback to polling stat.
        
        # Mapping from path to agent_id
        target_files = {} # path -> agent_id
        
        for aid, ws_name in AGENT_WORKSPACE_MAP.items():
            p = OPENCLAW_HOME / ws_name / "memory" / "context-compaction-latest.md"
            # We watch the file path even if it doesn't exist yet
            target_files[str(p)] = aid
        
        logger.info(f"Polling {len(target_files)} artifact paths...")
        
        last_mtime = {}
        
        while True:
            for p_str, agent_id in target_files.items():
                p = Path(p_str)
                if p.exists():
                    try:
                        mtime = p.stat().st_mtime
                        if p_str in last_mtime:
                            if mtime > last_mtime[p_str]:
                                logger.info(f"Artifact updated for {agent_id}. Triggering reset in 5s...")
                                # Update mtime immediately to avoid debounce issues
                                last_mtime[p_str] = mtime
                                # Spawn reset task
                                asyncio.create_task(self._delayed_reset(agent_id))
                        else:
                            # First time seeing it, just track it
                            last_mtime[p_str] = mtime
                    except Exception as e:
                        logger.warning(f"Error checking {p}: {e}")
            
            await asyncio.sleep(5)

    async def _delayed_reset(self, agent_id):
        await asyncio.sleep(5)
        await self.reset_session(agent_id)

    async def run(self):
        await self.connect()
        
        # Run poller and watcher concurrently
        await asyncio.gather(
            self.poll_loop(),
            self.watch_artifacts()
        )

    async def poll_loop(self):
        while True:
            try:
                await self.check_context_usage()
            except Exception as e:
                logger.error(f"Poll error: {e}")
            await asyncio.sleep(CHECK_INTERVAL_S)

if __name__ == "__main__":
    monitor = ContextMonitor()
    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        pass
