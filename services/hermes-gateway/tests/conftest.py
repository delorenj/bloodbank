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
gateway_filters = types.ModuleType("gateway.response_filters")
gateway_filters.is_intentional_silence_response = lambda text: (
    " ".join(text.strip().upper().split()) in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"}
)

hermes_cli = types.ModuleType("hermes_cli")
hermes_cli.__path__ = []
hermes_profiles = types.ModuleType("hermes_cli.profiles")
hermes_profiles.normalize_profile_name = lambda name: str(name).strip().lower()


def validate_profile_name(name):
    if not name or not all(c.islower() or c.isdigit() or c in "_-" for c in name):
        raise ValueError("invalid profile")


hermes_profiles.validate_profile_name = validate_profile_name
hermes_profiles.profile_exists = lambda name: name in KNOWN_PROFILES
hermes_profiles.get_profile_dir = lambda name: Path("/fixture") / name

hermes_constants = types.ModuleType("hermes_constants")
hermes_constants.get_hermes_home = lambda: Path("/fixture/host")


class FixtureRunner:
    config = types.SimpleNamespace(multiplex_profiles=True)

    def __init__(self, handler):
        self.handler = handler

    def _get_proxy_url(self):
        return None

    def _resolve_profile_home_for_source(self, source):
        return hermes_profiles.get_profile_dir(source.profile)

    def _resolve_session_agent_runtime(self, **_kwargs):
        return "fixture-model", {"api_mode": "chat_completions"}

    def _resolve_turn_agent_config(self, _text, _model, runtime):
        return {"runtime": runtime}

    async def handle_message(self, event):
        with sys.modules["gateway.run"]._profile_runtime_scope(
            self._resolve_profile_home_for_source(event.source)
        ):
            return await self.handler(event)


gateway_run = types.ModuleType("gateway.run")
gateway_run._load_gateway_config = dict
gateway_run._profile_runtime_scope = lambda _home: __import__("contextlib").nullcontext()
hermes_plugins = types.ModuleType("hermes_cli.plugins")
hermes_plugins.discover_plugins = lambda: None
hermes_plugins.get_plugin_manager = lambda: types.SimpleNamespace(
    iter_hook_callbacks=lambda name: (getattr(
        sys.modules["bloodbank_hermes_gateway.plugin"], name
    ),)
)

sys.modules.setdefault("gateway", gateway)
sys.modules.setdefault("gateway.config", gateway_config)
sys.modules.setdefault("gateway.platforms", gateway_platforms)
sys.modules.setdefault("gateway.platforms.base", gateway_base)
sys.modules.setdefault("gateway.response_filters", gateway_filters)
sys.modules.setdefault("hermes_cli", hermes_cli)
sys.modules.setdefault("hermes_cli.profiles", hermes_profiles)
sys.modules.setdefault("hermes_constants", hermes_constants)
sys.modules.setdefault("hermes_cli.plugins", hermes_plugins)
sys.modules.setdefault("gateway.run", gateway_run)


@pytest.fixture(autouse=True)
def handler_runtime_fixture(monkeypatch):
    from bloodbank_hermes_gateway.adapter import BloodbankAdapter

    original = BloodbankAdapter.set_message_handler

    def install(adapter, handler):
        if getattr(handler, "__self__", None) is None and not getattr(
            handler, "native_handler", False
        ):
            handler = FixtureRunner(handler).handle_message
        original(adapter, handler)

    monkeypatch.setattr(BloodbankAdapter, "set_message_handler", install)


@pytest.fixture
def valid_command():
    command_id = str(uuid.uuid4())
    correlation_id = str(uuid.uuid4())
    return {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": "urn:33god:service:test-pm",
        "type": "bloodbank.agent.invocation.start",
        "subject": "agents/bloodbank-pm/invocations/start",
        "time": "2026-07-31T12:00:00Z",
        "datacontenttype": "application/json",
        "dataschema": "apicurio://holyfields/bloodbank.agent.invocation.start/versions/1",
        "correlationid": correlation_id,
        "causationid": None,
        "producer": "test-pm",
        "service": "bloodbank",
        "domain": "agent",
        "schemaref": "bloodbank.agent.invocation.start.v1",
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
