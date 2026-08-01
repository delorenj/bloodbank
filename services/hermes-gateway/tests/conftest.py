from __future__ import annotations

import asyncio
import sys
import types
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest


KNOWN_PROFILES = {"default", "research", "operations", "bloodbank-pm"}


class Platform(str, Enum):
    BLOODBANK = "bloodbank"


class MessageType(Enum):
    TEXT = "text"


class ProcessingOutcome(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None


@dataclass
class MessageEvent:
    text: str
    message_type: MessageType
    source: object
    raw_message: object = None
    message_id: str | None = None
    internal: bool = False


class BasePlatformAdapter:
    """Minimal execution-compatible fake for standalone plugin tests."""

    def __init__(self, config, platform):
        self.config = config
        self.platform = platform
        self._message_handler = None
        self._running = False
        self._background_tasks = set()
        self.connected = False

    def set_message_handler(self, handler):
        self._message_handler = handler

    def build_source(self, **kwargs):
        return types.SimpleNamespace(platform=self.platform, profile=None, **kwargs)

    async def handle_message(self, event):
        async def run():
            outcome = ProcessingOutcome.SUCCESS
            try:
                response = await self._message_handler(event)
                if response:
                    result = await self.send(event.source.chat_id, response)
                    if not result.success:
                        outcome = ProcessingOutcome.FAILURE
            except asyncio.CancelledError:
                outcome = ProcessingOutcome.CANCELLED
                await self.on_processing_complete(event, outcome)
                raise
            except Exception:
                outcome = ProcessingOutcome.FAILURE
            await self.on_processing_complete(event, outcome)

        task = asyncio.create_task(run())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def cancel_background_tasks(self):
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    def _acquire_platform_lock(self, *_args):
        return True

    def _release_platform_lock(self):
        return None

    def _mark_connected(self):
        self.connected = True

    def _mark_disconnected(self):
        self.connected = False

    def _set_fatal_error(self, *_args, **_kwargs):
        return None


gateway = types.ModuleType("gateway")
gateway.__path__ = []
gateway_config = types.ModuleType("gateway.config")
gateway_config.Platform = Platform
gateway_platforms = types.ModuleType("gateway.platforms")
gateway_platforms.__path__ = []
gateway_base = types.ModuleType("gateway.platforms.base")
gateway_base.BasePlatformAdapter = BasePlatformAdapter
gateway_base.MessageEvent = MessageEvent
gateway_base.MessageType = MessageType
gateway_base.ProcessingOutcome = ProcessingOutcome
gateway_base.SendResult = SendResult

hermes_cli = types.ModuleType("hermes_cli")
hermes_cli.__path__ = []
hermes_profiles = types.ModuleType("hermes_cli.profiles")
hermes_profiles.normalize_profile_name = lambda name: str(name).strip().lower()


def validate_profile_name(name):
    if not name or not all(c.islower() or c.isdigit() or c in "_-" for c in name):
        raise ValueError("invalid profile")


hermes_profiles.validate_profile_name = validate_profile_name
hermes_profiles.profile_exists = lambda name: name in KNOWN_PROFILES

sys.modules.setdefault("gateway", gateway)
sys.modules.setdefault("gateway.config", gateway_config)
sys.modules.setdefault("gateway.platforms", gateway_platforms)
sys.modules.setdefault("gateway.platforms.base", gateway_base)
sys.modules.setdefault("hermes_cli", hermes_cli)
sys.modules.setdefault("hermes_cli.profiles", hermes_profiles)


@pytest.fixture
def valid_command():
    command_id = str(uuid.uuid4())
    correlation_id = str(uuid.uuid4())
    return {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": "urn:33god:service:test-pm",
        "type": "bloodbank.v1.agent.invocation.start",
        "subject": "agents/bloodbank-pm/invocations/start",
        "time": "2026-07-31T12:00:00Z",
        "datacontenttype": "application/json",
        "dataschema": "apicurio://holyfields/bloodbank.v1.agent.invocation.start/versions/1",
        "correlationid": correlation_id,
        "causationid": None,
        "producer": "test-pm",
        "service": "bloodbank",
        "domain": "agent",
        "schemaref": "bloodbank.v1.agent.invocation.start.v1",
        "kind": "command",
        "actor": {"type": "agent_api", "agent_id": "test-pm"},
        "command_id": command_id,
        "idempotency_key": f"agent.invocation.start:turn:{command_id}",
        "delivery": "single_consumer",
        "data": {
            "target_agent_id": "bloodbank-pm",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "prompt": "Review the current Bloodbank queue.",
            "context": {"priority": "normal"},
        },
    }


@pytest.fixture
def repo_root():
    return Path(__file__).resolve().parents[3]
