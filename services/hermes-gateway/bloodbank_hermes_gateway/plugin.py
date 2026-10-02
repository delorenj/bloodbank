"""Adapt the pinned Hermes turn-finalizer hooks to one scoped command result."""

from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from gateway.response_filters import is_intentional_silence_response

from .contract import Invocation, final_answer_event, terminal_events, validate_fact
from .execution_state import ExecutionRecord, ExecutionStateStore


@dataclass
class CommandBinding:
    invocation: Invocation
    digest: str
    store: ExecutionStateStore
    native: tuple[str, str, str] | None = None
    candidate: str | None = None
    candidate_native: tuple[str, str, str] | None = None
    result: ExecutionRecord | None = None
    error: Exception | None = None
    end_flags: tuple[Any, Any, Any] | None = None
    closed: bool = False
    lock: Any = field(default_factory=threading.RLock)

    def observe(self, phase: str, payload: dict[str, Any]) -> None:
        with self.lock:
            if self.closed or self.error is not None:
                return
            try:
                self._observe(phase, payload)
            except Exception as exc:
                # Hermes swallows observer exceptions. Keep a sticky error for
                # on_processing_complete so a failed capture cannot become ACK.
                self.error = exc
                raise

    def _observe(self, phase: str, payload: dict[str, Any]) -> None:
        native = (
            payload.get("session_id"),
            payload.get("turn_id"),
            payload.get("task_id"),
        )
        if phase == "pre":
            if payload.get("platform") != "bloodbank" or payload.get(
                "parent_session_id"
            ):
                return
            if any(not isinstance(value, str) or not value for value in native):
                raise ValueError(
                    "root finalizer must expose native session, turn and task IDs"
                )
            if self.native is None:
                self.native = native
            # Nested invocations inherit the binding but must never replace it.
            return
        # Compaction may rotate the native session during a root turn. Its
        # task/turn remain stable; delegated invocations have their own turn.
        if self.native is None or native[1:] != self.native[1:]:
            return
        if phase == "post":
            text = payload.get("assistant_response")
            if not isinstance(text, str) or not text.strip():
                return
            if is_intentional_silence_response(text):
                return
            if self.candidate is not None and (
                self.candidate != text or self.candidate_native != native
            ):
                raise RuntimeError("conflicting final assistant capture")
            if not isinstance(native[0], str) or not native[0]:
                raise ValueError("finalizer must expose the observed native session")
            self.candidate = text
            self.candidate_native = native
            return
        flags = tuple(
            payload.get(key) for key in ("completed", "failed", "interrupted")
        )
        if any(type(flag) is not bool for flag in flags):
            raise ValueError("finalizer outcome flags must be explicit booleans")
        if self.end_flags is not None:
            if flags != self.end_flags:
                raise RuntimeError("conflicting final execution outcome")
            return
        completed, failed, interrupted = flags
        outcome = (
            "cancelled"
            if interrupted
            else "success"
            if completed and not failed
            else "failure"
        )
        events: tuple[dict[str, Any], ...] = terminal_events(
            self.invocation, outcome=outcome
        )
        if outcome == "success" and self.candidate is not None:
            assert self.candidate_native is not None
            answer = final_answer_event(
                self.invocation,
                role="assistant",
                final_answer=True,
                text=self.candidate,
                native_session_id=self.candidate_native[0],
                native_turn_id=self.candidate_native[1],
            )
            events = (answer, *events)
        for event in events:
            validate_fact(event)
        self.result = self.store.mark_completed(
            command_id=self.invocation.invocation_id,
            digest=self.digest,
            outcome=outcome,
            terminal_events=events,
        )
        self.end_flags = flags


command_binding: ContextVar[CommandBinding | None] = ContextVar(
    "bloodbank_command_binding", default=None
)


def _observe(phase: str, payload: dict[str, Any]) -> None:
    binding = command_binding.get()
    if binding is not None:
        binding.observe(phase, payload)


def pre_llm_call(**payload: Any) -> None:
    _observe("pre", payload)


def post_llm_call(**payload: Any) -> None:
    # This callback is the discriminator: Hermes fires it once after final
    # output transformation, never for token/tool/history/reasoning callbacks.
    _observe("post", payload)


def on_session_end(**payload: Any) -> None:
    _observe("end", payload)


def register_hooks(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
