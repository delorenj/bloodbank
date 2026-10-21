"""Hermes platform adapter backed by the Bloodbank JetStream command lane."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
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
    TURN_TIMEOUT_CODE,
    CommandInvalid,
    Invocation,
    LaneSpec,
    ProfileResolver,
    RegistryInvalid,
    RouteInvalid,
    decode_command,
    progress_event,
    progress_text,
    started_events,
    terminal_events,
)
from .execution_state import ExecutionRecord, ExecutionStateStore, envelope_digest
from .refusals import NOT_RUN, HermesNotice, NoticeClassifier

logger = logging.getLogger(__name__)
ROUTE_REJECTION_REASON = "route_policy_invalid_before_dispatch"
ROUTE_REJECTION_MESSAGE = "Hermes route became ineligible before dispatch"
# Failed-fact reasons when Hermes did not run a command as asked (see refusals.py):
# refused = no turn ran (closed only once no delivery is left, or when a retry
# cannot help); turn_error = a turn ran and Hermes ended it with an error notice.
HERMES_REFUSED_CODE = "hermes_refused"
HERMES_TURN_ERROR_CODE = "hermes_turn_error"
# How often a lane paused by a Hermes drain re-reads the runner's state.
DRAIN_POLL_SECONDS = 1.0
_NOT_ACCEPTING_TEXT = {
    "restarting": "restarting (draining in-flight turns)",
    "shutting_down": "shutting down (draining in-flight turns)",
    "external_drain": "draining for a maintenance action",
}


class TurnNotRun(Exception):
    """Hermes did not run the command's turn: retry it, never close it as done."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


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


DEFAULT_INTERACTIVE_TURN_TIMEOUT_SECONDS = 300.0
MAX_TURN_TIMEOUT_SECONDS = 86_400.0
# How long the gateway waits for Hermes to unwind a stopped turn before it
# reports the turn as not confirmed stopped.
STOP_BUDGET_SECONDS = 10.0


def _turn_timeout(
    extra: dict[str, Any],
    default: float | None,
    *,
    prefix: str = "bloodbank.extra",
) -> float | None:
    """Per-turn wall-clock seconds: absent = ``default``, otherwise a strict
    positive finite number (not a bool, not a numeric string, not null)."""

    if "turn_timeout_seconds" not in extra:
        return default
    raw = extra["turn_timeout_seconds"]
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or raw <= 0
        or raw > MAX_TURN_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"{prefix}.turn_timeout_seconds must be a positive number "
            f"no greater than {int(MAX_TURN_TIMEOUT_SECONDS)}"
        )
    return float(raw)


