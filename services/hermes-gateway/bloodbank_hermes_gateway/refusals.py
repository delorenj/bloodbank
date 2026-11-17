"""Hermes replies that mean a Bloodbank command was not executed as asked.

Hermes answers some inbound messages with a notice instead of an agent turn
(the gateway is restarting, another turn holds the session, an unknown slash
command) and reports the turn to the adapter as a plain SUCCESS: the notice
goes out through ``send()`` like any reply. A Bloodbank command has no human
reading that notice, so the gateway reads it itself and decides:

``NOT_RUN``  Hermes refused before starting the turn. Nothing ran, so the
             command is retried (NAK with a delay) and stays dispatchable.
             A false positive here runs the command twice, so a NOT_RUN
             notice must be the WHOLE reply (a short trailing footer aside),
             never just its opening words.
``FAILED``   Hermes ended the turn with an error notice instead of an answer
             (the agent hit a rate limit, session storage failed mid-turn, the
             turn lease timed out, the prompt was an unknown slash command).
             Some work may have run, so it is not retried; it is closed as an
             explicit failure. A reply that opens with the notice matches.

The notices come from Hermes' own i18n catalog: every ``gateway.errors.*`` key
it ships is a FAILED notice unless it is a known fragment (a hint appended to
another notice) or listed in ``NOT_RUN_ERRORS``, so a new Hermes error notice
can never fall through as a success. The English copies below are used when the
catalog cannot be imported (tests, a Hermes without it) and give the busy /
drain / slash-command notices outside ``gateway.errors`` their disposition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from string import Formatter
from typing import Any, Callable, Iterable, Mapping

NOT_RUN = "not_run"
FAILED = "failed"

# Below this many significant characters of fixed text a notice is too generic to trust.
_MIN_FIXED = 20
# What may follow a whole NOT_RUN notice: Hermes' optional runtime footer
# (display.runtime_footer) or a short "send it again" coda, never an answer.
_MAX_NOT_RUN_TAIL = 200

ERRORS_PREFIX = "gateway.errors."
# gateway.errors.* keys that are pieces of another notice, never a reply on their own.
_FRAGMENT_SUFFIXES = ("_label", "_joiner")
_FRAGMENT_PREFIXES = ("hint_",)
_FRAGMENT_KEYS = frozenset({"gateway.errors.processing_incomplete"})

# gateway.errors.* notices that are sent INSTEAD of starting the turn: retried.
NOT_RUN_ERRORS = frozenset(
    {
        "gateway.errors.interrupted_before_start",
        "gateway.errors.previous_turn_cleanup",
        "gateway.errors.history_unavailable",
    }
)

# Busy/drain/slash notices outside gateway.errors and their disposition, plus the
# English copy of every notice the gateway knows by name.
_ENGLISH: dict[str, tuple[str, str]] = {
    # --- refused before a turn started: retry --------------------------------
    "gateway.busy.drain_rejected_new_work": (
        NOT_RUN,
        "⏳ Gateway is {action} and is not accepting new work right now.",
    ),
    "gateway.busy.drain_rejected": (
        NOT_RUN,
        "⏳ Gateway is {action} and is not accepting another turn right now.",
    ),
    "gateway.busy.draining_maintenance": (
        NOT_RUN,
        "⏳ This agent is draining for a maintenance action and isn't accepting "
        "new turns right now. It'll be back in a moment — please resend shortly.",
    ),
    "gateway.errors.interrupted_before_start": (
        NOT_RUN,
        "⚠️ Your message was interrupted before processing started (likely by a "
        "recent /stop). Please send it again.",
    ),
    "gateway.errors.previous_turn_cleanup": (
        NOT_RUN,
        "⚠️ Your message wasn't processed (the previous turn was still being "
        "cleaned up). Please send it again.",
    ),
    "gateway.errors.history_unavailable": (
        NOT_RUN,
        "⚠️ This session's history is temporarily unavailable, so this message "
        "was not processed. Ask the operator to inspect state.db, then resend "
        "after it is healthy.",
    ),
    # --- refused, and a retry cannot help: explicit failure ------------------
    "gateway.busy.drain_queued": (
        # Only sent when busy_input_mode is queue/steer: Hermes kept its OWN copy
        # of the message for after the restart, so a NAK would run it twice.
        FAILED,
        "⏳ Gateway {action} — queued for the next turn after it comes back.",
    ),
    "gateway.busy.another_turn_running": (
        # The turn lease wait (up to 30 min) timed out behind another turn on
        # the same Hermes session; a retry would wait the same 30 min again.
        FAILED,
        "⏳ Another turn is still running on this session. To protect the "
        "transcript, this message was not processed. Wait for the active turn "
        "to finish, then resend it.",
    ),
    "gateway.unknown_command": (
        FAILED,
        "Unknown command `/{command}`. Type /commands to see what's available, "
        "or resend without the leading slash to send as a regular message.",
    ),
    # --- a turn ran and ended in an error notice: explicit failure ------------
    "gateway.errors.generic_failed_with_hint": (
        # Rate limits, usage limits, expired sign-in, quota, overload, rejected
        # requests: the hint is inserted into this sentence.
        FAILED,
        "⚠️ Something went wrong and I couldn't finish this reply.{hint}\nUse "
        "/retry to try again, or /new to start a fresh conversation.",
    ),
    "gateway.errors.generic_failed": (
        FAILED,
        "⚠️ Something went wrong and I couldn't finish this reply. Use /retry to "
        "try again, or /new to start a fresh conversation.",
    ),
    "gateway.errors.rate_limited": (
        FAILED,
        "⏱️ The AI model service is rate-limiting requests. Wait a moment, then use /retry.",
    ),
    "gateway.errors.auth_failed": (
        FAILED,
        "⚠️ Sign-in to the AI model service failed.",
    ),
    "gateway.errors.usage_limit_resets": (
        FAILED,
        "⏱️ The AI model service's usage limit is reached; it resets in {window}.",
    ),
    "gateway.errors.provider_kept_failing": (
        FAILED,
        "⚠️ The AI model service kept failing. Use /retry to try again, or /model "
        "to switch models.",
    ),
    "gateway.errors.no_credentials": (
        FAILED,
        "⚠️ I couldn't connect to the AI model service, so this message wasn't processed.",
    ),
    "gateway.errors.unreachable": (
        FAILED,
        "⚠️ The AI model service isn't reachable right now",
    ),
    "gateway.errors.connection_interrupted": (
        FAILED,
        "⚠️ The connection to the AI model service was interrupted mid-request",
    ),
    "gateway.errors.connection_unknown": (
        FAILED,
        "⚠️ Hermes could not reach the AI model service (no further detail from the SDK).",
    ),
    "gateway.errors.bad_request": (
        FAILED,
        "⚠️ The AI model service rejected this request.",
    ),
    "gateway.errors.no_response": (
        FAILED,
        "⚠️ Processing completed but no response was generated.",
    ),
    "gateway.errors.context_overflow": (
        FAILED,
        "⚠️ This conversation has grown too long for me to read all at once.",
    ),
    "gateway.errors.stopped_before_finishing": (
        FAILED,
        "⚠️ I had to stop before finishing{reason}. Use /retry to try again, or "
        "/compress if this conversation has grown very long.",
    ),
    "gateway.errors.unexpected_silence": (
        FAILED,
        "⚠️ The model returned only a silence marker for a message that needed a reply.",
    ),
    # Session persistence failed inside a tool round: earlier rounds' tools may
    # already have run, so it is not retried even though the copy says "send it again".
    "gateway.errors.session_storage_unavailable": (
        FAILED,
        "⚠️ Session storage was temporarily unavailable, so this turn was stopped "
        "to protect your conversation history.",
    ),
    "gateway.errors.session_storage_unavailable_disk": (
        FAILED,
        "⚠️ Session storage was temporarily unavailable, so this turn was stopped "
        "to protect your conversation history. Please check available disk space",
    ),
    # Hermes' max_concurrent_sessions refusal is not in the catalog
    # (hermes_cli.active_sessions.active_session_limit_message): capacity, retry.
    "hermes_cli.active_session_limit": (
        NOT_RUN,
        "Hermes is at the active session limit ({count}/{limit}).{detail} Try again "
        "when another session finishes.",
    ),
}

_ACTION_KEYS = (
    ("gateway.busy.action_restarting", "restarting"),
    ("gateway.busy.action_shutting_down", "shutting down"),
    ("gateway.shutdown.action_restarting", "restarting"),
    ("gateway.shutdown.action_shutting_down", "shutting down"),
)

# The agent loop's own final text for a turn it abandoned on an interrupt
# ("Operation interrupted: waiting for model response (12.3s elapsed).", "...
# during retry (...)", "...: handling API error (...)"); ACP and the TUI treat
# this prefix as cancellation metadata, not assistant prose.
_INTERRUPT_TEXT = re.compile(r"\A\s*Operation interrupted\b[^\n]{0,280}\Z")


@dataclass(frozen=True)
class HermesNotice:
    """One recognised Hermes notice: its catalog key and what it means for a command."""

    key: str
    disposition: str


def is_fragment_key(key: str) -> bool:
    """A gateway.errors.* key that only ever appears inside another notice."""

    if key in _FRAGMENT_KEYS:
        return True
    if not key.startswith(ERRORS_PREFIX):
        return False
    leaf = key[len(ERRORS_PREFIX) :]
    return leaf.startswith(_FRAGMENT_PREFIXES) or leaf.endswith(_FRAGMENT_SUFFIXES)


def disposition_for(key: str) -> str | None:
    """NOT_RUN / FAILED for a notice key, None for a key that is not a reply notice."""

    known = _ENGLISH.get(key)
    if known is not None:
        return known[0]
    if key.startswith(ERRORS_PREFIX) and not is_fragment_key(key):
        return NOT_RUN if key in NOT_RUN_ERRORS else FAILED
    return None


def is_interrupt_text(reply: Any) -> bool:
    """The reply is the agent loop's "Operation interrupted ..." abandonment line."""

    return isinstance(reply, str) and bool(_INTERRUPT_TEXT.match(reply))


