"""Hermes platform adapter backed by the Bloodbank JetStream command lane."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
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
    BATCH,
    COMMAND_STREAM,
    EVENT_STREAM,
    INTERACTIVE,
    CommandInvalid,
    Invocation,
    LaneSpec,
    ProfileResolver,
    RegistryInvalid,
    RouteInvalid,
    decode_command,
    started_events,
    terminal_events,
)
from .execution_state import ExecutionRecord, ExecutionStateStore, envelope_digest

logger = logging.getLogger(__name__)
ROUTE_REJECTION_REASON = "route_policy_invalid_before_dispatch"
ROUTE_REJECTION_MESSAGE = "Hermes route became ineligible before dispatch"


def _bounded_int(
    extra: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
    *,
    prefix: str = "bloodbank.extra",
) -> int:
    raw = extra.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{prefix}.{key} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{prefix}.{key} must be between {minimum} and {maximum}")
    return value


def _bounded_float(
    extra: dict[str, Any],
    key: str,
    default: float,
    minimum: float,
    maximum: float,
    *,
    prefix: str = "bloodbank.extra",
) -> float:
    raw = extra.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{prefix}.{key} must be numeric") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{prefix}.{key} must be between {minimum} and {maximum}")
    return value


def _strict_bool(
    extra: dict[str, Any],
    key: str,
    default: bool = False,
    *,
    prefix: str = "bloodbank.extra",
) -> bool:
    value = extra.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{prefix}.{key} must be true or false")
    return value


@dataclass
class LaneRuntime:
    """One delivery lane: its durable consumer, bounds and live tasks.

    Lanes share nothing at runtime except the NATS connection and the
    execution journal, so a saturated lane cannot delay another lane's fetch.
    """

    spec: LaneSpec
    durable: str
    max_inflight: int
    max_deliver: int
    ack_wait: float
    ack_progress: float
    nak_delay: float
    subscription: Any = None
    task: asyncio.Task[None] | None = None
    inflight: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def name(self) -> str:
        return self.spec.name


def _batch_attr(name: str) -> property:
    """Expose a batch-lane setting under its original flat adapter name."""

    return property(
        lambda self: getattr(self._batch, name),
        lambda self, value: setattr(self._batch, name, value),
    )


@dataclass
class PendingInvocation:
    invocation: Invocation
    broker_message: Any
    completion: asyncio.Future[None]
    envelope_digest: str


class BloodbankAdapter(BasePlatformAdapter):
    """Durable command consumers (one per lane) shared by a multiplex gateway.

    The batch lane (``invocation.start``) is always present and configured by
    the flat ``extra`` keys.  The interactive lane (``invocation.invoke``) is
    added by ``extra.interactive.enabled: true`` and has its own durable
    consumer and in-flight slots.
    """

    supports_async_delivery = False
    interactive_resume = False
    allow_update_command = False

    # Flat names predate lanes and always address the batch lane.
    durable_name = _batch_attr("durable")
    max_inflight = _batch_attr("max_inflight")
    max_deliver = _batch_attr("max_deliver")
    ack_wait_seconds = _batch_attr("ack_wait")
    ack_progress_seconds = _batch_attr("ack_progress")
    nak_delay_seconds = _batch_attr("nak_delay")
    _subscription = _batch_attr("subscription")
    _consumer_task = _batch_attr("task")
    _inflight_tasks = _batch_attr("inflight")

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
        durable_name = str(
            extra.get("durable_name", "bloodbank-hermes-gateway")
        ).strip()
        if not durable_name:
            raise ValueError("bloodbank.extra.durable_name must be non-empty")

        self.max_command_bytes = _bounded_int(
            extra, "max_command_bytes", 262_144, 1_024, 1_048_576
        )
        ack_wait = _bounded_float(extra, "ack_wait_seconds", 90.0, 10.0, 3_600.0)
        ack_progress = _bounded_float(
            extra, "ack_progress_seconds", 20.0, 1.0, 1_200.0
        )
        if ack_progress >= ack_wait:
            raise ValueError("ack_progress_seconds must be less than ack_wait_seconds")
        self.fetch_timeout_seconds = _bounded_float(
            extra, "fetch_timeout_seconds", 1.0, 0.1, 30.0
        )
        self.publish_timeout_seconds = _bounded_float(
            extra, "publish_timeout_seconds", 5.0, 0.1, 60.0
        )
        self._batch = LaneRuntime(
            spec=BATCH,
            durable=durable_name,
            max_inflight=_bounded_int(extra, "max_inflight", 4, 1, 64),
            max_deliver=_bounded_int(extra, "max_deliver", 5, 1, 100),
            ack_wait=ack_wait,
            ack_progress=ack_progress,
            nak_delay=_bounded_float(extra, "nak_delay_seconds", 5.0, 0.0, 300.0),
        )
        self._lanes: list[LaneRuntime] = [self._batch]
        interactive = self._interactive_lane(extra.get("interactive"))
        if interactive is not None:
            if interactive.durable == self._batch.durable:
                raise ValueError(
                    "bloodbank.extra.interactive.durable_name must differ from "
                    "the batch durable_name"
                )
            self._lanes.append(interactive)

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
        self._records: dict[str, PendingInvocation] = {}
        self._execution_claim_lock = asyncio.Lock()
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._conversation_lock_refs: dict[str, int] = {}

    @staticmethod
    def _interactive_lane(raw: Any) -> LaneRuntime | None:
        """Build the interactive lane from ``extra.interactive`` (absent = off)."""

        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("bloodbank.extra.interactive must be a mapping")
        prefix = "bloodbank.extra.interactive"
        if not _strict_bool(raw, "enabled", False, prefix=prefix):
            return None
        durable = str(
            raw.get("durable_name", "bloodbank-hermes-gateway-interactive")
        ).strip()
        if not durable:
            raise ValueError(f"{prefix}.durable_name must be non-empty")
        ack_wait = _bounded_float(
            raw, "ack_wait_seconds", 180.0, 10.0, 3_600.0, prefix=prefix
        )
        ack_progress = _bounded_float(
            raw, "ack_progress_seconds", 30.0, 1.0, 1_200.0, prefix=prefix
        )
        if ack_progress >= ack_wait:
            raise ValueError(
                f"{prefix}.ack_progress_seconds must be less than ack_wait_seconds"
            )
        return LaneRuntime(
            spec=INTERACTIVE,
            durable=durable,
            max_inflight=_bounded_int(raw, "max_inflight", 2, 1, 64, prefix=prefix),
            max_deliver=_bounded_int(raw, "max_deliver", 3, 1, 100, prefix=prefix),
            ack_wait=ack_wait,
            ack_progress=ack_progress,
            nak_delay=_bounded_float(
                raw, "nak_delay_seconds", 2.0, 0.0, 300.0, prefix=prefix
            ),
        )

    @property
    def name(self) -> str:
        return "Bloodbank"

    @property
    def lanes(self) -> tuple[LaneRuntime, ...]:
        return tuple(self._lanes)

    def _lane(self, lane: LaneRuntime | None) -> LaneRuntime:
        return self._batch if lane is None else lane

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
            # Every lane must bind: a half-bound gateway is worse than a
            # loudly failed (and retried) one.
            for lane in self._lanes:
                consumer_config = ConsumerConfig(
                    durable_name=lane.durable,
                    description=f"Hermes host gateway invocation consumer ({lane.name} lane)",
                    deliver_policy=DeliverPolicy.ALL,
                    ack_policy=AckPolicy.EXPLICIT,
                    ack_wait=lane.ack_wait,
                    max_deliver=lane.max_deliver,
                    filter_subject=lane.spec.subject,
                    max_waiting=1,
                    max_ack_pending=lane.max_inflight,
                )
                lane.subscription = await self._js.pull_subscribe(
                    lane.spec.subject,
                    durable=lane.durable,
                    stream=COMMAND_STREAM,
                    config=consumer_config,
                    pending_msgs_limit=lane.max_inflight,
                    pending_bytes_limit=lane.max_inflight * self.max_command_bytes,
                )
                info = await self._js.consumer_info(
                    COMMAND_STREAM,
                    lane.durable,
                    timeout=self.publish_timeout_seconds,
                )
                self._assert_consumer_contract(info.config, lane)
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
        for lane in self._lanes:
            lane.task = asyncio.create_task(
                self._consume_loop(lane),
                name=f"bloodbank-hermes-consumer-{lane.name}",
            )
            logger.info(
                "Bloodbank gateway connected lane=%s durable=%s max_inflight=%d",
                lane.name,
                lane.durable,
                lane.max_inflight,
            )
        return True

    def _assert_consumer_contract(
        self, actual: Any, lane: LaneRuntime | None = None
    ) -> None:
        """Refuse to bind a stale durable with unsafe delivery semantics."""

        lane = self._lane(lane)
        ack_policy = getattr(actual, "ack_policy", None)
        ack_policy = getattr(ack_policy, "value", ack_policy)
        expected = {
            "filter_subject": lane.spec.subject,
            "ack_policy": "explicit",
            "max_ack_pending": lane.max_inflight,
            "max_deliver": lane.max_deliver,
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
        for lane in self._lanes:
            if lane.task is not None:
                lane.task.cancel()
                await asyncio.gather(lane.task, return_exceptions=True)
                lane.task = None

        # Ensure Hermes turns reach their processing-complete hook before the
        # broker waiters are canceled. A canceled turn publishes its terminal
        # failure/turn outcome and may then be acknowledged safely.
        await self.cancel_background_tasks()
        tasks = [task for lane in self._lanes for task in lane.inflight]
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
        for lane in self._lanes:
            lane.inflight.clear()
        await self._close_nats()
        self._release_platform_lock()

    async def _close_nats(self) -> None:
        nc, self._nc = self._nc, None
        self._js = None
        for lane in self._lanes:
            lane.subscription = None
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

    async def _consume_loop(self, lane: LaneRuntime | None = None) -> None:
        lane = self._lane(lane)
        while self._running:
            capacity = lane.max_inflight - len(lane.inflight)
            if capacity <= 0:
                await asyncio.wait(
                    lane.inflight,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                continue
            try:
                messages = await lane.subscription.fetch(
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
                    "Bloodbank durable fetch failed lane=%s error_type=%s",
                    lane.name,
                    type(exc).__name__,
                )
                await asyncio.sleep(min(self.fetch_timeout_seconds, 5.0))
                continue

            for message in messages:
                task = asyncio.create_task(self._handle_broker_message(message, lane))
                lane.inflight.add(task)
                task.add_done_callback(lane.inflight.discard)
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

    async def _handle_broker_message(
        self, message: Any, lane: LaneRuntime | None = None
    ) -> None:
        lane = self._lane(lane)
        progress_task: asyncio.Task[None] | None = None
        record: PendingInvocation | None = None
        persisted: ExecutionRecord | None = None
        started_publish_complete = False
        try:
            envelope = decode_command(
                message.data, max_bytes=self.max_command_bytes, lane=lane.spec
            )
            command_id = envelope["command_id"]
            digest = envelope_digest(envelope)
            # Serialize the short durable-claim section so a concurrent local
            # redelivery cannot observe the same pending row and dispatch a
            # second Hermes turn. Different commands still execute concurrently.
            async with self._execution_claim_lock:
                if command_id in self._records:
                    await message.nak(delay=lane.nak_delay)
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
                    invocation = Invocation.from_envelope(envelope, profile, lane.name)
                    persisted = await asyncio.to_thread(
                        self.execution_state.claim_pending,
                        command_id=command_id,
                        digest=digest,
                        profile=profile,
                        started_events=started_events(invocation),
                        lane=lane.name,
                    )
                    if persisted.envelope_digest != digest:
                        raise CommandInvalid(
                            "command_id collides with a different command envelope"
                        )

                if persisted.state in {"pending", "started"}:
                    invocation = Invocation.from_envelope(
                        envelope, persisted.profile, lane.name
                    )
                    completion = asyncio.get_running_loop().create_future()
                    record = PendingInvocation(invocation, message, completion, digest)
                    self._records[invocation.invocation_id] = record

            if persisted.state == "completed":
                progress_task = asyncio.create_task(self._ack_progress(message, lane))
                await self._publish_many(persisted.started_events)
                await self._publish_many(persisted.terminal_events)
                await message.ack()
                return
            if persisted.state == "rejected_pre_start":
                await message.term()
                return
            if persisted.state in {"rejected_closing", "rejected_closed"}:
                progress_task = asyncio.create_task(self._ack_progress(message, lane))
                invocation = Invocation.from_envelope(
                    envelope, persisted.profile, lane.name
                )
                await self._persist_and_publish_route_rejection(
                    invocation=invocation,
                    digest=digest,
                    persisted=persisted,
                    replay_started=True,
                )
                await message.term()
                return

            assert record is not None
            invocation = record.invocation
            progress_task = asyncio.create_task(self._ack_progress(message, lane))

            lock_key = f"{invocation.profile}:{invocation.thread_id}"
            lock = self._conversation_locks.setdefault(lock_key, asyncio.Lock())
            self._conversation_lock_refs[lock_key] = (
                self._conversation_lock_refs.get(lock_key, 0) + 1
            )
            try:
                async with lock:
                    await self._assert_dispatch_route(invocation)
                    if persisted.state == "pending":
                        persisted = await asyncio.to_thread(
                            self.execution_state.mark_started,
                            command_id=invocation.invocation_id,
                            digest=record.envelope_digest,
                        )
                    if persisted.state != "started":
                        raise RuntimeError(
                            "execution journal is not dispatchable after route check"
                        )
                    await self._publish_many(persisted.started_events)
                    started_publish_complete = True
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
            if record is not None and persisted is not None:
                try:
                    await self._persist_and_publish_route_rejection(
                        invocation=record.invocation,
                        digest=record.envelope_digest,
                        persisted=persisted,
                        replay_started=not started_publish_complete,
                    )
                except Exception as journal_error:
                    logger.error(
                        "Bloodbank route rejection closure could not be completed "
                        "error_type=%s",
                        type(journal_error).__name__,
                    )
                    await message.nak(delay=lane.nak_delay)
                    return
            logger.warning("Terminally rejecting Bloodbank command: %s", exc)
            await message.term()
        except CommandInvalid as exc:
            logger.warning("Terminally rejecting Bloodbank command: %s", exc)
            await message.term()
        except RegistryInvalid as exc:
            logger.error("Bloodbank fleet routing is unavailable: %s", exc)
            await message.nak(delay=lane.nak_delay)
        except asyncio.CancelledError:
            try:
                await message.nak(delay=lane.nak_delay)
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
                await message.nak(delay=lane.nak_delay)
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

    async def _persist_and_publish_route_rejection(
        self,
        *,
        invocation: Invocation,
        digest: str,
        persisted: ExecutionRecord,
        replay_started: bool,
    ) -> ExecutionRecord:
        """Persist rejection evidence and close any lifecycle that may have opened."""

        if persisted.state == "pending":
            return await asyncio.to_thread(
                self.execution_state.reject_pre_start,
                command_id=invocation.invocation_id,
                digest=digest,
                reason=ROUTE_REJECTION_REASON,
            )

        if persisted.state == "started":
            persisted = await asyncio.to_thread(
                self.execution_state.begin_rejection,
                command_id=invocation.invocation_id,
                digest=digest,
                reason=ROUTE_REJECTION_REASON,
            )
        if persisted.state == "rejected_closing":
            if persisted.rejected_at is None:
                raise RuntimeError("rejection intent is missing its stable timestamp")
            events = terminal_events(
                invocation,
                outcome="failure",
                failure_code=ROUTE_REJECTION_REASON,
                failure_message=ROUTE_REJECTION_MESSAGE,
                occurred_at=persisted.rejected_at,
            )
            persisted = await asyncio.to_thread(
                self.execution_state.finalize_rejection,
                command_id=invocation.invocation_id,
                digest=digest,
                terminal_events=events,
            )
        if persisted.state != "rejected_closed":
            raise RuntimeError(
                f"execution journal rejection state {persisted.state!r} is invalid"
            )

        # A previous publication may have failed between the two started
        # events. Replaying both stable envelopes is safe because each publish
        # carries the deterministic event ID as Nats-Msg-Id.
        if replay_started:
            await self._publish_many(persisted.started_events)
        await self._publish_many(persisted.terminal_events)
        return persisted

    async def _ack_progress(
        self, message: Any, lane: LaneRuntime | None = None
    ) -> None:
        lane = self._lane(lane)
        while True:
            await asyncio.sleep(lane.ack_progress)
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