def _seconds(value: float) -> str:
    """Human seconds: whole when it is whole, else one decimal."""

    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


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
    # Wall-clock budget for one Hermes turn on this lane; None = unlimited.
    turn_timeout: float | None = None
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
    # The MessageEvent handed to Hermes (its source addresses the session a
    # timeout must stop).
    event: Any = None
    # Set synchronously by whichever path owns the terminal outcome first
    # (Hermes' processing-complete hook or the turn timeout); the other path
    # then stands down, so a late completion can never double-publish.
    finalizing: bool = False
    # Monotonic dispatch time (the turn's clock) and the last progress seq
    # published for this command (agent.invocation.progressed, 1..n).
    dispatched_at: float | None = None
    progress_seq: int = 0
    # First chunk of the turn's final reply (the non-interim send anchored to
    # the command id): how a Hermes refusal notice is told from an answer.
    final_reply: str | None = None


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
    # Nothing this adapter "sends" can be edited (send() delivers nothing).
    # Declaring it makes Hermes skip token streaming for Bloodbank turns: an
    # editing platform gets the reply streamed as a first chunk plus edits,
    # and a mid-turn commentary as that chunk plus an unanchored tail flush.
    # Without editing, every interim assistant message arrives as ONE whole
    # send() carrying `_interim_send` and the command id as its reply anchor,
    # which is what agent.invocation.progressed is built from.
    SUPPORTS_MESSAGE_EDITING = False

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
            # Batch turns (ticket grooming, delegation) are legitimately long:
            # no wall clock unless the operator opts in.
            turn_timeout=_turn_timeout(extra, None),
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
        self.drain_poll_seconds = DRAIN_POLL_SECONDS
        self._notices = NoticeClassifier()
        # lane name -> why it stopped fetching (logged once per pause).
        self._paused_lanes: dict[str, str] = {}

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
            max_deliver=_bounded_int(raw, "max_deliver", 8, 1, 100, prefix=prefix),
            ack_wait=ack_wait,
            ack_progress=ack_progress,
            nak_delay=_bounded_float(
                raw, "nak_delay_seconds", 3.0, 0.0, 300.0, prefix=prefix
            ),
            turn_timeout=_turn_timeout(
                raw, DEFAULT_INTERACTIVE_TURN_TIMEOUT_SECONDS, prefix=prefix
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
        """Pull this lane's commands until disconnect, surviving unexpected errors.

        Each lane has its own task and nothing else watches it: if an unexpected
        exception ended the task the gateway would stay "connected" while the lane
        silently stopped pulling (voice memos then fall back to the slow lane).
        Anything but cancellation is logged and the loop resumes."""

        lane = self._lane(lane)
        while self._running:
            try:
                await self._consume_once(lane)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Bloodbank lane consume loop crashed lane=%s error_type=%s; resuming",
                    lane.name,
                    type(exc).__name__,
                )
                await asyncio.sleep(1.0)

    def _gateway_not_accepting(self) -> str | None:
        """Why the Hermes runner refuses new turns right now, or None when it takes them.

        A reload/restart (SIGUSR1) or shutdown keeps every adapter connected while
        Hermes drains in-flight turns (up to restart_after_turn_timeout, 30 min by
        default) and answers each new message with a "not accepting new work"
        notice that the base adapter reports as a successful turn. An external
        drain (.drain_request.json) refuses new turns the same way."""

        runner = getattr(self, "gateway_runner", None)
        if runner is None:
            return None
        if getattr(runner, "_restart_requested", False) is True:
            return "restarting"
        if getattr(runner, "_draining", False) is True:
            return "shutting_down"
        if getattr(runner, "_external_drain_active", False) is True:
            return "external_drain"
        return None

    def _refuse_if_not_accepting(self) -> None:
        reason = self._gateway_not_accepting()
        if reason is not None:
            raise TurnNotRun(
                f"gateway_{reason}",
                f"Hermes gateway is {_NOT_ACCEPTING_TEXT[reason]}; not dispatched",
            )

    def _pause_lane(self, lane: LaneRuntime, reason: str) -> None:
        if self._paused_lanes.get(lane.name) == reason:
            return
        self._paused_lanes[lane.name] = reason
        logger.warning(
            "Bloodbank lane=%s stopped fetching: Hermes gateway is %s and refuses "
            "new turns; commands stay queued on durable %s and run when the "
            "gateway is back",
            lane.name,
            _NOT_ACCEPTING_TEXT[reason],
            lane.durable,
        )

    def _resume_lane(self, lane: LaneRuntime) -> None:
        if self._paused_lanes.pop(lane.name, None) is not None:
            logger.info(
                "Bloodbank lane=%s resumed fetching: Hermes accepts new turns again",
                lane.name,
            )

    async def _consume_once(self, lane: LaneRuntime) -> None:
        """One fetch cycle: wait for a free slot, pull, start a task per command.

        While Hermes refuses new turns the lane does not pull at all: commands
        stay in JetStream (no delivery, so no max_deliver budget is spent however
        long the drain lasts) and the next process fetches them."""

        reason = self._gateway_not_accepting()
        if reason is not None:
            self._pause_lane(lane, reason)
            await asyncio.sleep(self.drain_poll_seconds)
            return
        self._resume_lane(lane)
        capacity = lane.max_inflight - len(lane.inflight)
        if capacity <= 0:
            await asyncio.wait(lane.inflight, return_when=asyncio.FIRST_COMPLETED)
            return
        try:
            messages = await lane.subscription.fetch(
                capacity, timeout=self.fetch_timeout_seconds
            )
        # nats-py may surface an idle pull as either its own timeout class
        # or asyncio's timeout depending on which fetch path completed.
        # Both mean "no command available", not a broker failure.
        except TimeoutError:
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Bloodbank durable fetch failed lane=%s error_type=%s",
                lane.name,
                type(exc).__name__,
            )
            await asyncio.sleep(min(self.fetch_timeout_seconds, 5.0))
            return

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
                running = self._records.get(command_id)
                if running is not None:
                    # Only a same-lane, same-envelope redelivery of a command that is
                    # still running is worth another try. A twin from the other
                    # lane (or with a different envelope) can never become valid, so
                    # terminate it now instead of letting it burn max_deliver and
                    # sit in the workqueue forever once the original finishes.
                    if (
                        running.invocation.lane != lane.name
                        or running.envelope_digest != digest
                    ):
                        raise CommandInvalid(
                            "command_id is already executing on another lane or "
                            "with a different command envelope"
                        )
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
                    # A drain that began after this command was fetched (or while
                    # it waited for a same-thread predecessor): publish nothing and
                    # keep the journal row dispatchable, so the next process runs it.
                    self._refuse_if_not_accepting()
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
                    # Last look before the hand-off (no await from here on): a
                    # drain that began while the started facts were published.
                    self._refuse_if_not_accepting()
                    record.event = event
                    # The wall clock starts at dispatch, not at claim: time spent
                    # waiting for a same-thread predecessor is not the turn's.
                    dispatched_at = time.monotonic()
                    record.dispatched_at = dispatched_at
                    await super().handle_message(event)
                    # Hermes records whether it admitted the event (a task was
                    # started or it was queued for one). Unadmitted, no turn and
                    # no processing-complete hook will ever come for it.
                    if getattr(event, "_gateway_accepted", True) is not True:
                        record.finalizing = True
                        raise TurnNotRun(
                            "not_accepted",
                            "Hermes did not admit the turn (dropped at adapter ingress)",
                        )
                    await self._await_turn(record, lane, dispatched_at)
            finally:
                remaining = self._conversation_lock_refs.get(lock_key, 1) - 1
                if remaining <= 0:
                    self._conversation_lock_refs.pop(lock_key, None)
                    self._conversation_locks.pop(lock_key, None)
                else:
                    self._conversation_lock_refs[lock_key] = remaining

            await message.ack()
        except TurnNotRun as exc:
            await self._not_run(
                message, lane, record, exc, replay_started=not started_publish_complete
            )
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

    @staticmethod
    def _last_delivery(message: Any, lane: LaneRuntime) -> bool:
        """True when JetStream will not deliver ``message`` again after a NAK."""

        try:
            delivered = message.metadata.num_delivered
        except Exception:
            return False
        return (
            isinstance(delivered, int)
            and not isinstance(delivered, bool)
            and delivered >= lane.max_deliver
        )

    async def _not_run(
        self,
        message: Any,
        lane: LaneRuntime,
        record: PendingInvocation | None,
        exc: TurnNotRun,
        *,
        replay_started: bool = True,
    ) -> None:
        """Hermes did not run the turn: NAK it for a later delivery, journal untouched.

        No completed fact is published and the journal row stays pending or
        started (both are dispatched again on redelivery). Only when this was the
        last delivery JetStream will make is the command closed as an explicit
        failure instead: a NAK there would drop it without any terminal fact."""

        command_id = record.invocation.invocation_id if record is not None else None
        profile = record.invocation.profile if record is not None else None
        if record is not None and self._last_delivery(message, lane):
            logger.warning(
                "Bloodbank command not run by Hermes lane=%s profile=%s command_id=%s "
                "reason=%s: %s; no deliveries left, closing it as failed",
                lane.name,
                profile,
                command_id,
                exc.reason,
                exc.detail,
            )
            try:
                await self._close_not_run(record, exc, replay_started=replay_started)
            except Exception as close_error:
                logger.error(
                    "Bloodbank not-run closure could not be completed error_type=%s",
                    type(close_error).__name__,
                )
                await message.nak(delay=lane.nak_delay)
                return
            await message.ack()
            return
        logger.warning(
            "Bloodbank command not run by Hermes lane=%s profile=%s command_id=%s "
            "reason=%s: %s; NAK, it runs on a later delivery",
            lane.name,
            profile,
            command_id,
            exc.reason,
            exc.detail,
        )
        await message.nak(delay=lane.nak_delay)

    async def _close_not_run(
        self, record: PendingInvocation, exc: TurnNotRun, *, replay_started: bool
    ) -> None:
        """Close a command Hermes never ran as failed, started facts first.

        The started facts are published when this attempt has not published
        them already (a pending row never did; an earlier attempt may have
        stopped between the two); each carries its deterministic Nats-Msg-Id."""

        invocation = record.invocation
        before = await asyncio.to_thread(
            self.execution_state.get, invocation.invocation_id
        )
        persisted = await asyncio.to_thread(
            self.execution_state.mark_started,
            command_id=invocation.invocation_id,
            digest=record.envelope_digest,
        )
        if replay_started or before is None or before.state == "pending":
            await self._publish_many(persisted.started_events)
        events = terminal_events(
            invocation,
            outcome="failure",
            failure_code=HERMES_REFUSED_CODE,
            failure_message=(
                f"Hermes did not run the turn ({exc.detail}) and the command has "
                "no deliveries left"
            ),
            failure_data={"refusal": exc.reason},
        )
        await asyncio.to_thread(
            self.execution_state.mark_completed,
            command_id=invocation.invocation_id,
            digest=record.envelope_digest,
            outcome="failure",
            terminal_events=events,
            failure_code=HERMES_REFUSED_CODE,
        )
        await self._publish_many(events)

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

    @staticmethod
    def _turn_deadline(
        lane: LaneRuntime, invocation: Invocation
    ) -> tuple[float | None, str]:
        """Effective wall-clock budget for one turn and where it came from.

        The lane's ``turn_timeout`` is the ceiling.  A command may only ask for
        less, through ``data.deadline_seconds``; a lane without a timeout has no
        ceiling to cap against, so it ignores the deadline."""

        if lane.turn_timeout is None:
            return None, "none"
        deadline = invocation.envelope["data"].get("deadline_seconds")
        if deadline is not None and deadline < lane.turn_timeout:
            return float(deadline), "envelope"
        return lane.turn_timeout, "lane"

    async def _await_turn(
        self, record: PendingInvocation, lane: LaneRuntime, dispatched_at: float
    ) -> None:
        """Wait for Hermes to finish the turn, or stop it when the budget runs out."""

        timeout, source = self._turn_deadline(lane, record.invocation)
        if timeout is None:
            await record.completion
            return
        try:
            # shield: expiry must not cancel the future the completion hook sets.
            await asyncio.wait_for(asyncio.shield(record.completion), timeout)
        except asyncio.TimeoutError:
            await self._expire_turn(record, lane, timeout, source, dispatched_at)

    async def _expire_turn(
        self,
        record: PendingInvocation,
        lane: LaneRuntime,
        timeout: float,
        source: str,
        dispatched_at: float,
    ) -> None:
        """The turn outlived its budget: stop it, close the journal, publish failed.

        Order matters.  The outcome is claimed synchronously first so a late
        completion (or the CANCELLED hook our own stop provokes) stands down.
        The turn is stopped before the journal is written so the failed fact can
        say truthfully whether Hermes confirmed the stop.  The journal row is
        written before publication so a publish failure replays the identical
        terminal events on redelivery instead of running the prompt again."""

        if record.finalizing or record.completion.done():
            # Lost the race to a legitimate completion: let it finish.
            await record.completion
            return
        record.finalizing = True
        invocation = record.invocation
        try:
            stopped = await self._stop_turn(record)
            elapsed = time.monotonic() - dispatched_at
            message = (
                f"agent turn timed out after {_seconds(elapsed)}s on {lane.name} "
                f"lane (limit {_seconds(timeout)}s)"
            )
            events = terminal_events(
                invocation,
                outcome="failure",
                failure_code=TURN_TIMEOUT_CODE,
                failure_message=message
                + ("" if stopped else "; Hermes did not confirm the turn stopped"),
                failure_data={
                    "elapsed_seconds": round(elapsed, 1),
                    "timeout_seconds": round(timeout, 1),
                    "timeout_source": source,
                    "turn_stopped": stopped,
                },
            )
            await asyncio.to_thread(
                self.execution_state.mark_completed,
                command_id=invocation.invocation_id,
                digest=record.envelope_digest,
                outcome="failure",
                terminal_events=events,
                failure_code=TURN_TIMEOUT_CODE,
            )
            logger.warning(
                "Bloodbank turn timed out lane=%s profile=%s command_id=%s "
                "elapsed=%.1fs limit=%.1fs source=%s stopped=%s",
                lane.name,
                invocation.profile,
                invocation.invocation_id,
                elapsed,
                timeout,
                source,
                stopped,
            )
            await self._publish_many(events)
        except Exception as exc:
            record.completion.set_exception(exc)
            # Retrieved here: the handler re-raises it into the NAK path.
            record.completion.exception()
            raise
        record.completion.set_result(None)

    def _session_key(self, event: Any) -> str | None:
        derive = getattr(self, "_event_session_key", None)
        if derive is None:
            return None
        try:
            return str(derive(event))
        except Exception:
            return None

    async def _stop_turn(self, record: PendingInvocation) -> bool:
        """Stop the Hermes turn behind ``record``; True once its session is idle.

        The stop goes through Hermes' own ``/stop`` command path: the gateway
        runner hard-interrupts the running agent, invalidates its run
        generation, kills the shell processes the turn spawned and cancels the
        session's processing task.  If the session guard is still held after
        that, fall back to the adapter-level session cancellation, which
        cancels the owning asyncio task directly.  A tool call blocked in a
        thread that never checks its interrupt flag can outlive both; the
        return value tells the caller (and the failed fact) when that happens."""

        event = record.event
        if event is None:
            return False
        key = self._session_key(event)
        stop = MessageEvent(
            text="/stop",
            message_type=MessageType.TEXT,
            source=event.source,
            message_id=f"{record.invocation.invocation_id}:turn-timeout-stop",
            internal=True,
        )
        try:
            await asyncio.wait_for(super().handle_message(stop), STOP_BUDGET_SECONDS)
        except Exception as exc:
            logger.error(
                "Bloodbank turn stop request failed error_type=%s", type(exc).__name__
            )
        active = getattr(self, "_active_sessions", None)
        if key is None or active is None:
            return False
        if key in active:
            try:
                await asyncio.wait_for(
                    self.cancel_session_processing(key), STOP_BUDGET_SECONDS
                )
            except Exception as exc:
                logger.error(
                    "Bloodbank session cancel failed error_type=%s", type(exc).__name__
                )
        return key not in active

    async def on_processing_complete(
        self, event: MessageEvent, outcome: ProcessingOutcome
    ) -> None:
        record = self._records.get(str(event.message_id or ""))
        if record is None or record.completion.done() or record.finalizing:
            return
        record.finalizing = True
        result = getattr(outcome, "value", str(outcome)).lower()
        terminal_outcome = (
            "success"
            if result == "success"
            else "cancelled"
            if result in {"cancelled", "canceled"}
            else "failure"
        )
        # Hermes reports a turn it refused (draining, busy, an error notice in
        # place of an answer) as SUCCESS: its notice is the "reply". Read it.
        notice = (
            self._notices.classify(record.final_reply)
            if terminal_outcome == "success"
            else None
        )
        if notice is not None and notice.disposition == NOT_RUN:
            record.completion.set_exception(
                TurnNotRun(
                    notice.key,
                    f"Hermes answered with its {notice.key} notice instead of running "
                    "the turn",
                )
            )
            # Retrieved here: the dispatching task re-raises it into the NAK path.
            record.completion.exception()
            return
        failure_code = failure_message = failure_data = None
        if notice is not None:
            terminal_outcome = "failure"
            failure_code, failure_message, failure_data = self._notice_failure(
                notice, record.final_reply
            )
            logger.warning(
                "Bloodbank turn closed as failed lane=%s profile=%s command_id=%s: "
                "Hermes replied with its %s notice",
                record.invocation.lane,
                record.invocation.profile,
                record.invocation.invocation_id,
                notice.key,
            )
        try:
            events = terminal_events(
                record.invocation,
                outcome=terminal_outcome,
                failure_code=failure_code,
                failure_message=failure_message,
                failure_data=failure_data,
            )
            await asyncio.to_thread(
                self.execution_state.mark_completed,
                command_id=record.invocation.invocation_id,
                digest=record.envelope_digest,
                outcome=terminal_outcome,
                terminal_events=events,
                failure_code=failure_code,
            )
            await self._publish_many(events)
        except Exception as exc:
            record.completion.set_exception(exc)
            raise
        else:
            record.completion.set_result(None)

    @staticmethod
    def _notice_failure(
        notice: HermesNotice, reply: str | None
    ) -> tuple[str, str, dict[str, Any]]:
        """failure_code, failure_message and failure_data for a Hermes error notice."""

        text = " ".join(str(reply or "").split())
        if len(text) > 300:
            text = text[:299] + "…"
        if notice.key.startswith("gateway.errors."):
            code = HERMES_TURN_ERROR_CODE
            what = "Hermes ended the turn with an error notice instead of an answer"
        else:
            code = HERMES_REFUSED_CODE
            what = "Hermes refused the turn"
        return code, f"{what} ({notice.key}): {text}", {"refusal": notice.key}

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Deliver nothing: a command turn's consumer reads the reply from the
        Hermes session and owns its audio.

        Two kinds of send are observed on the way through: an interim assistant
        message of a command turn this adapter is running is published as
        ``bloodbank.agent.invocation.progressed`` (what the agent says it is
        doing while a human waits), and the first chunk of a turn's final reply
        is kept so a Hermes refusal notice is not mistaken for an answer.
        Observability never fails the turn: any error is logged and the send
        still succeeds."""

        try:
            self._note_final_reply(chat_id, content, reply_to, metadata)
        except Exception as exc:
            logger.warning(
                "Bloodbank final reply bookkeeping failed error_type=%s",
                type(exc).__name__,
            )
        try:
            await self._publish_progress(chat_id, content, metadata)
        except Exception as exc:
            logger.warning(
                "Bloodbank progress publication failed error_type=%s",
                type(exc).__name__,
            )
        return SendResult(success=True, message_id=str(uuid.uuid4()))

    def _note_final_reply(
        self,
        chat_id: str,
        content: Any,
        reply_to: str | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        """Keep the first non-interim send anchored to a running command.

        Hermes delivers a turn's final text with ``reply_to`` = the triggering
        MessageEvent id (our command id); status notices and shutdown warnings
        carry no anchor and an interim message carries ``_interim_send``."""

        if isinstance(metadata, dict) and metadata.get("_interim_send") is True:
            return
        record = self._records.get(str(reply_to)) if reply_to else None
        if (
            record is None
            or record.invocation.thread_id != str(chat_id)
            or record.event is None
            or record.completion.done()
            or record.final_reply is not None
            or not isinstance(content, str)
        ):
            return
        record.final_reply = content

    def _progress_record(
        self, chat_id: str, metadata: dict[str, Any] | None
    ) -> PendingInvocation | None:
        """The running command whose interim assistant message this send is, or None.

        Hermes marks every mid-turn send with ``_interim_send``; its stream
        consumer additionally anchors assistant commentary to the triggering
        message (``reply_to_message_id`` = the MessageEvent id = our command
        id). Status notices (the "still working" heartbeat, background-review
        notes, warnings) carry no anchor, a final reply carries no interim
        marker, and a chat or anchor this adapter is not running is somebody
        else's: all of them are ignored."""

        if not isinstance(metadata, dict) or metadata.get("_interim_send") is not True:
            return None
        anchor = metadata.get("reply_to_message_id")
        record = self._records.get(str(anchor)) if anchor else None
        if record is None:
            if any(r.invocation.thread_id == str(chat_id) for r in self._records.values()):
                # A live command turn's chat, but no anchor: a status notice, or a
                # Hermes change that stopped anchoring commentary (then no progress
                # facts appear at all, and this line says why).
                logger.info(
                    "Bloodbank interim send without a command anchor ignored chat_id=%s",
                    chat_id,
                )
            return None
        if (
            record.invocation.thread_id != str(chat_id)
            or record.event is None
            or record.finalizing
            or record.completion.done()
        ):
            return None
        return record

    async def _publish_progress(
        self, chat_id: str, content: Any, metadata: dict[str, Any] | None
    ) -> None:
        record = self._progress_record(chat_id, metadata)
        if record is None:
            return
        text, truncated = progress_text(content)
        if text is None or self._js is None:
            return
        record.progress_seq += 1
        elapsed = (
            time.monotonic() - record.dispatched_at
            if record.dispatched_at is not None
            else None
        )
        invocation = record.invocation
        event = progress_event(
            invocation,
            seq=record.progress_seq,
            text=text,
            truncated=truncated,
            elapsed_seconds=elapsed,
        )
        await self._publish_many((event,))
        logger.info(
            "Bloodbank progress published lane=%s profile=%s command_id=%s seq=%d "
            "chars=%d elapsed=%.1fs",
            invocation.lane,
            invocation.profile,
            invocation.invocation_id,
            record.progress_seq,
            len(text),
            elapsed or 0.0,
        )

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": str(chat_id), "type": "dm"}

    def _should_auto_tts_for_chat(self, chat_id: str) -> bool:
        """Never auto-voice a Bloodbank turn.

        A Bloodbank command turn is not a conversation with a listener: this adapter has no send_voice, so the
        gateway's global voice.auto_tts synthesized a reply, failed to deliver it ("send_voice fallback") and threw
        it away, on every command turn (~557 discarded syntheses; +3.9 s before invocation.completed on every
        voice memo, whose audio the Holocene inbox-watcher then synthesized again). Whoever consumes the turn owns
        its audio. Same class as the a2a skip in hermes-agent run_voice.py (#90103).
        """
        del chat_id
        return False


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