def _significant(text: str) -> str:
    """Letters, digits and single spaces, lowercased: emoji and markdown never decide a match."""

    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip().lower()


def _hermes_catalogs() -> list[Mapping[str, str]] | None:
    """Hermes' merged catalogs for English and the active language, or None."""

    try:
        from agent.i18n import (  # type: ignore[import-not-found]
            DEFAULT_LANGUAGE,
            _load_catalog,
            get_language,
        )
    except Exception:
        return None
    catalogs: list[Mapping[str, str]] = []
    for lang in dict.fromkeys((DEFAULT_LANGUAGE, get_language())):
        try:
            catalog = _load_catalog(lang)
        except Exception:
            continue
        if isinstance(catalog, Mapping):
            catalogs.append(catalog)
    return catalogs or None


def _fixed_parts(template: str, action: str | None) -> list[str] | None:
    """The template's fixed text split at every placeholder ({action} filled in)."""

    parts: list[str] = []
    current = ""
    try:
        parsed = list(Formatter().parse(template))
    except (ValueError, TypeError):
        return None
    for literal, field_name, _spec, _conversion in parsed:
        current += literal
        if field_name is None:
            continue
        if field_name == "action" and action is not None:
            current += action
            continue
        parts.append(current)
        current = ""
    parts.append(current)
    return parts


