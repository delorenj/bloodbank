"""Hermes platform adapter backed by the Bloodbank JetStream command lane."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from hermes_cli.profiles import (
    normalize_profile_name,
    profile_exists,
    validate_profile_name,
)

from .contract import (
    COMMAND_STREAM,
    COMMAND_SUBJECT,
    EVENT_STREAM,
    CommandInvalid,
    Invocation,
    ProfileResolver,
    RegistryInvalid,
    RouteInvalid,
    decode_command,
    started_events,
    terminal_events,
)
from .execution_state import ExecutionStateStore, envelope_digest

logger = logging.getLogger(__name__)
ROUTE_REJECTION_REASON = "route_policy_invalid_before_dispatch"


def _bounded_int(
    extra: dict[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    raw = extra.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bloodbank.extra.{key} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(
            f"bloodbank.extra.{key} must be between {minimum} and {maximum}"
        )
    return value


def _bounded_float(
    extra: dict[str, Any], key: str, default: float, minimum: float, maximum: float
) -> float:
    raw = extra.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bloodbank.extra.{key} must be numeric") from exc
    if value < minimum or value > maximum:
        raise ValueError(
            f"bloodbank.extra.{key} must be between {minimum} and {maximum}"
        )
    return value


def _strict_bool(extra: dict[str, Any], key: str, default: bool = False) -> bool:
    value = extra.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"bloodbank.extra.{key} must be true or false")
    return value


@dataclass
class PendingInvocation:
    invocation: Invocation
    broker_message: Any
    completion: asyncio.Future[None]
    envelope_digest: str


class BloodbankAdapter(BasePlatformAdapter):
    """A single durable command consumer shared by a multiplex Hermes gateway."""

    supports_async_delivery = False
    interactive_resume = False
    allow_update_command = False

    def __init__(self, config: Any) -> None:
        super().__init__(config=config, platform=Platform("bloodbank"))
        extra = getattr(config, "extra", {}) or {}
        if not isinstance(extra, dict):
            raise ValueError("bloodbank platform extra configuration must be a mapping")

        self.nats_url = os.getenv("BLOODBANK_NATS_URL") or str(
            extra.get("nats_url", "nats://127.0.0.1:4222")
        )
        self.nats_credentials = os.getenv("BLOODBANK_NATS_CREDS") or str(
            extra.get("credentials_file", "")
        )
        self.durable_name = str(
            extra.get("durable_name", "bloodbank-hermes-gateway-v1")
        ).strip()
        if not self.durable_name:
            raise ValueError("bloodbank.extra.durable_name must be non-empty")

        self.max_inflight = _bounded_int(extra, "max_inflight", 4, 1, 64)
        self.max_deliver = _bounded_int(extra, "max_deliver", 5, 1, 100)
        self.max_command_bytes = _bounded_int(
            extra, "max_command_bytes", 262_144, 1_024, 1_048_576
        )
        self.ack_wait_seconds = _bounded_float(
            extra, "ack_wait_seconds", 90.0, 10.0, 3_600.0
        )
        self.ack_progress_seconds = _bounded_float(
            extra, "ack_progress_seconds", 20.0, 1.0, 1_200.0
        )
        if self.ack_progress_seconds >= self.ack_wait_seconds:
            raise ValueError("ack_progress_seconds must be less than ack_wait_seconds")
        self.fetch_timeout_seconds = _bounded_float(
            extra, "fetch_timeout_seconds", 1.0, 0.1, 30.0
        )
        self.publish_timeout_seconds = _bounded_float(
            extra, "publish_timeout_seconds", 5.0, 0.1, 60.0
        )
        self.nak_delay_seconds = _bounded_float(
            extra, "nak_delay_seconds", 5.0, 0.0, 300.0
        )

        target_profiles = extra.get("target_profiles", {}) or {}
        if not isinstance(target_profiles, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in target_profiles.items()
        ):
            raise ValueError(
                "bloodbank.extra.target_profiles must map strings to strings"
            )
        registry_path = Path(
            os.path.expanduser(
                str(extra.get("fleet_registry", "~/.hermes/agents-registry.yaml"))
            )
        )
        self.profile_resolver = ProfileResolver(
            target_profiles=target_profiles,
            fleet_registry=registry_path,
            allow_direct_profile_targets=_strict_bool(
                extra, "allow_direct_profile_targets", False
            ),
            normalize_profile_name=normalize_profile_name,
            validate_profile_name=validate_profile_name,
            profile_exists=profile_exists,
        )
        execution_state_path = Path(
            os.path.expanduser(
                str(
                    extra.get(
                        "execution_state_file",
                        "~/.hermes/bloodbank-hermes-gateway-state.sqlite3",
                    )
                )
            )
        )
        self.execution_state = ExecutionStateStore(execution_state_path)

        self._nc: Any = None
        self._js: Any = None
        self._subscription: Any = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._inflight_tasks: set[asyncio.Task[None]] = set()
        self._records: dict[str, PendingInvocation] = {}
        self._execution_claim_lock = asyncio.Lock()
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._conversation_lock_refs: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "Bloodbank"

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        del is_reconnect
        if not self._acquire_platform_lock(
            "bloodbank-hermes-gateway",
            self.durable_name,
            f"Bloodbank durable consumer {self.durable_name}",
        ):
            return False

        try:
            import nats
            from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

            connect_kwargs: dict[str, Any] = {
                "servers": [self.nats_url],
                "name": f"hermes-{self.durable_name}",
                "connect_timeout": 5,
                "max_reconnect_attempts": -1,
            }
            if self.nats_credentials:
                connect_kwargs["user_credentials"] = self.nats_credentials
            self._nc = await nats.connect(**connect_kwargs)
            self._js = self._nc.jetstream()
            consumer_config = ConsumerConfig(
                durable_name=self.durable_name,
                description="Hermes host gateway invocation consumer",
                deliver_policy=DeliverPolicy.ALL,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=self.ack_wait_seconds,
                max_deliver=self.max_deliver,
                filter_subject=COMMAND_SUBJECT,
                max_waiting=1,
                max_ack_pending=self.max_inflight,
            )
            self._subscription = await self._js.pull_subscribe(
                COMMAND_SUBJECT,
                durable=self.durable_name,
                stream=COMMAND_STREAM,
                config=consumer_config,
                pending_msgs_limit=self.max_inflight,
                pending_bytes_limit=self.max_inflight * self.max_command_bytes,
            )
            info = await self._js.consumer_info(
                COMMAND_STREAM,
                self.durable_name,
                timeout=self.publish_timeout_seconds,
            )
            self._assert_consumer_contract(info.config)
        except Exception as exc:
            logger.error(
                "Bloodbank gateway could not connect to JetStream error_type=%s",
                type(exc).__name__,
            )
            self._set_fatal_error(
                "bloodbank_connect_failed",
                "Bloodbank JetStream connection or durable setup failed",
                retryable=True,
            )
            await self._close_nats()
            self._release_platform_lock()
            return False

        self._running = True
        self._mark_connected()
        self._consumer_task = asyncio.create_task(
            self._consume_loop(), name="bloodbank-hermes-consumer"
        )
        logger.info(
            "Bloodbank gateway connected durable=%s max_inflight=%d",
            self.durable_name,
            self.max_inflight,
        )
        return True

    def _assert_consumer_contract(self, actual: Any) -> None:
        """Refuse to bind a stale durable with unsafe delivery semantics."""

        ack_policy = getattr(actual, "ack_policy", None)
        ack_policy = getattr(ack_policy, "value", ack_policy)
        expected = {
            "filter_subject": COMMAND_SUBJECT,
            "ack_policy": "explicit",
            "max_ack_pending": self.max_inflight,
            "max_deliver": self.max_deliver,
        }
        observed = {
            "filter_subject": getattr(actual, "filter_subject", None),
            "ack_policy": ack_policy,
            "max_ack_pending": getattr(actual, "max_ack_pending", None),
            "max_deliver": getattr(actual, "max_deliver", None),
        }
        if observed != expected:
            raise RuntimeError(
                "existing Bloodbank durable consumer does not match the configured "
                "canonical subject and bounded delivery contract"
            )

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            await asyncio.gather(self._consumer_task, return_exceptions=True)
            self._consumer_task = None

        # Ensure Hermes turns reach their processing-complete hook before the
        # broker waiters are canceled. A canceled turn publishes its terminal
        # failure/turn outcome and may then be acknowledged safely.
        await self.cancel_background_tasks()
        tasks = list(self._inflight_tasks)
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=5.0
                )
            except asyncio.TimeoutError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight_tasks.clear()
        await self._close_nats()
        self._release_platform_lock()

    async def _close_nats(self) -> None:
        nc, self._nc = self._nc, None
        self._js = None
        self._subscription = None
        if nc is None:
            return
        try:
            # A pull subscription can keep nats-py's drain pending longer than
            # Hermes' adapter shutdown budget. Bound it and fall back to close
            # so routine service restarts do not look like forced failures.
            await asyncio.wait_for(
                nc.drain(), timeout=min(self.publish_timeout_seconds, 3.0)
            )
        except Exception:
            try:
                await nc.close()
            except Exception:
                pass

    async def _consume_loop(self) -> None:
        while self._running:
            capacity = self.max_inflight - len(self._inflight_tasks)
            if capacity <= 0:
                await asyncio.wait(
                    self._inflight_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                continue
            try:
                messages = await self._subscription.fetch(
                    capacity, timeout=self.fetch_timeout_seconds
                )
            # nats-py may surface an idle pull as either its own timeout class
            # or asyncio's timeout depending on which fetch path completed.
            # Both mean "no command available", not a broker failure.
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Bloodbank durable fetch failed error_type=%s",
                    type(exc).__name__,
                )
                await asyncio.sleep(min(self.fetch_timeout_seconds, 5.0))
                continue

            for message in messages:
                task = asyncio.create_task(self._handle_broker_message(message))
                self._inflight_tasks.add(task)
                task.add_done_callback(self._inflight_tasks.discard)
                task.add_done_callback(self._log_task_failure)

    @staticmethod
    def _log_task_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "Bloodbank command task escaped error_type=%s",
                type(error).__name__,
            )

    async def _handle_broker_message(self, message: Any) -> None:
        progress_task: asyncio.Task[None] | None = None
        record: PendingInvocation | None = None
        try:
            envelope = decode_command(message.data, max_bytes=self.max_command_bytes)
            command_id = envelope["command_id"]
            digest = envelope_digest(envelope)
            # Serialize the short durable-claim section so a concurrent local
            # redelivery cannot observe the same pending row and dispatch a
            # second Hermes turn. Different commands still execute concurrently.
            async with self._execution_claim_lock:
                if command_id in self._records:
                    await message.nak(delay=self.nak_delay_seconds)
                    return

                persisted = await asyncio.to_thread(
                    self.execution_state.get, command_id
                )
                if persisted is not None and persisted.envelope_digest != digest:
                    raise CommandInvalid(
                        "command_id collides with a different command envelope"
                    )
                if persisted is None:
                    profile = await asyncio.to_thread(
                        self.profile_resolver.resolve,
                        envelope["data"]["target_agent_id"],
                    )
                    invocation = Invocation.from_envelope(envelope, profile)
                    persisted = await asyncio.to_thread(
                        self.execution_state.claim_pending,
                        command_id=command_id,
                        digest=digest,
                        profile=profile,
                        started_events=started_events(invocation),
                    )
                    if persisted.envelope_digest != digest:
                        raise CommandInvalid(
                            "command_id collides with a different command envelope"
                        )

                if persisted.state == "pending":
                    invocation = Invocation.from_envelope(envelope, persisted.profile)
                    completion = asyncio.get_running_loop().create_future()
                    record = PendingInvocation(invocation, message, completion, digest)
                    self._records[invocation.invocation_id] = record

            if persisted.state == "completed":
                progress_task = asyncio.create_task(self._ack_progress(message))
                await self._publish_many(persisted.started_events)
                await self._publish_many(persisted.terminal_events)
                await message.ack()
                return
            if persisted.state == "rejected":
                await message.term()
                return

            assert record is not None
            invocation = record.invocation
            progress_task = asyncio.create_task(self._ack_progress(message))

            lock_key = f"{invocation.profile}:{invocation.thread_id}"
            lock = self._conversation_locks.setdefault(lock_key, asyncio.Lock())
            self._conversation_lock_refs[lock_key] = (
                self._conversation_lock_refs.get(lock_key, 0) + 1
            )
            try:
                async with lock:
                    await self._assert_dispatch_route(invocation)
                    await self._publish_many(persisted.started_events)
                    if self._message_handler is None:
                        raise RuntimeError("Hermes message handler is not installed")

                    source = self.build_source(
                        chat_id=invocation.thread_id,
                        chat_name=invocation.target_agent_id,
                        chat_type="dm",
                        user_id=invocation.envelope["actor"]["agent_id"],
                        user_name=invocation.envelope["producer"],
                        message_id=invocation.envelope["command_id"],
                        role_authorized=True,
                    )
                    source.profile = invocation.profile
                    event = MessageEvent(
                        text=invocation.prompt,
                        message_type=MessageType.TEXT,
                        source=source,
                        raw_message={
                            "command_id": invocation.envelope["command_id"],
                            "correlationid": invocation.envelope["correlationid"],
                            "causationid": invocation.envelope.get("causationid"),
                            "idempotency_key": invocation.envelope["idempotency_key"],
                            "context": invocation.envelope["data"].get("context"),
                        },
                        message_id=invocation.envelope["command_id"],
                        internal=True,
                    )
                    # Re-read the registry after all pre-dispatch awaits. A
                    # claimed or restarted pending command must not execute on
                    # a route disabled while lifecycle publication was in flight.
                    await self._assert_dispatch_route(invocation)
                    await super().handle_message(event)
                    await completion
            finally:
                remaining = self._conversation_lock_refs.get(lock_key, 1) - 1
                if remaining <= 0:
                    self._conversation_lock_refs.pop(lock_key, None)
                    self._conversation_locks.pop(lock_key, None)
                else:
                    self._conversation_lock_refs[lock_key] = remaining

            await message.ack()
        except RouteInvalid as exc:
            if record is not None:
                try:
                    await asyncio.to_thread(
                        self.execution_state.mark_rejected,
                        command_id=record.invocation.invocation_id,
                        digest=record.envelope_digest,
                        reason=ROUTE_REJECTION_REASON,
                    )
                except Exception as journal_error:
                    logger.error(
                        "Bloodbank route rejection could not be persisted "
                        "error_type=%s",
                        type(journal_error).__name__,
                    )
                    await message.nak(delay=self.nak_delay_seconds)
                    return
            logger.warning("Terminally rejecting Bloodbank command: %s", exc)
            await message.term()
        except CommandInvalid as exc:
            logger.warning("Terminally rejecting Bloodbank command: %s", exc)
            await message.term()
        except RegistryInvalid as exc:
            logger.error("Bloodbank fleet routing is unavailable: %s", exc)
            await message.nak(delay=self.nak_delay_seconds)
        except asyncio.CancelledError:
            try:
                await message.nak(delay=self.nak_delay_seconds)
            except Exception:
                pass
            raise
        except Exception as exc:
            logger.error(
                "Bloodbank command processing failed before acknowledgement "
                "error_type=%s",
                type(exc).__name__,
            )
            try:
                await message.nak(delay=self.nak_delay_seconds)
            except Exception:
                logger.warning("Bloodbank command NAK failed", exc_info=True)
        finally:
            if progress_task is not None:
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            if record is not None:
                self._records.pop(record.invocation.invocation_id, None)

    async def _assert_dispatch_route(self, invocation: Invocation) -> None:
        resolved_profile = await asyncio.to_thread(
            self.profile_resolver.resolve,
            invocation.target_agent_id,
        )
        if resolved_profile != invocation.profile:
            raise RouteInvalid(
                f"target_agent_id {invocation.target_agent_id!r} changed profile "
                "after durable claim"
            )

    async def _ack_progress(self, message: Any) -> None:
        while True:
            await asyncio.sleep(self.ack_progress_seconds)
            try:
                await message.in_progress()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Bloodbank in-progress acknowledgement failed", exc_info=True
                )

    async def _publish_many(self, events: tuple[dict[str, Any], ...]) -> None:
        for envelope in events:
            payload = json.dumps(
                envelope, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            await self._js.publish(
                envelope["subject"],
                payload,
                timeout=self.publish_timeout_seconds,
                stream=EVENT_STREAM,
                headers={"Nats-Msg-Id": envelope["id"]},
            )

    async def on_processing_complete(
        self, event: MessageEvent, outcome: ProcessingOutcome
    ) -> None:
        record = self._records.get(str(event.message_id or ""))
        if record is None or record.completion.done():
            return
        result = getattr(outcome, "value", str(outcome)).lower()
        terminal_outcome = (
            "success"
            if result == "success"
            else "cancelled"
            if result in {"cancelled", "canceled"}
            else "failure"
        )
        try:
            events = terminal_events(record.invocation, outcome=terminal_outcome)
            await asyncio.to_thread(
                self.execution_state.mark_completed,
                command_id=record.invocation.invocation_id,
                digest=record.envelope_digest,
                outcome=terminal_outcome,
                terminal_events=events,
            )
            await self._publish_many(events)
        except Exception as exc:
            record.completion.set_exception(exc)
            raise
        else:
            record.completion.set_result(None)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        del chat_id, content, reply_to, metadata
        return SendResult(success=True, message_id=str(uuid.uuid4()))

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": str(chat_id), "type": "dm"}


def check_requirements() -> bool:
    try:
        import nats  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config: Any) -> bool:
    try:
        extra = getattr(config, "extra", {}) or {}
        if not isinstance(extra, dict):
            return False
        target_profiles = extra.get("target_profiles", {}) or {}
        if not isinstance(target_profiles, dict):
            return False
        if any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in target_profiles.items()
        ):
            return False
        return True
    except Exception:
        return False


def is_connected(config: Any) -> bool:
    return bool(getattr(config, "enabled", False)) and validate_config(config)


def register(ctx: Any) -> None:
    ctx.register_platform(
        name="bloodbank",
        label="Bloodbank",
        adapter_factory=lambda cfg: BloodbankAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[],
        install_hint="Install the Bloodbank Hermes gateway plugin with its nats-py dependency",
        allow_update_command=False,
        emoji="🩸",
        platform_hint=(
            "This is an internal Bloodbank command invocation. Execute the supplied "
            "prompt for the routed Hermes profile; no interactive human is present."
        ),
    )
