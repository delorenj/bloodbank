"""Hermes platform adapter backed by the Bloodbank JetStream command lane."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
import weakref
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
    DETACHED_CEILING_SOURCE,
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
    detached_event,
    progress_event,
    progress_text,
    started_events,
    terminal_events,
)
from .execution_state import ExecutionRecord, ExecutionStateStore, envelope_digest
from .refusals import NOT_RUN, HermesNotice, NoticeClassifier, is_interrupt_text

logger = logging.getLogger(__name__)
ROUTE_REJECTION_REASON = "route_policy_invalid_before_dispatch"
ROUTE_REJECTION_MESSAGE = "Hermes route became ineligible before dispatch"
# Failed-fact reasons when Hermes did not run a command as asked (see refusals.py):
# refused = no turn ran (closed only once no delivery is left, or when a retry
# cannot help); turn_error = a turn ran and Hermes ended it with an error notice.
HERMES_REFUSED_CODE = "hermes_refused"
HERMES_TURN_ERROR_CODE = "hermes_turn_error"
# A turn still running when Hermes' stop() drain ran out (restart_drain_timeout,
# or restart_after_turn_timeout on a reload) was hard-interrupted (or cancelled
# at adapter teardown) and reported as an ordinary reply: it did not finish.
INTERRUPTED_BY_SHUTDOWN_CODE = "interrupted_by_shutdown"
# What a lane does with such a turn: fail = close it as interrupted_by_shutdown
# (partial work may have run; nothing runs twice); retry = NAK it so the next
# process re-dispatches the same thread (its Hermes session keeps the partial
# transcript, but tool side effects of the cut-off attempt can repeat).
SHUTDOWN_INTERRUPT_MODES = ("fail", "retry")
# Refusal key for the agent loop's "Operation interrupted ..." text in place of an answer.
OPERATION_INTERRUPTED_REFUSAL = "agent.operation_interrupted"
# How often a lane paused by a Hermes drain re-reads the runner's state, and how
# often a pause that is still on is logged again (a leaked drain marker shows up).
DRAIN_POLL_SECONDS = 1.0
PAUSE_REMIND_SECONDS = 600.0
# The runner attribute holding the adapters told about its shutdown interrupts.
_RUNNER_WATCHERS = "_bloodbank_gateway_interrupt_watchers"
_NOT_ACCEPTING_TEXT = {
    "restarting": "restarting (draining in-flight turns)",
    "shutting_down": "shutting down (draining in-flight turns)",
    "external_drain": "draining for a maintenance action",
}


class TurnNotRun(Exception):
    """Hermes did not run the command's turn as asked: never close it as done.

    ``retryable`` (the default) NAKs it for a later delivery; otherwise, or on
    the last delivery, it is closed as an explicit failure with ``code``."""

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        code: str = HERMES_REFUSED_CODE,
        retryable: bool = True,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.code = code
        self.retryable = retryable
        # failed-fact fields when it is closed; default {"refusal": reason}.
        self.data = data


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
# Detach at the wall clock (extra.interactive.detach_on_timeout): how many
# turns may run on past it at once, and when one is stopped after all (seconds
# after dispatch). 45 min: past Hermes' own 30 min inactivity stop
# (agent.gateway_timeout) for a turn that stalls, short of the watcher's 1 h
# queue horizon (MEMO_QUEUE_MAX_WAIT_S) for one that keeps working.
DEFAULT_MAX_DETACHED = 2
DEFAULT_DETACHED_CEILING_SECONDS = 2_700.0
# How long the gateway waits for Hermes to unwind a stopped turn before it
# reports the turn as not confirmed stopped.
STOP_BUDGET_SECONDS = 10.0
# How long the gateway waits for Hermes to finish an injected `/model --once`
# (the switch resolves the route's provider runtime) before it dispatches the
# turn anyway, on whatever model the session then has.
MODEL_SWITCH_BUDGET_SECONDS = 10.0


def _turn_timeout(
    extra: dict[str, Any],
    default: float | None,
    *,
    prefix: str = "bloodbank.extra",
    key: str = "turn_timeout_seconds",
) -> float | None:
    """Per-turn wall-clock seconds: absent = ``default``, otherwise a strict
    positive finite number (not a bool, not a numeric string, not null)."""

    if key not in extra:
        return default
    raw = extra[key]
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or raw <= 0
        or raw > MAX_TURN_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"{prefix}.{key} must be a positive number "
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


def _choice(
    extra: dict[str, Any],
    key: str,
    default: str,
    choices: tuple[str, ...],
    *,
    prefix: str = "bloodbank.extra",
) -> str:
    value = extra.get(key, default)
    if value not in choices:
        raise ValueError(f"{prefix}.{key} must be one of {', '.join(choices)}")
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
    # fail | retry: a turn Hermes' shutdown interrupted (SHUTDOWN_INTERRUPT_MODES).
    shutdown_interrupt: str = "fail"
    # Detach at the wall clock: a turn that outlives the lane's turn_timeout
    # gives its slot back instead of being stopped, and keeps running (its
    # message un-acked) until it finishes or reaches detached_ceiling seconds
    # after dispatch, where it is stopped as before. At most max_detached at once.
    detach_on_timeout: bool = False
    max_detached: int = 0
    detached_ceiling: float | None = None
    # fail | retry for a turn that was DETACHED when Hermes' shutdown cut it off.
    # fail by default: it already ran past the wall clock (up to the ceiling), the
    # caller was told it runs on, and a NAK re-run would repeat all of that work
    # and every side-effecting tool call from the top.
    detached_shutdown_interrupt: str = "fail"
    subscription: Any = None
    task: asyncio.Task[None] | None = None
    inflight: set[asyncio.Task[None]] = field(default_factory=set)
    # Handler tasks of detached turns: they hold no slot, but their messages
    # are still un-acked (the consumer's max_ack_pending counts both sets).
    detached: set[asyncio.Task[None]] = field(default_factory=set)
    # Set when a turn detaches, so a fetch loop waiting for a slot wakes up.
    slot_freed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def max_ack_pending(self) -> int:
        """Messages the durable may have outstanding: the slots plus detached turns."""

        return self.max_inflight + (self.max_detached if self.detach_on_timeout else 0)


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
    # "restart" / "shutdown" once Hermes' stop() hard-interrupted this turn
    # because its drain ran out: whatever the turn reports next is not an answer.
    shutdown_interrupt: str | None = None
    # Monotonic time the turn detached at its lane's wall clock (its slot was
    # given back while it kept running); None for a turn that never detached.
    detached_at: float | None = None
    # data.model outcome: {"requested": route, "applied": bool, ...}; None when
    # the command asked for no model of its own.
    model_override: dict[str, Any] | None = None


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
            shutdown_interrupt=_choice(
                extra, "shutdown_interrupt", "fail", SHUTDOWN_INTERRUPT_MODES
            ),
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
        # data.model on a command: run that turn on the named route through
        # Hermes' own `/model <route> --once` (see _apply_turn_model).
        self.allow_model_override = _strict_bool(extra, "allow_model_override", True)
        self.model_switch_budget_seconds = MODEL_SWITCH_BUDGET_SECONDS
        # message id of an injected `/model` event -> the reply Hermes sent for it.
        self._model_switch_replies: dict[str, str | None] = {}

        self._nc: Any = None
        self._js: Any = None
        self._records: dict[str, PendingInvocation] = {}
        self._execution_claim_lock = asyncio.Lock()
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._conversation_lock_refs: dict[str, int] = {}
        self.drain_poll_seconds = DRAIN_POLL_SECONDS
        self.pause_remind_seconds = PAUSE_REMIND_SECONDS
        self._notices = NoticeClassifier()
        # lane name -> (why it stopped fetching, monotonic pause start, last logged).
        self._paused_lanes: dict[str, tuple[str, float, float]] = {}

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
        turn_timeout = _turn_timeout(
            raw, DEFAULT_INTERACTIVE_TURN_TIMEOUT_SECONDS, prefix=prefix
        )
        detach = _strict_bool(raw, "detach_on_timeout", False, prefix=prefix)
        max_detached = _bounded_int(
            raw, "max_detached", DEFAULT_MAX_DETACHED, 1, 64, prefix=prefix
        )
        ceiling = _turn_timeout(
            raw,
            DEFAULT_DETACHED_CEILING_SECONDS,
            prefix=prefix,
            key="detached_ceiling_seconds",
        )
        if detach and (turn_timeout is None or ceiling <= turn_timeout):
            raise ValueError(
                f"{prefix}.detached_ceiling_seconds must be greater than "
                "turn_timeout_seconds when detach_on_timeout is true"
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
            turn_timeout=turn_timeout,
            shutdown_interrupt=_choice(
                raw, "shutdown_interrupt", "fail", SHUTDOWN_INTERRUPT_MODES, prefix=prefix
            ),
            detach_on_timeout=detach,
            max_detached=max_detached if detach else 0,
            detached_ceiling=ceiling if detach else None,
            detached_shutdown_interrupt=_choice(
                raw,
                "detached_shutdown_interrupt",
                "fail",
                SHUTDOWN_INTERRUPT_MODES,
                prefix=prefix,
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
                    max_ack_pending=lane.max_ack_pending,
                )
                lane.subscription = await self._js.pull_subscribe(
                    lane.spec.subject,
                    durable=lane.durable,
                    stream=COMMAND_STREAM,
                    config=consumer_config,
                    pending_msgs_limit=lane.max_ack_pending,
                    pending_bytes_limit=lane.max_ack_pending * self.max_command_bytes,
                )
                info = await self._js.consumer_info(
                    COMMAND_STREAM,
                    lane.durable,
                    timeout=self.publish_timeout_seconds,
                )
                if self._needs_ack_pending_update(info.config, lane):
                    # pull_subscribe never edits an existing durable. Turning
                    # detach on (or off) changes only max_ack_pending, which
                    # JetStream lets a consumer update in place: do that, then
                    # hold the result to the full contract like any other bind.
                    logger.warning(
                        "Bloodbank durable %s max_ack_pending %s -> %d (lane=%s "
                        "max_inflight=%d detached=%d)",
                        lane.durable,
                        getattr(info.config, "max_ack_pending", None),
                        lane.max_ack_pending,
                        lane.name,
                        lane.max_inflight,
                        lane.max_detached,
                    )
                    await self._js.add_consumer(
                        COMMAND_STREAM,
                        config=consumer_config,
                        timeout=self.publish_timeout_seconds,
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
                "Bloodbank gateway connected lane=%s durable=%s max_inflight=%d "
                "max_detached=%d",
                lane.name,
                lane.durable,
                lane.max_inflight,
                lane.max_detached,
            )
        return True

    @staticmethod
    def _consumer_observed(actual: Any) -> dict[str, Any]:
        ack_policy = getattr(actual, "ack_policy", None)
        return {
            "filter_subject": getattr(actual, "filter_subject", None),
            "ack_policy": getattr(ack_policy, "value", ack_policy),
            "max_ack_pending": getattr(actual, "max_ack_pending", None),
            "max_deliver": getattr(actual, "max_deliver", None),
        }

    @staticmethod
    def _consumer_expected(lane: LaneRuntime) -> dict[str, Any]:
        return {
            "filter_subject": lane.spec.subject,
            "ack_policy": "explicit",
            "max_ack_pending": lane.max_ack_pending,
            "max_deliver": lane.max_deliver,
        }

    def _needs_ack_pending_update(self, actual: Any, lane: LaneRuntime) -> bool:
        """True when an existing durable differs from the lane only in max_ack_pending."""

        observed = self._consumer_observed(actual)
        expected = self._consumer_expected(lane)
        if observed["max_ack_pending"] == expected["max_ack_pending"]:
            return False
        return all(
            observed[key] == expected[key] for key in expected if key != "max_ack_pending"
        )

    def _assert_consumer_contract(
        self, actual: Any, lane: LaneRuntime | None = None
    ) -> None:
        """Refuse to bind a stale durable with unsafe delivery semantics."""

        lane = self._lane(lane)
        if self._consumer_observed(actual) != self._consumer_expected(lane):
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
        # Detached turns are command tasks too: their messages are un-acked and
        # each settles (ack, NAK for a retry) the same way an in-flight one does.
        # Snapshot them first: a detached task leaves lane.detached as soon as
        # its turn closes, before it has acked.
        tasks = [
            task
            for lane in self._lanes
            for task in (*lane.inflight, *lane.detached)
        ]
        await self.cancel_background_tasks()
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
            lane.detached.clear()
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
        now = time.monotonic()
        paused = self._paused_lanes.get(lane.name)
        if paused is not None and paused[0] == reason:
            if now - paused[2] < self.pause_remind_seconds:
                return
            # Still paused: say so again, so a drain that never ends (a leaked
            # .drain_request.json, a wedged restart wait) is visible in the log.
            self._paused_lanes[lane.name] = (reason, paused[1], now)
            logger.warning(
                "Bloodbank lane=%s still not fetching after %.0fs: Hermes gateway is "
                "%s; commands stay queued on durable %s",
                lane.name,
                now - paused[1],
                _NOT_ACCEPTING_TEXT[reason],
                lane.durable,
            )
            return
        self._paused_lanes[lane.name] = (reason, now, now)
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

    def _runner_stopping(self) -> bool:
        """Hermes' stop() has begun (it clears ``_running`` before its drain)."""

        runner = getattr(self, "gateway_runner", None)
        return runner is not None and getattr(runner, "_running", True) is False

    def _shutdown_kind(self) -> str:
        runner = getattr(self, "gateway_runner", None)
        restart = runner is not None and getattr(runner, "_restart_requested", False) is True
        return "restart" if restart else "shutdown"

    def _watch_runner_interrupts(self) -> None:
        """Learn which command turns Hermes' stop() hard-interrupts.

        When the stop drain (restart_drain_timeout, after a reload's
        restart_after_turn_timeout wait) runs out, Hermes calls the runner's
        ``_interrupt_running_agents`` once (and again at the end of its settle
        window). An interrupted turn then returns its partial text, or
        "Operation interrupted ...", as a normal reply that the base adapter
        reports as SUCCESS. The runner offers no hook for this, so the plugin
        wraps that one method (once per runner, every adapter on it notified).
        Missing method = no wrap; the reply/cancel heuristics still apply."""

        runner = getattr(self, "gateway_runner", None)
        if runner is None:
            return
        watchers = getattr(runner, _RUNNER_WATCHERS, None)
        if watchers is None:
            original = getattr(runner, "_interrupt_running_agents", None)
            if not callable(original):
                return
            watchers = weakref.WeakSet()

            def interrupting(reason: Any = None, *args: Any, **kwargs: Any) -> Any:
                for adapter in list(watchers):
                    try:
                        adapter._note_shutdown_interrupt(runner)
                    except Exception as exc:
                        logger.error(
                            "Bloodbank shutdown-interrupt bookkeeping failed error_type=%s",
                            type(exc).__name__,
                        )
                return original(reason, *args, **kwargs)

            try:
                runner._interrupt_running_agents = interrupting
                setattr(runner, _RUNNER_WATCHERS, watchers)
            except Exception:
                return
        watchers.add(self)

    def _note_shutdown_interrupt(self, runner: Any) -> None:
        """Mark every dispatched command whose Hermes session is being interrupted."""

        running = getattr(runner, "_running_agents", None)
        sessions = set(running) if isinstance(running, dict) else None
        kind = self._shutdown_kind()
        for record in list(self._records.values()):
            if record.event is None or record.completion.done():
                continue
            if record.shutdown_interrupt is not None:
                continue
            key = self._session_key(record.event)
            if sessions is not None and key is not None and key not in sessions:
                continue  # finished in Hermes already; its completion is on its way
            record.shutdown_interrupt = kind
            logger.warning(
                "Bloodbank turn interrupted by Hermes %s lane=%s profile=%s "
                "command_id=%s after %.1fs: the stop drain ran out before it finished",
                kind,
                record.invocation.lane,
                record.invocation.profile,
                record.invocation.invocation_id,
                time.monotonic() - (record.dispatched_at or time.monotonic()),
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
            # A slot comes back when a command task ends or when a turn
            # detaches (its task leaves lane.inflight but keeps running).
            lane.slot_freed.clear()
            freed = asyncio.ensure_future(lane.slot_freed.wait())
            try:
                await asyncio.wait(
                    {*lane.inflight, freed}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                freed.cancel()
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
                    if persisted.state in {"pending", "started"}:
                        # A 'started' row here is a new attempt (a redelivery after
                        # a NAK or an unclean exit): mark_started clears the last
                        # attempt's detached_at, so the journal never reports a
                        # re-run as detached from its first second.
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
                    # data.model: switch this session's model for the next
                    # turn only, before the prompt is handed over.
                    record.model_override = await self._apply_turn_model(
                        invocation, source
                    )
                    try:
                        # Re-read the registry after all pre-dispatch awaits. A
                        # claimed or restarted pending command must not execute on
                        # a route disabled while lifecycle publication was in flight.
                        await self._assert_dispatch_route(invocation)
                        # Last look before the hand-off (no await from here on): a
                        # drain that began while the started facts were published.
                        self._refuse_if_not_accepting()
                        self._watch_runner_interrupts()
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
                            if invocation.prompt.lstrip().startswith("/"):
                                # A bypass slash command (/stop, /new, /approve, ...)
                                # on a busy session runs inline without admission: it
                                # may have acted, so a redelivery must not repeat it.
                                raise TurnNotRun(
                                    "not_accepted_slash_command",
                                    "Hermes handled the prompt as a slash command inline "
                                    "instead of starting an agent turn",
                                    retryable=False,
                                )
                            raise TurnNotRun(
                                "not_accepted",
                                "Hermes did not admit the turn (dropped at adapter ingress)",
                            )
                    except BaseException:
                        # No turn will run (route gone, a drain, not admitted): the
                        # --once switch must not stay armed for the thread's next turn.
                        self._disarm_turn_model(record, source)
                        raise
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

    async def _apply_turn_model(
        self, invocation: Invocation, source: Any
    ) -> dict[str, Any] | None:
        """Run this command's turn on ``data.model``: Hermes' ``/model <route> --once``.

        The switch goes through the supported slash-command path, the same one a
        user typing it in a chat gets: Hermes resolves the route's provider
        runtime, keeps it for the session's NEXT turn only (never written to
        config.yaml or the session store) and restores the session's own model
        when that turn ends, however it ends (/stop and eviction included). Every
        command thread is its own Hermes session, so nothing else is affected.

        Best effort, never the turn's fate: an unknown route, a busy session or a
        slow switch leaves the turn on the profile's model, and the outcome
        (``requested``, ``applied``, ``reason``) rides on the terminal fact."""

        route = invocation.model
        if not route:
            return None
        outcome: dict[str, Any] = {"requested": route, "applied": False}
        if not self.allow_model_override:
            outcome["reason"] = "disabled"
            return outcome
        probe = MessageEvent(
            text=f"/model {route} --once",
            message_type=MessageType.TEXT,
            source=source,
            message_id=f"{invocation.invocation_id}:turn-model",
            internal=True,
        )
        key = self._session_key(probe)
        active = getattr(self, "_active_sessions", None)
        if key is not None and active is not None and key in active:
            # A busy session would queue the command as text, not run it.
            outcome["reason"] = "session_busy"
            logger.warning(
                "Bloodbank model override skipped lane=%s profile=%s command_id=%s "
                "model=%s: the session is busy",
                invocation.lane,
                invocation.profile,
                invocation.invocation_id,
                route,
            )
            return outcome
        self._model_switch_replies[probe.message_id] = None
        try:
            await asyncio.wait_for(
                super().handle_message(probe), self.model_switch_budget_seconds
            )
            # handle_message only starts the command's processing task: the
            # switch is done when the session is idle again.
            deadline = time.monotonic() + self.model_switch_budget_seconds
            while (
                key is not None
                and active is not None
                and key in active
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.05)
            if key is not None and active is not None and key in active:
                outcome["reason"] = "switch_timeout"
        except Exception as exc:
            outcome["reason"] = f"switch_failed:{type(exc).__name__}"
        finally:
            reply = self._model_switch_replies.pop(probe.message_id, None)
        applied = self._session_model_applied(source, key, route)
        if applied is None:
            # No runner view of the session (tests, an older Hermes): read the reply.
            applied = bool(reply) and not str(reply).lstrip().startswith("❌")
        outcome["applied"] = applied
        if not applied:
            outcome.setdefault("reason", "rejected")
            if reply:
                outcome["detail"] = " ".join(str(reply).split())[:200]
        logger.log(
            logging.INFO if applied else logging.WARNING,
            "Bloodbank model override %s lane=%s profile=%s command_id=%s model=%s%s",
            "applied" if applied else "not applied",
            invocation.lane,
            invocation.profile,
            invocation.invocation_id,
            route,
            "" if applied else f" reason={outcome.get('reason')} reply={outcome.get('detail', '')!r}",
        )
        return outcome

    def _session_model_applied(
        self, source: Any, key: str | None, route: str
    ) -> bool | None:
        """Whether Hermes now holds ``route`` as the session's model override,
        or None when the runner offers no view of it."""

        runner = getattr(self, "gateway_runner", None)
        lookup = getattr(runner, "_session_model_override", None)
        if not callable(lookup):
            return None
        try:
            to_key = getattr(runner, "_session_key_for_source", None)
            session_key = to_key(source) if callable(to_key) else key
            override = lookup(session_key) if session_key else None
        except Exception:
            return None
        if not isinstance(override, dict):
            return False
        model = str(override.get("model") or "")
        # Hermes may store the route as given or without its provider prefix.
        return bool(model) and (route == model or route.endswith("/" + model) or model.endswith(route))

    def _disarm_turn_model(self, record: PendingInvocation, source: Any) -> None:
        """Undo this command's ``/model <route> --once`` when its turn will not run.

        Hermes restores a --once switch only in an agent turn's finally. A command
        refused after the switch (its route disabled, a drain, not admitted) runs
        no turn, so without this the switch stays armed and the thread's NEXT turn
        runs on the route, asked for or not. The runner's own settlement restore
        is used (a no-op when nothing is armed); best effort."""

        override = record.model_override
        if not override or override.get("reason") in {"disabled", "session_busy"}:
            return  # nothing was sent to Hermes
        runner = getattr(self, "gateway_runner", None)
        restore = getattr(runner, "_restore_pending_one_turn_model_override", None)
        to_key = getattr(runner, "_session_key_for_source", None)
        if not callable(restore) or not callable(to_key):
            return
        try:
            restore(to_key(source))
        except Exception as exc:
            logger.warning(
                "Bloodbank model override not disarmed command_id=%s error_type=%s",
                record.invocation.invocation_id,
                type(exc).__name__,
            )
            return
        override["disarmed"] = True
        logger.info(
            "Bloodbank model override disarmed command_id=%s model=%s: the turn did not run",
            record.invocation.invocation_id,
            override.get("requested"),
        )

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
        last = self._last_delivery(message, lane)
        if record is not None and (last or not exc.retryable):
            logger.warning(
                "Bloodbank command not run by Hermes lane=%s profile=%s command_id=%s "
                "reason=%s: %s; %s, closing it as failed",
                lane.name,
                profile,
                command_id,
                exc.reason,
                exc.detail,
                "a retry cannot help" if not exc.retryable else "no deliveries left",
            )
            try:
                await self._close_not_run(
                    record, exc, replay_started=replay_started, last=last and exc.retryable
                )
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
        self,
        record: PendingInvocation,
        exc: TurnNotRun,
        *,
        replay_started: bool,
        last: bool = True,
    ) -> None:
        """Close a command Hermes did not run as asked as failed, started facts first.

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
            failure_code=exc.code,
            failure_message=(
                (
                    exc.detail
                    if exc.code == INTERRUPTED_BY_SHUTDOWN_CODE
                    else f"Hermes did not run the turn ({exc.detail})"
                )
                + ("; the command has no deliveries left" if last else "")
            ),
            failure_data=exc.data or {"refusal": exc.reason},
            extra_data=self._turn_annotations(record),
        )
        await asyncio.to_thread(
            self.execution_state.mark_completed,
            command_id=invocation.invocation_id,
            digest=record.envelope_digest,
            outcome="failure",
            terminal_events=events,
            failure_code=exc.code,
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
        """Wait for Hermes to finish the turn, or stop it when the budget runs out.

        On a lane with ``detach_on_timeout`` a turn that outlives the lane limit
        detaches instead (``_detach_turn``): its slot is given back, the wait
        goes on to the lane's ``detached_ceiling`` and only then is the turn
        stopped. Everything after the wait (journal, terminal facts, ack or NAK,
        conversation lock) is the same code path either way."""

        timeout, source = self._turn_deadline(lane, record.invocation)
        if timeout is None:
            await record.completion
            return
        try:
            # shield: expiry must not cancel the future the completion hook sets.
            await asyncio.wait_for(asyncio.shield(record.completion), timeout)
            return
        except asyncio.TimeoutError:
            pass
        task = self._detach_turn(record, lane, timeout, source)
        if task is None:
            await self._expire_turn(record, lane, timeout, source, dispatched_at)
            return
        try:
            await self._announce_detached(record, lane, timeout, source)
            ceiling = float(lane.detached_ceiling or timeout)
            remaining = ceiling - (time.monotonic() - dispatched_at)
            try:
                await asyncio.wait_for(
                    asyncio.shield(record.completion), max(remaining, 0.0)
                )
            except asyncio.TimeoutError:
                await self._expire_turn(
                    record, lane, ceiling, DETACHED_CEILING_SOURCE, dispatched_at
                )
        finally:
            lane.detached.discard(task)

    def _detach_turn(
        self,
        record: PendingInvocation,
        lane: LaneRuntime,
        timeout: float,
        source: str,
    ) -> asyncio.Task[Any] | None:
        """Give the turn's slot back and let it run on; the detached task, or None.

        Only the lane's own limit detaches: a ``data.deadline_seconds`` is the
        caller asking for the turn to be stopped, and is honoured as before. A
        turn whose outcome is already claimed, a lane already holding
        ``max_detached`` detached turns, or one with no ceiling past the limit
        falls back to the stop. The task leaves ``lane.inflight`` (the fetch
        loop may pull another command at once) but keeps its message un-acked,
        its progress acks, its journal row and the conversation lock."""

        if (
            not lane.detach_on_timeout
            or source != "lane"
            or record.finalizing
            or record.completion.done()
            or lane.detached_ceiling is None
            or lane.detached_ceiling <= timeout
        ):
            return None
        invocation = record.invocation
        if len(lane.detached) >= lane.max_detached:
            logger.warning(
                "Bloodbank turn not detached lane=%s profile=%s command_id=%s: "
                "%d detached turns already run on (max_detached=%d); stopping it",
                lane.name,
                invocation.profile,
                invocation.invocation_id,
                len(lane.detached),
                lane.max_detached,
            )
            return None
        task = asyncio.current_task()
        if task is None:
            return None
        record.detached_at = time.monotonic()
        lane.inflight.discard(task)
        lane.detached.add(task)
        task.add_done_callback(lane.detached.discard)
        lane.slot_freed.set()
        logger.warning(
            "Bloodbank turn detached lane=%s profile=%s command_id=%s after %.1fs "
            "(limit %.1fs): slot released, the turn runs on until %.0fs",
            lane.name,
            invocation.profile,
            invocation.invocation_id,
            record.detached_at - (record.dispatched_at or record.detached_at),
            timeout,
            lane.detached_ceiling,
        )
        return task

    async def _announce_detached(
        self,
        record: PendingInvocation,
        lane: LaneRuntime,
        timeout: float,
        source: str,
    ) -> None:
        """Journal ``detached_at`` and publish agent.invocation.detached.

        Observability, never the turn's fate: a journal or broker error is logged
        and the turn still runs on to its normal close."""

        invocation = record.invocation
        elapsed = self._detached_after(record) or timeout
        try:
            await asyncio.to_thread(
                self.execution_state.mark_detached,
                command_id=invocation.invocation_id,
                digest=record.envelope_digest,
            )
        except Exception as exc:
            if self._closing(record):
                # The turn finished during the journal hop: its row is final, so
                # there is nothing to stamp (the terminal fact says detached).
                logger.info(
                    "Bloodbank detached turn closed before its detach was journaled "
                    "command_id=%s",
                    invocation.invocation_id,
                )
            else:
                logger.error(
                    "Bloodbank detached journal write failed command_id=%s error_type=%s",
                    invocation.invocation_id,
                    type(exc).__name__,
                )
        if self._closing(record):
            # Never publish the non-terminal detached fact after (or alongside) the
            # terminal one: a reader of the bus would see "runs on" after "done".
            return
        try:
            await self._publish_many(
                (
                    detached_event(
                        invocation,
                        elapsed_seconds=elapsed,
                        timeout_seconds=timeout,
                        timeout_source=source,
                        ceiling_seconds=float(lane.detached_ceiling or timeout),
                        detached_count=len(lane.detached),
                        max_detached=lane.max_detached,
                    ),
                )
            )
        except Exception as exc:
            logger.error(
                "Bloodbank detached fact publication failed command_id=%s error_type=%s",
                invocation.invocation_id,
                type(exc).__name__,
            )

    @staticmethod
    def _closing(record: PendingInvocation) -> bool:
        """The turn's outcome is claimed (being closed, or closed)."""

        return record.finalizing or record.completion.done()

    @staticmethod
    def _detached_after(record: PendingInvocation) -> float | None:
        if record.detached_at is None or record.dispatched_at is None:
            return None
        return max(0.0, record.detached_at - record.dispatched_at)

    def _turn_annotations(self, record: PendingInvocation) -> dict[str, Any] | None:
        """Terminal-fact fields the turn earned on the way: how long it ran in
        all when it detached, and what became of its ``data.model``."""

        data: dict[str, Any] = {}
        if record.model_override is not None:
            data["model_override"] = dict(record.model_override)
        after = self._detached_after(record)
        if after is None:
            return data or None
        elapsed = time.monotonic() - (record.dispatched_at or record.detached_at or 0.0)
        data.update(
            detached=True,
            detached_after_seconds=round(after, 1),
            elapsed_seconds=round(max(0.0, elapsed), 1),
        )
        return data

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
            annotations = self._turn_annotations(record) or {}
            if annotations.get("detached"):
                message = (
                    f"agent turn timed out after {_seconds(elapsed)}s on {lane.name} "
                    f"lane (detached at {_seconds(annotations['detached_after_seconds'])}s, "
                    f"ceiling {_seconds(timeout)}s)"
                )
            events = terminal_events(
                invocation,
                outcome="failure",
                failure_code=TURN_TIMEOUT_CODE,
                failure_message=message
                + ("" if stopped else "; Hermes did not confirm the turn stopped"),
                failure_data={
                    **annotations,
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
        # A turn Hermes' stop() cut off reports its partial text (or nothing, or
        # a cancellation at adapter teardown) like a finished one: not an answer.
        interrupted = self._shutdown_interruption(record, terminal_outcome)
        if interrupted is not None:
            not_run = self._interrupted_not_run(record, interrupted, terminal_outcome)
            lane = self._lane_named(record.invocation.lane)
            # A detached turn has its own setting: it ran past the wall clock (up
            # to the ceiling) and its caller was told it runs on, so a NAK re-run
            # would redo all of it from the top (detached_shutdown_interrupt).
            mode = (
                lane.detached_shutdown_interrupt
                if record.detached_at is not None
                else lane.shutdown_interrupt
            )
            if mode == "retry":
                logger.warning(
                    "Bloodbank turn cut off by Hermes %s lane=%s profile=%s "
                    "command_id=%s: NAK, the next process re-dispatches the same thread",
                    interrupted,
                    record.invocation.lane,
                    record.invocation.profile,
                    record.invocation.invocation_id,
                )
                record.completion.set_exception(not_run)
                record.completion.exception()
                return
            notice = None
            terminal_outcome = "failure"
            failure_code, failure_message, failure_data = (
                not_run.code,
                not_run.detail,
                not_run.data,
            )
            logger.warning(
                "Bloodbank turn closed as %s lane=%s profile=%s command_id=%s "
                "(Hermes reported %s)",
                INTERRUPTED_BY_SHUTDOWN_CODE,
                record.invocation.lane,
                record.invocation.profile,
                record.invocation.invocation_id,
                result,
            )
        else:
            # Hermes reports a turn it refused (draining, busy, an error notice in
            # place of an answer) as SUCCESS: its notice is the "reply". Read it.
            notice = (
                self._notices.classify(record.final_reply)
                if terminal_outcome == "success"
                else None
            )
            failure_code = failure_message = failure_data = None
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
        elif terminal_outcome == "success" and is_interrupt_text(record.final_reply):
            # The agent loop abandoned the turn on an interrupt (Hermes' own
            # inactivity stop, say) and its "Operation interrupted ..." line is the
            # whole reply: the work did not finish.
            terminal_outcome = "failure"
            failure_code, failure_message, failure_data = self._notice_failure(
                HermesNotice(OPERATION_INTERRUPTED_REFUSAL, "failed"),
                record.final_reply,
            )
        try:
            events = terminal_events(
                record.invocation,
                outcome=terminal_outcome,
                failure_code=failure_code,
                failure_message=failure_message,
                failure_data=failure_data,
                extra_data=self._turn_annotations(record),
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
            if record.detached_at is not None:
                logger.info(
                    "Bloodbank detached turn closed lane=%s profile=%s command_id=%s "
                    "outcome=%s elapsed=%.1fs",
                    record.invocation.lane,
                    record.invocation.profile,
                    record.invocation.invocation_id,
                    terminal_outcome,
                    time.monotonic() - (record.dispatched_at or time.monotonic()),
                )
            record.completion.set_result(None)

    def _lane_named(self, name: str | None) -> LaneRuntime:
        for lane in self._lanes:
            if lane.name == name:
                return lane
        return self._batch

    def _shutdown_interruption(
        self, record: PendingInvocation, outcome: str
    ) -> str | None:
        """Why Hermes' stop() cut this turn off ("restart" / "shutdown"), or None.

        Primary signal: the runner's shutdown interrupt named this turn's session
        (``_watch_runner_interrupts``). Fallback while stop() is under way (the
        runner cleared ``_running``): a turn cancelled at adapter teardown, or
        one whose whole reply is the agent's "Operation interrupted ..." line. A
        turn that simply finished inside the drain window is neither."""

        if record.shutdown_interrupt is not None:
            return record.shutdown_interrupt
        if not self._runner_stopping():
            return None
        if outcome == "cancelled" or (
            outcome == "success" and is_interrupt_text(record.final_reply)
        ):
            return self._shutdown_kind()
        return None

    @staticmethod
    def _interrupted_not_run(
        record: PendingInvocation, kind: str, outcome: str
    ) -> TurnNotRun:
        elapsed = (
            time.monotonic() - record.dispatched_at
            if record.dispatched_at is not None
            else None
        )
        what = "restarting" if kind == "restart" else "shutting down"
        after = f" after {_seconds(elapsed)}s" if elapsed is not None else ""
        data: dict[str, Any] = {"interrupted_by": f"gateway_{kind}"}
        if elapsed is not None:
            data["elapsed_seconds"] = round(elapsed, 1)
        return TurnNotRun(
            f"gateway_{kind}",
            f"Hermes was {what} and interrupted the turn{after} before it finished "
            f"(its stop drain ran out; Hermes reported {outcome}); partial work may "
            "have run",
            code=INTERRUPTED_BY_SHUTDOWN_CODE,
            data=data,
        )

    @staticmethod
    def _notice_failure(
        notice: HermesNotice, reply: str | None
    ) -> tuple[str, str, dict[str, Any]]:
        """failure_code, failure_message and failure_data for a Hermes error notice."""

        text = " ".join(str(reply or "").split())
        if len(text) > 300:
            text = text[:299] + "…"
        if notice.key.startswith("gateway.errors.") or notice.key == OPERATION_INTERRUPTED_REFUSAL:
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

        if (
            reply_to
            and str(reply_to) in self._model_switch_replies
            and self._model_switch_replies[str(reply_to)] is None
            and isinstance(content, str)
        ):
            # Hermes' answer to an injected `/model --once` (confirmation or error).
            self._model_switch_replies[str(reply_to)] = content
            return SendResult(success=True, message_id=str(uuid.uuid4()))
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