@dataclass(frozen=True)
class _Pattern:
    notice: HermesNotice
    regex: re.Pattern[str]
    fixed: int
    # NOT_RUN: how many raw lines of the reply the notice spans.
    lines: int


class NoticeClassifier:
    """Recognise a Hermes notice in a command turn's final reply."""

    def __init__(
        self,
        catalogs: Iterable[Mapping[str, str]] | Callable[[], Any] | None = None,
    ) -> None:
        self._catalogs = catalogs
        self._patterns: list[_Pattern] | None = None

    def _resolve_catalogs(self) -> list[Mapping[str, str]]:
        source = self._catalogs
        if source is None:
            source = _hermes_catalogs
        if callable(source):
            try:
                source = source()
            except Exception:
                source = None
        return [c for c in (source or ()) if isinstance(c, Mapping)]

    @staticmethod
    def _actions(catalogs: list[Mapping[str, str]]) -> set[str]:
        actions = {english for _key, english in _ACTION_KEYS}
        for catalog in catalogs:
            for key, _english in _ACTION_KEYS:
                value = catalog.get(key)
                if isinstance(value, str) and value.strip():
                    actions.add(value)
        return actions

    def _build(self) -> list[_Pattern]:
        catalogs = self._resolve_catalogs()
        actions = self._actions(catalogs)
        templates: dict[str, set[str]] = {}
        for key, (_disposition, english) in _ENGLISH.items():
            templates.setdefault(key, set()).add(english)
        for catalog in catalogs:
            for key, value in catalog.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                if disposition_for(key) is not None:
                    templates.setdefault(key, set()).add(value)
        patterns: dict[tuple[str, str], _Pattern] = {}
        for key, variants in templates.items():
            disposition = disposition_for(key)
            if disposition is None:
                continue
            notice = HermesNotice(key, disposition)
            for template in variants:
                options = sorted(actions) if "{action}" in template else [None]
                for action in options:
                    pattern = self._compile(notice, template, action)
                    if pattern is not None:
                        patterns.setdefault((key, pattern.regex.pattern), pattern)
        # Most specific first: a notice wins over a shorter one it extends.
        return sorted(patterns.values(), key=lambda p: -p.fixed)

    @staticmethod
    def _compile(notice: HermesNotice, template: str, action: str | None) -> _Pattern | None:
        parts = _fixed_parts(template, action)
        if parts is None:
            return None
        segments = [_significant(part) for part in parts]
        fixed = sum(map(len, segments))
        if not segments[0] or fixed < _MIN_FIXED:
            return None
        body = r".*?".join(re.escape(segment) for segment in segments if segment)
        if notice.disposition == NOT_RUN:
            # The whole notice, a trailing free placeholder aside.
            tail = r".*" if not segments[-1] else ""
            regex = re.compile(rf"\A{body}{tail}\Z", re.DOTALL)
        else:
            regex = re.compile(rf"\A{body}", re.DOTALL)
        lines = template.strip().count("\n") + 1
        return _Pattern(notice, regex, fixed, lines)

    def _ensure(self) -> list[_Pattern]:
        if self._patterns is None:
            self._patterns = self._build()
        return self._patterns

    def classify(self, reply: Any) -> HermesNotice | None:
        if not isinstance(reply, str) or not reply.strip():
            return None
        whole = _significant(reply)
        raw_lines = reply.strip().split("\n")
        for pattern in self._ensure():
            if pattern.notice.disposition == NOT_RUN:
                head = _significant("\n".join(raw_lines[: pattern.lines]))
                tail = _significant("\n".join(raw_lines[pattern.lines :]))
                if len(tail) <= _MAX_NOT_RUN_TAIL and pattern.regex.match(head):
                    return pattern.notice
            elif pattern.regex.match(whole):
                return pattern.notice
        return None
