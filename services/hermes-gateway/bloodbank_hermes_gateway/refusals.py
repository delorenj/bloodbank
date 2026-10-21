"""Hermes replies that mean a Bloodbank command was not executed as asked.

Hermes answers some inbound messages with a notice instead of an agent turn
(the gateway is restarting, another turn holds the session, an unknown slash
command) and reports the turn to the adapter as a plain SUCCESS: the notice
goes out through ``send()`` like any reply. A Bloodbank command has no human
reading that notice, so the gateway reads it itself and decides:

``NOT_RUN``  Hermes refused before starting the turn. Nothing ran, so the
             command is retried (NAK with a delay) and stays dispatchable.
``FAILED``   Hermes ended the turn with an error notice (the agent hit a rate
             limit, the turn lease timed out, the prompt was an unknown slash
             command). Some work may have run, so it is not retried; it is
             closed as an explicit failure.

The notices are rendered with Hermes' own catalog (``agent.i18n.t``) so a copy
change in Hermes moves both sides together. When the catalog cannot be
imported (tests, a Hermes without it) the English copies below are used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

NOT_RUN = "not_run"
FAILED = "failed"

# A free placeholder ({hint}, {command}, ...): only the text before it is matched.
_FREE = "\ue000"
# Below this many significant characters a prefix is too generic to trust.
_MIN_PREFIX = 20


@dataclass(frozen=True)
class HermesNotice:
    """One recognised Hermes notice: its catalog key and what it means for a command."""

    key: str
    disposition: str


# (catalog key, disposition, placeholder names other than {action}, English copy)
_CATALOG: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    # --- refused before a turn started: retry --------------------------------
    (
        "gateway.busy.drain_rejected_new_work",
        NOT_RUN,
        (),
        "⏳ Gateway is {action} and is not accepting new work right now.",
    ),
    (
        "gateway.busy.drain_rejected",
        NOT_RUN,
        (),
        "⏳ Gateway is {action} and is not accepting another turn right now.",
    ),
    (
        "gateway.busy.drain_queued",
        NOT_RUN,
        (),
        "⏳ Gateway {action} — queued for the next turn after it comes back.",
    ),
    (
        "gateway.busy.draining_maintenance",
        NOT_RUN,
        (),
        "⏳ This agent is draining for a maintenance action and isn't accepting "
        "new turns right now. It'll be back in a moment — please resend shortly.",
    ),
    (
        "gateway.errors.interrupted_before_start",
        NOT_RUN,
        (),
        "⚠️ Your message was interrupted before processing started (likely by a "
        "recent /stop). Please send it again.",
    ),
    (
        "gateway.errors.previous_turn_cleanup",
        NOT_RUN,
        (),
        "⚠️ Your message wasn't processed (the previous turn was still being "
        "cleaned up). Please send it again.",
    ),
    (
        "gateway.errors.history_unavailable",
        NOT_RUN,
        (),
        "⚠️ This session's history is temporarily unavailable, so this message "
        "was not processed.",
    ),
    # --- a turn ran (or waited) and ended in an error notice: explicit failure -
    (
        # The turn lease wait (up to 30 min) timed out behind another turn on
        # the same Hermes session; a retry would wait the same 30 min again.
        "gateway.busy.another_turn_running",
        FAILED,
        (),
        "⏳ Another turn is still running on this session. To protect the "
        "transcript, this message was not processed.",
    ),
    (
        "gateway.unknown_command",
        FAILED,
        ("command",),
        "Unknown command `/{command}`. Type /commands to see what's available, "
        "or resend without the leading slash to send as a regular message.",
    ),
    (
        # Rate limits, usage limits, expired sign-in, quota, overload, rejected
        # requests: the hint is appended to this sentence.
        "gateway.errors.generic_failed_with_hint",
        FAILED,
        ("hint",),
        "⚠️ Something went wrong and I couldn't finish this reply.{hint}",
    ),
    (
        "gateway.errors.generic_failed",
        FAILED,
        (),
        "⚠️ Something went wrong and I couldn't finish this reply. Use /retry to "
        "try again, or /new to start a fresh conversation.",
    ),
    (
        "gateway.errors.rate_limited",
        FAILED,
        (),
        "⏱️ The AI model service is rate-limiting requests. Wait a moment, then use /retry.",
    ),
    (
        "gateway.errors.auth_failed",
        FAILED,
        (),
        "⚠️ Sign-in to the AI model service failed.",
    ),
    (
        "gateway.errors.usage_limit_resets",
        FAILED,
        ("window",),
        "⏱️ The AI model service's usage limit is reached; it resets in {window}.",
    ),
    (
        "gateway.errors.provider_kept_failing",
        FAILED,
        (),
        "⚠️ The AI model service kept failing. Use /retry to try again, or /model "
        "to switch models.",
    ),
    (
        "gateway.errors.no_credentials",
        FAILED,
        (),
        "⚠️ I couldn't connect to the AI model service, so this message wasn't "
        "processed.",
    ),
    (
        "gateway.errors.unreachable",
        FAILED,
        (),
        "⚠️ The AI model service isn't reachable right now",
    ),
    (
        "gateway.errors.connection_interrupted",
        FAILED,
        (),
        "⚠️ The connection to the AI model service was interrupted mid-request",
    ),
    (
        "gateway.errors.no_response",
        FAILED,
        (),
        "⚠️ Processing completed but no response was generated.",
    ),
)

# Hermes' max_concurrent_sessions refusal is not in the catalog
# (hermes_cli.active_sessions.active_session_limit_message): capacity, retry.
_SESSION_LIMIT = HermesNotice("hermes_cli.active_session_limit", NOT_RUN)
_SESSION_LIMIT_PREFIX = "Hermes is at the active session limit ("

_ACTIONS = (
    ("gateway.busy.action_restarting", "restarting"),
    ("gateway.busy.action_shutting_down", "shutting down"),
)


def _significant(text: str) -> str:
    """Letters, digits and single spaces, lowercased: emoji and markdown never decide a match."""

    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip().lower()


def _hermes_translate() -> Callable[..., str] | None:
    try:
        from agent.i18n import t  # type: ignore[import-not-found]
    except Exception:
        return None
    return t


class NoticeClassifier:
    """Recognise a Hermes notice in a command turn's final reply."""

    def __init__(self, translate: Callable[..., str] | None = None) -> None:
        self._translate = translate
        self._patterns: list[tuple[tuple[str, ...], HermesNotice]] | None = None

    def _render(self, key: str, english: str, **kwargs: Any) -> set[str]:
        out = set()
        for lang in (None, "en"):
            template = None
            if self._translate is not None:
                try:
                    template = (
                        self._translate(key, **kwargs)
                        if lang is None
                        else self._translate(key, lang=lang, **kwargs)
                    )
                except Exception:
                    template = None
                if template == key:  # catalog miss: Hermes returns the bare key
                    template = None
            if template is not None:
                out.add(template)
        try:
            out.add(english.format(**kwargs))
        except (KeyError, IndexError, ValueError):
            pass
        return out

    def _build(self) -> list[tuple[tuple[str, ...], HermesNotice]]:
        translate = self._translate
        actions = set()
        for key, english in _ACTIONS:
            actions.add(english)
            if translate is not None:
                try:
                    value = translate(key)
                except Exception:
                    value = key
                if value and value != key:
                    actions.add(value)
        patterns: dict[tuple[str, ...], HermesNotice] = {}
        for key, disposition, free, english in _CATALOG:
            notice = HermesNotice(key, disposition)
            kwargs_sets = (
                [{"action": action} for action in sorted(actions)]
                if "{action}" in english
                else [{}]
            )
            for kwargs in kwargs_sets:
                kwargs = {**kwargs, **{name: _FREE for name in free}}
                for rendered in self._render(key, english, **kwargs):
                    # The fixed text around each free placeholder, in order.
                    segments = tuple(
                        _significant(part) for part in rendered.split(_FREE)
                    )
                    if segments[0] and sum(map(len, segments)) >= _MIN_PREFIX:
                        patterns.setdefault(segments, notice)
        patterns.setdefault((_significant(_SESSION_LIMIT_PREFIX),), _SESSION_LIMIT)
        # Most specific first: a notice wins over a shorter one it extends.
        return sorted(patterns.items(), key=lambda item: -sum(map(len, item[0])))

    @staticmethod
    def _matches(text: str, segments: tuple[str, ...]) -> bool:
        """The reply starts with the notice's opening text and holds the rest in order."""

        if not text.startswith(segments[0]):
            return False
        position = len(segments[0])
        for segment in segments[1:]:
            if not segment:
                continue
            found = text.find(segment, position)
            if found < 0:
                return False
            position = found + len(segment)
        return True

    def classify(self, reply: Any) -> HermesNotice | None:
        if not isinstance(reply, str) or not reply.strip():
            return None
        if self._patterns is None:
            if self._translate is None:
                self._translate = _hermes_translate()
            self._patterns = self._build()
        text = _significant(reply)
        for segments, notice in self._patterns:
            if self._matches(text, segments):
                return notice
        return None
