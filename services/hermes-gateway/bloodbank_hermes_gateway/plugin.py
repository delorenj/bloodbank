"""Adapt the pinned Hermes turn-finalizer hooks to one scoped command result."""

from __future__ import annotations

import re
import threading
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from gateway.response_filters import is_intentional_silence_response

from .contract import (
    Invocation,
    RouteInvalid,
    final_answer_event,
    terminal_events,
    validate_fact,
)
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
    end_flags: tuple[Any, ...] | None = None
    post_seen: bool = False
    post_evidence: tuple[Any, ...] | None = None
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
            evidence = (text, *native)
            if self.post_seen and evidence != self.post_evidence:
                raise RuntimeError("conflicting final assistant capture")
            self.post_seen = True
            self.post_evidence = evidence
            if not isinstance(text, str) or not text.strip():
                return
            if is_intentional_silence_response(text):
                return
            if not isinstance(native[0], str) or not native[0]:
                raise ValueError("finalizer must expose the observed native session")
            self.candidate = re.sub(r"[\ud800-\udfff]", "\ufffd", text)
            self.candidate_native = native
            return
        flags = tuple(
            payload.get(key) for key in ("completed", "failed", "interrupted")
        )
        if any(type(flag) is not bool for flag in flags):
            raise ValueError("finalizer outcome flags must be explicit booleans")
        reason = payload.get("turn_exit_reason")
        evidence = (*flags, reason, *native)
        if self.end_flags is not None:
            if evidence != self.end_flags:
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
        eligible = reason in {
            "text_response(finish_reason=stop)",
            "text_response(finish_reason=end_turn)",
            "text_response(finish_reason=None)",
        }
        if outcome == "success" and eligible and self.candidate is not None:
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
        self.end_flags = evidence


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
    _observe("post", payload)


def on_session_end(**payload: Any) -> None:
    _observe("end", payload)


class CaptureUnavailable(RouteInvalid):
    reason = "final_answer_capture_unavailable_before_dispatch"


def prepare_capture(handler: Any, event: Any) -> None:
    from gateway.run import _load_gateway_config, _profile_runtime_scope
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import get_hermes_home

    owner = getattr(handler, "__self__", None)
    if owner is None:
        for cell in getattr(handler, "__closure__", ()) or ():
            value = cell.cell_contents
            if callable(getattr(value, "_resolve_session_agent_runtime", None)):
                owner = value
                break
    if owner is None or not callable(
        getattr(owner, "_resolve_session_agent_runtime", None)
    ):
        raise CaptureUnavailable("unsupported Hermes handler capture interface")
    if owner._get_proxy_url():
        raise CaptureUnavailable("proxy runtime has no local final-answer observer")
    expected_home = get_profile_dir(event.source.profile).resolve()
    multiplex = getattr(owner.config, "multiplex_profiles", False)
    home = (
        owner._resolve_profile_home_for_source(event.source).resolve()
        if multiplex
        else get_hermes_home().resolve()
    )
    if home != expected_home:
        raise CaptureUnavailable("execution home differs from the resolved profile")
    scope = _profile_runtime_scope(home) if multiplex else nullcontext()
    with scope:
        config = _load_gateway_config()
        model, runtime = owner._resolve_session_agent_runtime(
            source=event.source, user_config=config
        )
        route = owner._resolve_turn_agent_config(event.text, model, runtime)
        if route["runtime"].get("api_mode") not in {
            "chat_completions", "anthropic_messages", "codex_responses"
        }:
            raise CaptureUnavailable("runtime bypasses the supported finalizer")
        discover_plugins()
        manager = get_plugin_manager()
        for name, callback in (
            ("pre_llm_call", pre_llm_call),
            ("post_llm_call", post_llm_call),
            ("on_session_end", on_session_end),
        ):
            if callback not in manager.iter_hook_callbacks(name):
                raise CaptureUnavailable("resolved profile lacks the final-answer observer")


def register_hooks(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
