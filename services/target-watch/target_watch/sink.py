"""`target-watch sink`: watch.target.detected in; a ticket, a notification and watch.target.routed out.

Safety rules, each paid for elsewhere:
- A ticket create is never retried blindly. The intent is written to sqlite
  before px runs; a redelivery that finds an intent without an outcome marks
  the detection failed and notifies, instead of filing a second ticket.
- px success is read from its FIRST matching output line ('created JIMB-450 ...',
  'commented on JIMB-450'), never from the last line (the URL).
- Everything is idempotent per detection id: a redelivered detection that was
  already routed only republishes its stored routed event.
- A "ring:<E.164>" or "ring:sip:<user>@<host>" channel places a Telnyx Call Control call so
  the operator's phone (or SIP device) rings like an incoming call. It never carries speech,
  never logs the API key, rings at most once per cooldown per (watch, channel), and goes after
  the Telegram line. An accepted call is then followed in the background to its terminal
  outcome (ring_answered / ring_unanswered / ring_rejected), because Telnyx accepting the
  dial says nothing about the phone ringing.
- An "ntfy:<topic>" channel is the loud push: an urgent ntfy message, sent FIRST, before Telegram
  (which lands in a chat full of agent chatter) and before the ring. Its token comes from 1Password
  through the op CLI and lives in memory only.
- An "earshot:<channel>" channel speaks the alert: the earshot CLI synthesises a line and drops it into that
  earshot channel (an audible alarm on the operator's phone). It obeys the ring's cooldown and quiet hours. The
  earshot bucket can be listed and read by anyone, so the line says who wrote and how, never what (unless the
  target sets earshot_content), and the object is named by an opaque slug, never by the line.
- A target whose sink says `ticket = "button"` files nothing when a message arrives: the route is
  `awaiting_button`, and the ntfy push carries a "Create Ticket" button. The tap is an HMAC-signed POST that
  this process answers (button.py); it files exactly what auto mode would have, once, and says so on ntfy.
- Every channel attempt is written to the sqlite `notifications` table, and a ring's row is updated
  when its follower reaches a terminal outcome: journald keeps almost nothing for these units, so
  `target-watch status` reads that table to say what each channel did.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from . import button as btn
from . import envelope as env
from .bus import Permanent
from .log import log
from .matcher import epoch, text_hash
from .state import State
from .targets import Target, TargetRegistry, in_quiet_hours

CREATED = re.compile(r"^created ([A-Z][A-Z0-9]*-[0-9]+)\b")
COMMENTED = re.compile(r"^commented on ([A-Z][A-Z0-9]*-[0-9]+)\b")
TEXT_DUPLICATE_S = 60.0
# The carrier delivers one text as sms and rcs (and mms, iMessage) twins; no other providers are folded by text.
CARRIER_TWINS = frozenset({"sms", "mms", "rcs", "imessage"})
PX_TIMEOUT_S = 90
AWAITING_BUTTON = "awaiting_button"
NOTIFY_TIMEOUT_S = 30
HERMES_PROFILE = os.environ.get("TARGET_WATCH_HERMES_PROFILE") or "33god-pm"
NOTIFY_CWD = os.environ.get("TARGET_WATCH_NOTIFY_CWD") or os.path.expanduser("~")

RING_PREFIX = "ring:"
SIP_RING_PREFIX = "ring:sip:"
NTFY_PREFIX = "ntfy:"
EARSHOT_PREFIX = "earshot:"
TELNYX_CALLS_URL = "https://api.telnyx.com/v2/calls"
# An E.164 ring stops after 20 s: it must stay under the cell carrier's no-answer forward (about
# 25-30 s on Verizon), which sends an unanswered call back to the same Telnyx number: keep it <= 22.
RING_TIMEOUT_SECS = 20
RING_TIME_LIMIT_SECS = 40  # an answered call is silent and ends here
# A SIP target has no carrier in the path, so no forward to outrun. It is a push-woken softphone, which
# spends several seconds of the ring waking up: every real ring of 2026-11-05..11 timed out at 20 s
# unanswered. 45 s gives a person time to look, and the time limit must exceed it.
RING_SIP_TIMEOUT_SECS = 45
RING_SIP_TIME_LIMIT_SECS = 60
RING_HTTP_TIMEOUT_S = 15  # urllib applies this to each socket operation, so it is not a wall-clock cap...
OP_TIMEOUT_S = 20
RING_DEADLINE_S = 40  # ...this is: key lookup plus the POST, run off the event loop and abandoned after this long
# A detection older than this is not live news: after a sink outage or a nak/redelivery (30 s * attempt,
# five attempts, about 5 minutes in all) it must not ring the phone as if Jim had just written.
RING_MAX_AGE_S = 300
RING_TZ = ZoneInfo("America/New_York")
TELNYX_CALL_EVENTS_URL = "https://api.telnyx.com/v2/call_events"
# The outcome lookup starts after the ring has had time to time out (+5 s for Telnyx to write the hangup
# event), then re-reads for a hangup that has not landed yet. On 2026-11-11 the call_events of a ring that
# hung up at +20 s were still empty ~37 s after the dial and readable later, so the window is 12 reads
# 5 s apart: the last is 60 s past the ring's timeout, and the follower is bounded by that. An answered
# call stays up until the time limit; the answer event is enough to call it answered.
RING_OUTCOME_LEAD_S = 5
RING_OUTCOME_DELAY_S = RING_TIMEOUT_SECS + RING_OUTCOME_LEAD_S
RING_SIP_OUTCOME_DELAY_S = RING_SIP_TIMEOUT_SECS + RING_OUTCOME_LEAD_S
RING_OUTCOME_POLLS = 12
RING_OUTCOME_POLL_S = 5
# A busy phone (the operator is on another call: user_busy, SIP 486/600/603) is retried once. 100 s is long
# enough for a short call to end and short enough that the news is still fresh; it is a constant, not a
# loop: the retry never schedules another retry.
RING_BUSY_RETRY_S = 100
BUSY_SIP_CODES = frozenset({"486", "600", "603"})
# Telnyx refuses a dial when the outbound profile has no free channel, which is what "the operator is on
# another call" looks like from the API (2026-11-11: 90042 "OB profile channel limit exceeded"). It is
# treated like a busy phone: one retry after RING_BUSY_RETRY_S.
CHANNEL_LIMIT_CODES = frozenset({"90042"})
CHANNEL_LIMIT_TEXT = re.compile(r"channel limit|concurren", re.IGNORECASE)
# Telnyx's from_display_name: at most 128 characters of letters, digits, spaces and - _ ~ ! . +
DISPLAY_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9 _~!.+-]")
DISPLAY_NAME_MAX = 128
DEFAULT_KEY_REF = "op://DeLoSecrets/74k7nz3xkryqns4lpf65a323ue/credential"
# Cloudflare-fronted APIs refuse Python-urllib's default User-Agent.
RING_USER_AGENT = "target-watch/0.1 (bloodbank)"
NTFY_DEFAULT_URL = "https://ntfy.delo.sh"
NTFY_DEFAULT_ENV_FILE = "~/.config/infra-alerts/env"
NTFY_HTTP_TIMEOUT_S = 15
NTFY_DEADLINE_S = 30  # token lookup plus the POST, run off the event loop and abandoned after this long
URL_LINE = re.compile(r"^https://\S+$")
# earshot speaks the line aloud: a URL would be read out character by character.
URL_IN_TEXT = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
EARSHOT_DEFAULT_BIN = "~/.local/bin/earshot"
EARSHOT_TIMEOUT_S = 45  # synthesis plus upload
# The earshot bucket is anonymous list + read (the phone carries no credentials), and the CLI names an object after
# the first 40 characters of what it speaks unless told otherwise: every drop of ours is named by this word instead,
# so a listing never shows who wrote or what. An earshot without --slug refuses it (exit 2) and uploads nothing.
EARSHOT_SLUG = "target-watch"
SPOKEN_MAX = 200
BUTTON_PATH = re.compile(r"^/watch/ticket/([^/?#]+)$")
BUTTON_BAD_SIGNATURE = "bad or expired signature"
BUTTON_SECRET_MIN = 32  # characters; the 1Password value is 64
BUTTON_SECRET_RETRY_S = 60  # an unreadable secret is not asked for again for this long (unsigned callers must not drive op)
FOOTER = "Filed automatically by target-watch from a {provider} message. Do not invent a ticket number."


def run_command(argv: list[str], cwd: str | None = None, timeout: float = 60) -> tuple[int, str, str]:
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "") if isinstance(exc.stdout, str) else "", "timeout"
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}"
    return done.returncode, done.stdout or "", done.stderr or ""


def is_ring(channel: str) -> bool:
    return channel.startswith(RING_PREFIX)


def is_ntfy(channel: str) -> bool:
    return channel.startswith(NTFY_PREFIX)


def is_earshot(channel: str) -> bool:
    return channel.startswith(EARSHOT_PREFIX)


def channel_rank(channel: str) -> int:
    """Send order: ntfy (the loud one) first, then earshot (spoken), then the quiet text channels, then the
    ring. The line the operator reads after the phone rings must already be there."""
    return 0 if is_ntfy(channel) else 1 if is_earshot(channel) else 3 if is_ring(channel) else 2


def button_base_url() -> str:
    return (os.environ.get("TARGET_WATCH_BUTTON_URL") or btn.DEFAULT_BUTTON_URL).rstrip("/")


def trim_words(text: str, limit: int = SPOKEN_MAX) -> str:
    """At most `limit` characters, cut at a word boundary (the cut is marked with '...')."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    if text[limit - 3] != " ":
        boundary = cut.rfind(" ")
        if boundary >= limit // 2:  # a long unbroken token is cut hard rather than dropped back to the prefix
            cut = cut[:boundary]
    return cut.rstrip(" ,;:-") + "..."


def spoken_line(data: dict, *, content: bool = False) -> str:
    """What earshot says: who wrote and how, '<name> texted', '<name> emailed', '<name> called' or '<name> called
    and left a voicemail'. Anyone can list the earshot bucket and fetch the audio for 7 days, so the message itself
    ('<name> texted: <text>', '<name> emailed: <subject>', the voicemail's words) is spoken only with `content`
    (the target's earshot_content). No URL is read aloud (each becomes the word 'link'); at most 200 characters."""
    name = data["target_display_name"]
    body = data.get("content") or {}
    provider = data.get("provider")
    if provider == "email":
        line, what = f"{name} emailed", body.get("subject") or body.get("text")
    elif provider == "call":
        line, what = f"{name} called", None
    elif provider == "voicemail":
        line, what = f"{name} called and left a voicemail", body.get("text")
    else:
        line, what = f"{name} texted", body.get("text")
    if content and what:
        line += f": {what}"
    return trim_words(" ".join(URL_IN_TEXT.sub("link", line).split()))


def ring_timing(channel: str) -> tuple[int, int]:
    """(timeout_secs, time_limit_secs) for this ring: a SIP device may ring longer than a cell."""
    if channel.startswith(SIP_RING_PREFIX):
        return RING_SIP_TIMEOUT_SECS, RING_SIP_TIME_LIMIT_SECS
    return RING_TIMEOUT_SECS, RING_TIME_LIMIT_SECS


def ring_outcome_delay(channel: str) -> float:
    return RING_SIP_OUTCOME_DELAY_S if channel.startswith(SIP_RING_PREFIX) else RING_OUTCOME_DELAY_S


def ring_display_name(name: str) -> str | None:
    """The caller name Telnyx shows for the ring: the target's display name cut down to what Telnyx
    accepts (ASCII letters, digits, spaces and - _ ~ ! . +, at most 128). None when nothing is left."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    cleaned = " ".join(DISPLAY_NAME_UNSAFE.sub("", " ".join(ascii_name.split())).split())
    return cleaned[:DISPLAY_NAME_MAX].strip() or None


def mask_channel(channel: str) -> str:
    """Log form of a channel: a ring target's number or SIP user is personal data. A number keeps its last
    4 digits; a SIP URI keeps the host and the last 2 characters of a user name of 8 or more characters."""
    if not is_ring(channel):
        return channel
    if channel.startswith(SIP_RING_PREFIX):
        user, _, host = channel[len(SIP_RING_PREFIX):].partition("@")
        return f"{SIP_RING_PREFIX}...{user[-2:] if len(user) >= 8 else ''}@{host}"
    return f"{RING_PREFIX}...{channel[-4:]}"


def telnyx_http(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, bytes]:
    """POST; (status, body) for any HTTP answer. Anything else (timeouts, connection errors, a body that
    breaks off mid-read) raises; the caller decides whether the call may already have been placed."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        except Exception:  # noqa: BLE001 - the status is the answer; an unreadable error body is not worth a redelivery
            return exc.code, b""


def ntfy_http_post(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, bytes]:
    """POST to ntfy: the same plain POST as the Telnyx dial (status and body for any HTTP answer)."""
    return telnyx_http(url, headers, body, timeout)


def telnyx_get(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    """GET; (status, body) for any HTTP answer, anything else raises (as telnyx_http)."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        except Exception:  # noqa: BLE001
            return exc.code, b""


def classify_ring(events: list) -> tuple[str, dict]:
    """Terminal outcome of one ring leg from its Telnyx call events.
    ('answered' | 'unanswered' | 'busy' | 'rejected' | 'pending', {hangup_cause, sip_hangup_cause, hangup_source})."""
    names = {e.get("name") for e in events if isinstance(e, dict)}
    hangup = next((e for e in events if isinstance(e, dict) and e.get("name") == "call.hangup"), None)
    detail: dict = {}
    if hangup:
        inner = ((hangup.get("payload") or {}).get("payload") or {}) if isinstance(hangup.get("payload"), dict) else {}
        detail = {k: inner.get(k) for k in ("hangup_cause", "sip_hangup_cause", "hangup_source")}
    if "call.answered" in names:
        return "answered", detail
    if not hangup:
        return "pending", detail
    cause, sip = detail.get("hangup_cause"), str(detail.get("sip_hangup_cause") or "")
    if cause == "timeout":
        return "unanswered", detail
    if cause == "user_busy" or sip in BUSY_SIP_CODES:
        return "busy", detail
    return "rejected", detail


def telnyx_error_title(body: bytes) -> str | None:
    try:
        errors = json.loads(body or b"").get("errors")
        return str(errors[0].get("title"))[:120] if errors else None
    except (ValueError, AttributeError, IndexError, TypeError):
        return None


def telnyx_error_code(body: bytes) -> str | None:
    try:
        errors = json.loads(body or b"").get("errors")
        code = errors[0].get("code") if errors else None
        return str(code)[:20] if code is not None else None
    except (ValueError, AttributeError, IndexError, TypeError):
        return None


def is_channel_limit(status: int, body: bytes) -> bool:
    """A 4xx whose Telnyx error says there is no free channel: the profile's concurrent-call limit (the
    operator is on another call). Any other refusal stays a plain failure."""
    if not 400 <= status < 500:
        return False
    try:
        errors = json.loads(body or b"").get("errors") or []
        return any(str(e.get("code")) in CHANNEL_LIMIT_CODES or CHANNEL_LIMIT_TEXT.search(f"{e.get('title')} {e.get('detail')}")
                   for e in errors if isinstance(e, dict))
    except (ValueError, AttributeError, TypeError):
        return False


def ntfy_header(value: str) -> str:
    """A header value ntfy can read: ASCII as is, anything else as an RFC 2047 word (which ntfy decodes);
    http.client would refuse a raw non-latin-1 header."""
    flat = " ".join(value.split())
    try:
        flat.encode("ascii")
        return flat
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(flat.encode("utf-8")).decode("ascii") + "?="


def env_file_value(path: str, name: str) -> str | None:
    """`NAME=value` from a shell env file (optional `export`, optional quotes); None when absent or unreadable."""
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            for line in handle:
                found = re.match(rf"^\s*(?:export\s+)?{re.escape(name)}=(.*)$", line.rstrip("\n"))
                if found:
                    return found.group(1).strip().strip("'\"") or None
    except OSError:
        return None
    return None


def ntfy_base_url() -> str:
    return (os.environ.get("TARGET_WATCH_NTFY_URL") or NTFY_DEFAULT_URL).rstrip("/")


def confirmed(stdout: str) -> bool:
    """hermes send --json printed a success verdict (no error, not skipped)."""
    try:
        result = json.loads(stdout or "")
    except ValueError:
        return False
    return isinstance(result, dict) and result.get("success") is True and not result.get("error") and not result.get("skipped")


def notify_error(stdout: str) -> str | None:
    try:
        result = json.loads(stdout or "")
    except ValueError:
        return None
    return str(result.get("error"))[:300] if isinstance(result, dict) and result.get("error") else None


def first_match(pattern: re.Pattern, stdout: str) -> str | None:
    for line in stdout.splitlines():
        found = pattern.match(line.strip())
        if found:
            return found.group(1)
    return None


def url_after(pattern: re.Pattern, stdout: str) -> str | None:
    """The https URL px prints on its own line after the line that confirmed the ticket, else None. Only a
    line that is nothing but a URL counts, so a Click link is either px's own or absent."""
    confirmed_line = False
    for line in stdout.splitlines():
        text = line.strip()
        if not confirmed_line:
            confirmed_line = bool(pattern.match(text))
        elif URL_LINE.match(text):
            return text
    return None


def snippet(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


@dataclass
class Plan:
    detection_id: str
    watch_id: str
    action: str
    conv_key: str
    ticket_ref: str | None = None
    px_argv: list[str] | None = None
    px_cwd: str | None = None
    notify: list[str] = field(default_factory=list)
    reason: str | None = None
    ticket_url: str | None = None  # px's own link to the ticket, once px has confirmed it


def conversation_key(data: dict) -> str:
    conversation = data.get("conversation") or {}
    if conversation.get("thread_id"):
        key = f"thread:{conversation['thread_id']}"
    elif conversation.get("group_label"):
        key = f"group:{conversation['group_label'].casefold()}"
    else:
        key = f"sender:{(data.get('sender') or {}).get('handle', '').casefold()}"
    return f"{data['provider']}:{key}"


def headline(data: dict, limit: int = 60) -> str:
    """Subject, else the start of the text, else what kind of event it was."""
    content = data["content"]
    if content.get("subject"):
        return snippet(content["subject"], limit)
    if content.get("text"):
        return snippet(content["text"], limit)
    return f"no text ({data['source_type'].split('.', 2)[2]})"


def ticket_title(data: dict) -> str:
    return f"[watch] {data['target_display_name']} via {data['provider']}: {headline(data)}"


def ticket_body(data: dict, detection_id: str) -> str:
    sender = data["sender"]
    who = f"{sender['display_name']} <{sender['handle']}>" if sender.get("display_name") else sender["handle"]
    conversation = data.get("conversation") or {}
    lines = [
        f"From: {who}",
        f"Provider: {data['provider']} ({data['source_type']})",
        f"Received: {data['received_at']}",
    ]
    if conversation.get("group_label"):
        lines.append(f"Group: {conversation['group_label']}")
    if data["content"].get("subject"):
        lines.append(f"Subject: {data['content']['subject']}")
    lines += [
        "",
        data["content"].get("text") or "(no text: the event carried no message body)",
        "",
        f"Source event: {data['source_event_id']}",
        f"Detection: {detection_id}",
        "",
        FOOTER.format(provider=data["provider"]),
    ]
    return "\n".join(lines)


def comment_body(data: dict, detection_id: str) -> str:
    sender = data["sender"]
    who = sender.get("display_name") or sender["handle"]
    return "\n".join([
        f"[watch] Another {data['provider']} message from {who} at {data['received_at']}:",
        "",
        data["content"].get("text") or "(no text: the event carried no message body)",
        "",
        f"Source event: {data['source_event_id']} (detection {detection_id}). Added by target-watch.",
    ])


def ntfy_title(data: dict) -> str:
    return f"{data['target_display_name']} - {data['provider']}"


def notify_line(data: dict, action: str, ticket_ref: str | None, *, button: bool = True) -> str:
    if action == AWAITING_BUTTON:
        tail = "no ticket yet, tap Create Ticket" if button else "no ticket filed (the Create Ticket button is unavailable)"
    else:
        tail = f"{action} {ticket_ref}" if ticket_ref else action
    return f"{data['target_display_name']} via {data['provider']}: {headline(data, 120)} -> {tail}"


class SinkService:
    def __init__(self, registry: TargetRegistry, state: State, publisher, *, runner=run_command,
                 px: str = "px", hermes: str = "hermes", dry_run: bool = False, validate=env.validate, clock=None, out=None,
                 http=telnyx_http, op: str | None = None, http_get=None, ntfy_http=None):
        self.registry = registry
        self.state = state
        self.publisher = publisher
        self.runner = runner
        self.px = px
        self.hermes = hermes
        self.dry_run = dry_run
        self.validate = validate
        self.clock = clock or (lambda: datetime.now(UTC))
        self.out = out
        self.http = http
        self.http_get = http_get or telnyx_get
        self.ntfy_http = ntfy_http or ntfy_http_post  # its own seam: a Telnyx fake must never see an ntfy POST
        self._bg: set[asyncio.Task] = set()  # outcome followers; held so they are not garbage collected
        self._followups: set[asyncio.Task] = set()  # button follow-up pushes: short, and waited for at shutdown
        self.op = op or os.environ.get("TARGET_WATCH_OP") or os.path.expanduser("~/.local/bin/op")
        self._ring_key: str | None = None  # resolved at first use, held in memory only
        self._ntfy_key: str | None = None  # likewise
        self._button_key: bytes | None = None  # the button HMAC secret, likewise
        self._button_key_retry_at = 0.0  # time.monotonic() before which an unreadable secret is not asked for again
        # One px at a time, across the consumer and the button taps: the coalescing read and the ticket write
        # must not interleave, or two concurrent messages of one conversation would each create a ticket.
        self._px_lock = asyncio.Lock()

    # --- planning (pure: reads state, writes nothing) -------------------------

    def plan(self, detection: dict) -> Plan:
        data = detection["data"]
        detection_id = detection["id"]
        conv_key = conversation_key(data)
        target = self.registry.get(data["watch_id"])
        if target is None:
            return Plan(detection_id, data["watch_id"], "failed", conv_key,
                        reason="no target file defines this watch_id")
        notify = sorted(target.sink.notify, key=channel_rank)  # ntfy first, then telegram, then the ring
        digest = text_hash(data["content"].get("text") or "")
        if digest:
            twin = self.state.recent_text_route(data["watch_id"], digest, epoch(data["received_at"]), detection_id, TEXT_DUPLICATE_S,
                                                 conv_key, CARRIER_TWINS)
            if twin:
                return Plan(detection_id, target.id, "skipped_duplicate", conv_key,
                            reason=f"same text as detection {twin} within {int(TEXT_DUPLICATE_S)}s")
        if target.sink.kind == "notify_only":
            return Plan(detection_id, target.id, "notified_only", conv_key, notify=notify)
        if target.sink.ticket == "button":
            # Nothing is filed until the operator taps Create Ticket on the push; the route says so.
            return Plan(detection_id, target.id, AWAITING_BUTTON, conv_key, notify=notify)
        return self.ticket_plan(detection, target, notify)

    def ticket_plan(self, detection: dict, target: Target, notify=()) -> Plan:
        """What px must do for this detection: a comment on the conversation's open ticket (within
        coalesce_hours), else a new ticket. The one place that says so, for auto mode and for a button tap."""
        data = detection["data"]
        detection_id = detection["id"]
        conv_key = conversation_key(data)
        notify = list(notify)
        repo = str(target.sink.repo)
        since = self.clock().timestamp() - target.sink.coalesce_hours * 3600
        existing = self.state.coalesce_ticket(target.id, conv_key, since) if target.sink.coalesce_hours > 0 else None
        if existing:
            argv = [self.px, "comment", existing, "-m", comment_body(data, detection_id)]
            return Plan(detection_id, target.id, "ticket_commented", conv_key, ticket_ref=existing,
                        px_argv=argv, px_cwd=repo, notify=notify)
        argv = [self.px, "task", "create", ticket_title(data), "-d", ticket_body(data, detection_id)]
        if target.sink.state:
            argv += ["--state", target.sink.state]
        if target.sink.labels:
            argv += ["-l", ",".join(target.sink.labels)]
        if target.sink.priority:
            argv += ["--priority", target.sink.priority]
        return Plan(detection_id, target.id, "ticket_created", conv_key, px_argv=argv, px_cwd=repo, notify=notify)

    # --- execution --------------------------------------------------------------

    async def _run_px(self, plan: Plan) -> tuple[str, str | None, str | None]:
        """(action, ticket_ref, reason). Never retried: an unclear outcome is a failure. px runs in a worker
        thread, so a slow board never stops the event loop (which also answers the button's HTTP taps)."""
        code, stdout, stderr = await asyncio.to_thread(self.runner, plan.px_argv, cwd=plan.px_cwd, timeout=PX_TIMEOUT_S)
        if plan.action == "ticket_created":
            ref = first_match(CREATED, stdout)
            if ref:
                plan.ticket_url = url_after(CREATED, stdout)
                self.state.record_ticket(plan.watch_id, plan.conv_key, ref, self.clock().timestamp())
                return "ticket_created", ref, None if code == 0 else f"px exited {code} after creating {ref}"
            return "failed", None, f"px task create did not confirm a ticket (exit {code}); not retried, check the board"
        ref = first_match(COMMENTED, stdout)
        if ref:
            plan.ticket_url = url_after(COMMENTED, stdout)
            return "ticket_commented", ref, None
        return "failed", plan.ticket_ref, f"px comment did not confirm (exit {code}); not retried"

    async def _notify(self, channels: list[str], line: str, target: Target | None = None,
                      received_at: float | None = None, *, detection_id: str | None = None,
                      title: str | None = None, click: str | None = None, actions: str | None = None,
                      spoken: str | None = None) -> tuple[list[str], list[str]]:
        """(sent, failed). ntfy first (the loud one), then earshot, then Telegram, then the ring: the line the
        operator reads after the phone rings must already be there. A throttled, quiet or stale ring or
        earshot line is neither sent nor failed. `actions` is ntfy's Actions header (the Create Ticket
        button), sent on ntfy channels only. Every attempt is written to the notification log."""
        # Name the hermes profile, as Candybar's pager does: without -p, hermes
        # resolves a profile from its surroundings, and from a service process on
        # 2026-11-04 it found one with no Telegram token and exited 1. Success is
        # the JSON verdict, not the exit code alone.
        sent, failed = [], []
        watch_id = target.id if target else "?"
        for channel in sorted(channels, key=channel_rank):
            if is_ring(channel):
                outcome = await self._ring(target, channel, received_at, detection_id)
                if outcome == "sent":
                    sent.append(channel)
                elif outcome in ("failed", "channel_limit"):
                    failed.append(channel)
                continue
            if is_earshot(channel):
                # Never the notify line as a fallback: it carries the message, and the earshot bucket is public.
                said = spoken or (f"{target.display_name} wrote" if target else "a watched person wrote")
                outcome = await self._earshot(target, channel, said, received_at, detection_id)
                if outcome == "sent":
                    sent.append(channel)
                elif outcome == "failed":
                    failed.append(channel)
                continue
            if is_ntfy(channel):
                ok, detail = await self._ntfy(channel, title or "target-watch", line, click, actions=actions)
                if ok and actions:
                    detail += "; with the Create Ticket button"
            else:
                ok, detail = self._send(channel, line)
            self._note(detection_id, watch_id, mask_channel(channel), "sent" if ok else "failed", detail)
            (sent if ok else failed).append(channel)
        return sent, failed

    def _send(self, channel: str, line: str) -> tuple[bool, str]:
        """One `hermes send`. Blocking; never raises on a bad verdict. (confirmed, detail for the log)."""
        argv = [self.hermes, "-p", HERMES_PROFILE, "send", "--to", channel, "--json", "--", line]
        code, stdout, stderr = self.runner(argv, cwd=NOTIFY_CWD, timeout=NOTIFY_TIMEOUT_S)
        if code == 0 and confirmed(stdout):
            return True, ""
        error = notify_error(stdout) or (stderr or "").strip()[-300:]
        log("notify_failed", channel=channel, exit=code, profile=HERMES_PROFILE, error=error)
        return False, f"hermes exit {code}: {error}"[:300] if error else f"hermes exit {code}"

    # --- notification log -----------------------------------------------------------

    def _note(self, detection_id: str | None, watch_id: str, masked: str, outcome: str, detail: str | None = None,
              *, attempt: int = 1, ref: str | None = None) -> int | None:
        """Write one notification row; the row id, or None when there is no detection to attach it to or the
        write failed. A state fault must never escape into the bus path (nor into a second ring)."""
        if detection_id is None:
            return None
        try:
            return self.state.record_notification(detection_id, watch_id, masked, outcome, detail or None, attempt=attempt, ref=ref)
        except Exception as exc:  # noqa: BLE001
            log("notification_state_write_failed", watch_id=watch_id, channel=masked, error=type(exc).__name__)
            return None

    def _note_update(self, row_id: int | None, outcome: str, detail: str | None = None) -> None:
        if row_id is None:
            return
        try:
            self.state.update_notification(row_id, outcome, detail or None)
        except Exception as exc:  # noqa: BLE001
            log("notification_state_write_failed", row=row_id, error=type(exc).__name__)

    # --- ntfy ---------------------------------------------------------------------------

    def _ntfy_token(self) -> str | None:
        if self._ntfy_key is None:
            ref = (os.environ.get("TARGET_WATCH_NTFY_TOKEN_REF")
                   or env_file_value(os.environ.get("TARGET_WATCH_NTFY_ENV_FILE") or NTFY_DEFAULT_ENV_FILE, "NTFY_TOKEN_REF"))
            if not ref or not ref.startswith("op://"):
                log("ntfy_failed", reason="no op:// token reference (TARGET_WATCH_NTFY_TOKEN_REF, or NTFY_TOKEN_REF in the infra-alerts env file)")
                return None
            # The argv carries the op:// reference only; the token comes back on stdout and goes nowhere but memory.
            code, stdout, stderr = self.runner([self.op, "read", "--no-newline", ref], cwd=NOTIFY_CWD, timeout=OP_TIMEOUT_S)
            token = (stdout or "").strip()
            if code != 0 or not token:
                log("ntfy_failed", reason="token unavailable", exit=code, error=(stderr or "").strip()[-200:])
                return None
            self._ntfy_key = token
        return self._ntfy_key

    def _ntfy_post(self, topic: str, title: str, text: str, click: str | None, *, priority: str = "urgent",
                   tags: str = "rotating_light", actions: str | None = None) -> tuple[bool, str]:
        """Blocking, so it runs in a worker thread and touches no sqlite. Never raises. (published, detail).
        Success is HTTP 200 with a JSON body that carries the message "id"."""
        try:
            token = self._ntfy_token()
        except Exception as exc:  # noqa: BLE001
            log("ntfy_failed", reason="token unavailable", error=type(exc).__name__)
            token = None
        if token is None:
            return False, "no ntfy token"
        headers = {"Authorization": f"Bearer {token}", "Title": ntfy_header(title), "Priority": priority,
                   "Tags": tags, "Content-Type": "text/plain; charset=utf-8", "Accept": "application/json",
                   "User-Agent": RING_USER_AGENT}
        if click and URL_LINE.match(click):
            headers["Click"] = click
        if actions:
            headers["Actions"] = actions  # ASCII by construction (button.ntfy_action); never log it: it carries the signature
        url = f"{ntfy_base_url()}/{urllib.parse.quote(topic, safe='')}"
        try:
            status, answer = self.ntfy_http(url, headers, text.encode("utf-8"), NTFY_HTTP_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - whatever breaks, it must not escape and get the detection redelivered
            log("ntfy_failed", reason="transport", error=type(exc).__name__)
            return False, f"transport {type(exc).__name__}"
        message_id = None
        try:
            parsed = json.loads(answer or b"")
            message_id = parsed.get("id") if isinstance(parsed, dict) else None
        except ValueError:
            parsed = None
        if status == 200 and message_id:
            return True, f"id {str(message_id)[:40]}"
        if status in (401, 403):
            self._ntfy_key = None  # a rotated token is re-read next time
        error = str(parsed.get("error"))[:100] if isinstance(parsed, dict) and parsed.get("error") else None
        log("ntfy_failed", status=status, error=error, reason=None if status != 200 else "no message id in the answer")
        return False, f"HTTP {status}" + (f": {error}" if error else "" if status != 200 else ": no message id in the answer")

    async def _ntfy(self, channel: str, title: str, text: str, click: str | None, *, priority: str = "urgent",
                    tags: str = "rotating_light", actions: str | None = None) -> tuple[bool, str]:
        """Publish to the channel's topic. (ok, detail); never raises."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._ntfy_post, channel[len(NTFY_PREFIX):], title, text, click,
                                  priority=priority, tags=tags, actions=actions), NTFY_DEADLINE_S)
        except TimeoutError:
            log("ntfy_failed", reason="deadline", seconds=NTFY_DEADLINE_S)
            return False, f"no answer in {NTFY_DEADLINE_S}s"
        except Exception as exc:  # noqa: BLE001
            log("ntfy_failed", reason="unexpected", error=type(exc).__name__)
            return False, f"unexpected {type(exc).__name__}"

    # --- earshot ------------------------------------------------------------------------

    @staticmethod
    def _earshot_bin() -> str:
        return os.path.expanduser(os.environ.get("TARGET_WATCH_EARSHOT") or EARSHOT_DEFAULT_BIN)

    def _earshot_argv(self, name: str, spoken: str) -> list[str]:
        return [self._earshot_bin(), "say", "-c", name, "--slug", EARSHOT_SLUG, "--", spoken]

    async def _earshot(self, target: Target | None, channel: str, spoken: str, received_at: float | None = None,
                       detection_id: str | None = None) -> str:
        """Speak the line through the earshot CLI. 'sent' | 'failed' | 'throttled' | 'quiet' | 'stale'.

        An audible alarm, so it follows the ring's rules exactly: quiet hours, nothing stale, and one per
        cooldown per (watch, channel). Success is exit 0 AND the object key the CLI prints (a line that starts
        with '<channel>/'). When the outcome is uncertain (exit 0 without a key, or a timeout: the line may
        already be uploaded), the cooldown is taken but the channel is reported failed; a plain failure leaves
        the cooldown alone, because nothing was dropped. An earshot too old for --slug exits 2 before uploading,
        which is a plain failure, and the line is never retried without the slug. Never raises."""
        watch_id = target.id if target else "?"
        name = channel[len(EARSHOT_PREFIX):]
        skip = self.ring_skip(target, channel, received_at)
        if skip:
            log(f"earshot_{skip}", watch_id=watch_id, channel=channel)
            self._note(detection_id, watch_id, channel, skip)
            return skip
        try:
            code, stdout, stderr = await asyncio.to_thread(
                self.runner, self._earshot_argv(name, spoken), cwd=NOTIFY_CWD, timeout=EARSHOT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - nothing in a notify path raises into the bus
            log("earshot_failed", watch_id=watch_id, channel=channel, error=type(exc).__name__)
            self._note(detection_id, watch_id, channel, "failed", f"unexpected {type(exc).__name__}")
            return "failed"
        key = first_match(re.compile(rf"^({re.escape(name)}/\S+)$"), stdout or "")
        if code == 0 and key:
            if target is not None:
                self._record_ring(watch_id, channel, channel)
            log("earshot_sent", watch_id=watch_id, channel=channel, key=key)
            self._note(detection_id, watch_id, channel, "sent", f"spoke {len(spoken)} characters", ref=key)
            return "sent"
        if code == 0:
            detail = "earshot exited 0 but printed no object key"
        elif code == 124:
            detail = f"earshot did not finish in {EARSHOT_TIMEOUT_S}s (the line may have been dropped)"
        else:
            tail = (stderr or "").strip()[-200:]
            detail = f"earshot exit {code}" + (f": {tail}" if tail else "")
        if code in (0, 124) and target is not None:
            self._record_ring(watch_id, channel, channel)  # uncertain: it may have played, so it holds the cooldown
        log("earshot_failed", watch_id=watch_id, channel=channel, exit=code, error=(stderr or "").strip()[-200:])
        self._note(detection_id, watch_id, channel, "failed", detail)
        return "failed"

    # --- the Create Ticket button -----------------------------------------------------------
    # The ntfy push of a `ticket = "button"` target carries one http action; tapping it POSTs a signed URL to
    # button.ButtonListener, which hands the request to button_request. The detection is stored whole in the
    # `buttons` table, so a tap files exactly what auto mode would have filed for it.

    def _button_secret(self) -> bytes | None:
        """The HMAC key: the 1Password item TARGET_WATCH_BUTTON_SECRET_REF names, read through op and held in
        memory only. Blocking (it may run op), so it is called from a worker thread. An unreadable secret is
        not asked for again for BUTTON_SECRET_RETRY_S, so an unsigned caller cannot drive op."""
        if self._button_key is not None:
            return self._button_key
        if time.monotonic() < self._button_key_retry_at:
            return None
        ref = os.environ.get("TARGET_WATCH_BUTTON_SECRET_REF") or ""
        if not ref.startswith("op://"):
            log("button_failed", reason="no op:// secret reference (TARGET_WATCH_BUTTON_SECRET_REF)")
            self._button_key_retry_at = time.monotonic() + BUTTON_SECRET_RETRY_S
            return None
        # The argv carries the op:// reference only; the secret comes back on stdout and goes nowhere but memory.
        code, stdout, stderr = self.runner([self.op, "read", "--no-newline", ref], cwd=NOTIFY_CWD, timeout=OP_TIMEOUT_S)
        secret = (stdout or "").strip()
        if code != 0 or len(secret) < BUTTON_SECRET_MIN:
            log("button_failed", reason="secret unavailable" if code != 0 else f"secret shorter than {BUTTON_SECRET_MIN} characters",
                exit=code, error=(stderr or "").strip()[-200:])
            self._button_key_retry_at = time.monotonic() + BUTTON_SECRET_RETRY_S
            return None
        self._button_key = secret.encode("utf-8")
        return self._button_key

    async def _secret(self) -> bytes | None:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._button_secret), NTFY_DEADLINE_S)
        except Exception as exc:  # noqa: BLE001 - includes the deadline; no secret means no button, never an exception
            log("button_failed", reason="secret lookup", error=type(exc).__name__)
            return None

    async def _offer_button(self, detection: dict, target: Target) -> str | None:
        """Store the detection for a later tap and return the ntfy Actions header carrying its signed link
        (7 days). None when there is no secret: the push then goes out without a button, and says so."""
        secret = await self._secret()
        if secret is None:
            return None
        channels = [c for c in target.sink.notify if is_ntfy(c)]
        self.state.open_button(detection["id"], target.id, detection, channels)
        exp = int(self.clock().timestamp()) + btn.BUTTON_TTL_S
        return btn.ntfy_action(btn.signed_url(button_base_url(), secret, detection["id"], exp))

    async def button_request(self, method: str, path: str, query: str) -> tuple[int, dict]:
        """One request to the button listener: (HTTP status, JSON body). 404 for a path that is not
        /watch/ticket/<id>, 405 for anything but POST, 403 for a malformed, forged or expired link (the
        signature is checked before the detection is looked up, so nothing is learnt without it), 404 for a
        genuine link whose detection is not stored, else the filing."""
        found = BUTTON_PATH.match(path)
        if not found:
            return 404, {"error": "not found"}
        if method != "POST":
            return 405, {"error": "use POST"}
        detection_id = urllib.parse.unquote(found.group(1))
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        exp, sig = params.get("exp", []), params.get("sig", [])
        if len(exp) != 1 or len(sig) != 1 or not btn.EXP.fullmatch(exp[0]) or not btn.SIG.fullmatch(sig[0]):
            return 403, {"error": BUTTON_BAD_SIGNATURE}
        secret = await self._secret()
        if secret is None:
            return 503, {"error": "the Create Ticket button is not configured on this host"}
        why = btn.check(secret, detection_id, exp[0], sig[0], self.clock().timestamp())
        if why == "expired":
            return 403, {"error": "link expired"}
        if why:
            return 403, {"error": BUTTON_BAD_SIGNATURE}
        return await self._file_from_button(detection_id)

    async def _file_from_button(self, detection_id: str) -> tuple[int, dict]:
        """A genuine tap. Files what auto mode would have (comment on the open ticket, else create), once: a second
        tap returns the same ticket and files nothing. Same no-blind-retry rule as a route: 'pending' is written
        before px runs, so an interrupted run ends 'failed' and a failed one is never re-run."""
        async with self._px_lock:
            row = self.state.button(detection_id)
            if row is None:
                return 404, {"error": "unknown detection"}
            watch_id = row["watch_id"]
            if row["state"] == "done":
                self._note(detection_id, watch_id, "button", "already_filed", None, ref=row["ticket_ref"])
                return 200, {"ticket_ref": row["ticket_ref"]}
            if row["state"] == "pending":
                reason = "interrupted while px was running; outcome unknown, not retried, check the board"
                self.state.update_button(detection_id, state="failed", action="failed", reason=reason)
                self._note(detection_id, watch_id, "button", "failed", reason)
                self._button_followup(row, None, None, reason)
                return 409, {"error": "ticket not filed", "reason": reason}
            if row["state"] == "failed":
                return 409, {"error": "an earlier tap did not file a ticket and is not retried; check the board",
                             "reason": row["reason"]}
            detection = row["detection"]
            target = self.registry.get(watch_id)
            if target is None or target.sink.kind != "plane":
                return 409, {"error": "this watch no longer files tickets"}
            plan = self.ticket_plan(detection, target)
            self.state.update_button(detection_id, state="pending", px_kind=plan.action)
            action, ref, reason = await self._run_px(plan)
            if action == "failed":
                self.state.update_button(detection_id, state="failed", action=action, reason=reason)
                self._note(detection_id, watch_id, "button", "failed", reason)
                self._button_followup(row, None, None, reason or "px did not confirm a ticket")
                return 502, {"error": "ticket not filed", "reason": reason}
            self.state.update_button(detection_id, state="done", action=action, ticket_ref=ref,
                                     ticket_url=plan.ticket_url, reason=reason)
            self._note(detection_id, watch_id, "button", action, reason, ref=ref)
            self._button_followup(row, ref, plan, None)
            return 200, {"ticket_ref": ref}

    def _button_followup(self, row: dict, ref: str | None, plan: Plan | None, failure: str | None) -> None:
        """ONE ntfy line to the push's own topic after a tap, in the background (the tap's HTTP answer never
        waits for it): '<ref> filed' at low priority with Click = the ticket's URL when px printed one, or the
        reason the ticket was not filed."""
        data = row["detection"]["data"]
        target = self.registry.get(row["watch_id"])
        channels = row["channels"] or [c for c in (target.sink.notify if target else ()) if is_ntfy(c)]
        if not channels:
            log("button_followup_unsent", watch_id=row["watch_id"], reason="no ntfy channel")
            return
        if failure is None:
            text = f"{ref} filed" + (" (as a comment on the open ticket)" if plan and plan.action == "ticket_commented" else "")
            kwargs = {"priority": "low", "tags": "ticket", "click": plan.ticket_url if plan else None}
        else:
            text = f"ticket not filed: {failure}"
            kwargs = {"priority": "default", "tags": "warning", "click": None}
        title = f"{data['target_display_name']} - ticket"

        async def send() -> None:
            for channel in channels:
                ok, detail = await self._ntfy(channel, title, text, kwargs["click"], priority=kwargs["priority"], tags=kwargs["tags"])
                self._note(row["detection_id"], row["watch_id"], f"{channel} (button follow-up)", "sent" if ok else "failed", detail)

        self._background(send, "button_followup")

    def _background(self, make, what: str) -> None:
        """Run `make()` (a coroutine factory) as a held background task. Never raises."""
        try:
            task = asyncio.get_running_loop().create_task(make())
        except Exception as exc:  # noqa: BLE001
            log("background_not_scheduled", what=what, error=type(exc).__name__)
            return
        self._followups.add(task)
        task.add_done_callback(self._followups.discard)

    async def drain_followups(self, timeout: float = 30) -> None:
        """Let the button follow-up pushes in flight finish (shutdown): each is one ntfy POST."""
        if self._followups:
            await asyncio.wait(list(self._followups), timeout=timeout)

    # --- ring ---------------------------------------------------------------------

    @staticmethod
    def ring_config() -> tuple[str | None, str | None]:
        return os.environ.get("TARGET_WATCH_TELNYX_CONNECTION_ID") or None, os.environ.get("TARGET_WATCH_RING_FROM") or None

    def ring_skip(self, target: Target | None, channel: str, received_at: float | None = None) -> str | None:
        """'quiet', 'stale' or 'throttled' when this ring must not be placed now, else None. Reads state, writes nothing."""
        if target is None:
            return None
        now = self.clock()
        local = now.astimezone(RING_TZ)
        if in_quiet_hours(target.sink.ring_quiet_hours, local.hour * 60 + local.minute):
            return "quiet"
        if received_at is not None and now.timestamp() - received_at > RING_MAX_AGE_S:
            return "stale"
        last = self.state.last_ring(target.id, channel)
        if last is not None and now.timestamp() - last < target.sink.ring_cooldown_minutes * 60:
            return "throttled"
        return None

    def _key(self) -> str | None:
        if self._ring_key is None:
            ref = os.environ.get("TARGET_WATCH_TELNYX_KEY_REF") or DEFAULT_KEY_REF
            # The argv carries the op:// reference only; the key comes back on stdout and goes nowhere but memory.
            code, stdout, stderr = self.runner([self.op, "read", "--no-newline", ref], cwd=NOTIFY_CWD, timeout=OP_TIMEOUT_S)
            key = (stdout or "").strip()
            if code != 0 or not key:
                log("ring_failed", reason="key unavailable", exit=code, error=(stderr or "").strip()[-200:])
                return None
            self._ring_key = key
        return self._ring_key

    def _place(self, to: str, connection_id: str, from_number: str, display_name: str | None,
               timeout_secs: int, time_limit_secs: int) -> tuple[str, int | None, bytes, Exception | None]:
        """Blocking, so it runs in a worker thread and touches no sqlite. Never raises.
        ('nokey' | 'answered' | 'error', http status, answer body, the exception for 'error')."""
        try:
            key = self._key()
        except Exception as exc:  # noqa: BLE001
            log("ring_failed", reason="key unavailable", error=type(exc).__name__)
            key = None
        if key is None:
            return "nokey", None, b"", None
        payload = {
            "connection_id": connection_id, "to": to, "from": from_number,
            "timeout_secs": timeout_secs, "time_limit_secs": time_limit_secs,
        }
        if display_name:
            payload["from_display_name"] = display_name  # the caller name the phone shows, instead of the bare number
        body = json.dumps(payload).encode()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": RING_USER_AGENT}
        try:
            status, answer = self.http(TELNYX_CALLS_URL, headers, body, RING_HTTP_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - whatever breaks, it must not escape and get the detection redelivered
            return "error", None, b"", exc
        return "answered", status, answer, None

    def _record_ring(self, watch_id: str, channel: str, masked: str) -> None:
        try:
            self.state.record_ring(watch_id, channel, self.clock().timestamp())
        except Exception as exc:  # noqa: BLE001 - a state fault after the POST must not turn into a second ring
            log("ring_state_write_failed", watch_id=watch_id, channel=masked, error=type(exc).__name__)

    async def _ring(self, target: Target | None, channel: str, received_at: float | None = None,
                    detection_id: str | None = None) -> str:
        """Place the call. 'sent' | 'failed' | 'channel_limit' | 'throttled' | 'quiet' | 'stale'.

        Nothing here raises: a ring that fails is a failed channel, and a redelivered detection must never
        place a second call. When the outcome is uncertain (the request may have been accepted before the
        answer broke), the ring counts as placed for the cooldown but is reported as failed. A dial Telnyx
        refuses for lack of a free channel (the operator is on another call) is retried once, like a busy
        phone."""
        watch_id = target.id if target else "?"
        masked = mask_channel(channel)
        skip = self.ring_skip(target, channel, received_at)
        if skip:
            log(f"ring_{skip}", watch_id=watch_id, channel=masked)
            self._note(detection_id, watch_id, masked, skip)
            return skip
        display_name = ring_display_name(target.display_name) if target else None
        outcome, leg_id, detail = await self._dial(watch_id, channel, masked, display_name, record=True)
        if outcome == "sent":
            row = self._note(detection_id, watch_id, masked, "placed", detail, ref=leg_id)
            if target is not None:
                self._follow(target, channel, leg_id, attempt=1, detection_id=detection_id, row_id=row)
        elif outcome == "channel_limit":
            self._note(detection_id, watch_id, masked, "channel_limit", f"{detail}; one retry in {RING_BUSY_RETRY_S}s")
            if target is not None:
                self._schedule(lambda: self._retry_after_refusal(target, channel, detection_id), target, channel)
        else:
            self._note(detection_id, watch_id, masked, "failed", detail)
        return outcome

    async def _dial(self, watch_id: str, channel: str, masked: str, display_name: str | None, *,
                    record: bool) -> tuple[str, str | None, str]:
        """POST the call. ('sent' | 'failed' | 'channel_limit', call_leg_id, detail for the log). `record`
        writes the cooldown row; the retries pass False because the first ring of the burst already holds
        the cooldown (a channel_limit refusal writes it too: its pending retry stands for the burst's ring)."""
        connection_id, from_number = self.ring_config()
        if not connection_id or not from_number:
            log("ring_failed", watch_id=watch_id, channel=masked,
                reason="TARGET_WATCH_TELNYX_CONNECTION_ID and TARGET_WATCH_RING_FROM must both be set")
            return "failed", None, "ring is not configured (connection id or from number missing)"
        mark = (lambda: self._record_ring(watch_id, channel, masked)) if record else (lambda: None)
        timeout_secs, time_limit_secs = ring_timing(channel)
        try:
            state, status, answer, exc = await asyncio.wait_for(
                asyncio.to_thread(self._place, channel[len(RING_PREFIX):], connection_id, from_number, display_name,
                                  timeout_secs, time_limit_secs), RING_DEADLINE_S)
        except TimeoutError:
            # The worker may still finish, and the call may already be placed.
            mark()
            log("ring_failed", watch_id=watch_id, channel=masked, reason="deadline", seconds=RING_DEADLINE_S)
            return "failed", None, f"no answer from Telnyx in {RING_DEADLINE_S}s (the call may have been placed)"
        if state == "nokey":
            return "failed", None, "no Telnyx key"
        if state == "error":
            # A refused connection (URLError) never reached Telnyx; a timeout or a broken read may have.
            if not isinstance(exc, urllib.error.URLError):
                mark()
            log("ring_failed", watch_id=watch_id, channel=masked, reason="transport", error=type(exc).__name__)
            return "failed", None, f"transport {type(exc).__name__}"
        call_id = leg_id = None
        if 200 <= status < 300:
            try:
                data = json.loads(answer or b"").get("data") or {}
                call_id = data.get("call_control_id")
                leg_id = data.get("call_leg_id") if isinstance(data.get("call_leg_id"), str) else None
            except (ValueError, AttributeError):
                call_id = None
            if not call_id:
                mark()  # accepted, so the phone may be ringing
        if not call_id:
            if status in (401, 403):
                self._ring_key = None  # a rotated key is re-read next time
            title, code = telnyx_error_title(answer), telnyx_error_code(answer)
            if is_channel_limit(status, answer):
                mark()  # the retry stands for this burst's ring; later texts of the burst must not ring as well
                log("ring_failed", watch_id=watch_id, channel=masked, status=status, code=code, title=title, reason="channel_limit")
                return "channel_limit", None, f"HTTP {status} code {code}: {title}"
            log("ring_failed", watch_id=watch_id, channel=masked, status=status, title=title,
                reason=None if status >= 300 else "no call_control_id in the answer")
            return "failed", None, (f"HTTP {status}: {title}" if status >= 300 else "accepted without a call_control_id")
        mark()
        log("ring_placed", watch_id=watch_id, channel=masked, call_control_id=call_id, call_leg_id=leg_id,
            timeout_secs=timeout_secs, from_display_name=display_name)
        return "sent", leg_id, "accepted by Telnyx"

    # --- ring outcome ---------------------------------------------------------------
    # Telnyx accepting the dial (HTTP 200) is not the phone ringing: on 2026-11-05 two rings were accepted,
    # declined with SIP 603 in 0.6 s, and nothing said so for a day. Each accepted ring is followed to its
    # terminal state in a background task: off the detection path and off the bus ack. The task lives in
    # this process only: a restart inside the window (the ring's timeout + 60 s, or +100 s more with a
    # retry) loses the check and the failure line, and the call itself is unaffected.

    def _schedule(self, make, target: Target, channel: str) -> None:
        """Run `make()` (a coroutine factory) as a held background task."""
        try:
            task = asyncio.get_running_loop().create_task(make())
        except Exception as exc:  # noqa: BLE001 - the ring is placed; failing to watch it must not raise into the bus
            log("ring_outcome_unknown", watch_id=target.id, channel=mask_channel(channel), reason="not scheduled",
                error=type(exc).__name__)
            return
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _follow(self, target: Target, channel: str, leg_id: str | None, attempt: int,
                detection_id: str | None = None, row_id: int | None = None) -> None:
        self._schedule(lambda: self._follow_ring(target, channel, leg_id, attempt, detection_id, row_id), target, channel)

    async def settle(self) -> None:
        """Wait for every outcome follower and button follow-up (tests)."""
        while self._bg or self._followups:
            await asyncio.gather(*list(self._bg), *list(self._followups), return_exceptions=True)

    def _get_events(self, leg_id: str) -> tuple[int, bytes]:
        key = self._key()
        if key is None:
            raise RuntimeError("key unavailable")
        url = f"{TELNYX_CALL_EVENTS_URL}?filter%5Bleg_id%5D={urllib.parse.quote(leg_id, safe='')}"
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": RING_USER_AGENT}
        return self.http_get(url, headers, RING_HTTP_TIMEOUT_S)

    async def _lookup_outcome(self, leg_id: str) -> tuple[str, dict]:
        """(kind, detail) where kind is classify_ring's, or 'unknown' when the events never became readable."""
        detail: dict = {}
        for poll in range(RING_OUTCOME_POLLS):
            if poll:
                await asyncio.sleep(RING_OUTCOME_POLL_S)
            try:
                status, body = await asyncio.wait_for(asyncio.to_thread(self._get_events, leg_id), RING_DEADLINE_S)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - includes the deadline and a missing key
                detail = {"error": type(exc).__name__}
                continue
            if status != 200:
                if status in (401, 403):
                    self._ring_key = None
                detail = {"status": status, "title": telnyx_error_title(body)}
                continue
            try:
                events = json.loads(body or b"").get("data")
            except (ValueError, AttributeError):
                events = None
            if not isinstance(events, list):
                detail = {"error": "unreadable answer"}
                continue
            kind, found = classify_ring(events)
            if kind != "pending":
                return kind, found
            detail = {"error": "no hangup or answer event yet"}
        return "unknown", detail

    async def _follow_ring(self, target: Target, channel: str, leg_id: str | None, attempt: int,
                           detection_id: str | None = None, row_id: int | None = None) -> None:
        """Log the ring's terminal outcome (and write it to the notification log); retry a busy phone once;
        send ONE line when it never rang through. Never raises."""
        masked = mask_channel(channel)
        try:
            if not leg_id:
                kind, detail = "unknown", {"error": "the dial answer carried no call_leg_id"}
            else:
                await asyncio.sleep(ring_outcome_delay(channel))
                kind, detail = await self._lookup_outcome(leg_id)
            fields = {k: v for k, v in detail.items() if v is not None}
            self._note_update(row_id, kind, self._cause_parts(fields, with_error=True))
            base = {"watch_id": target.id, "channel": masked, "call_leg_id": leg_id, "attempt": attempt}
            if kind == "answered":
                log("ring_answered", **base)
                return
            if kind == "unanswered":
                log("ring_unanswered", **base, **fields)
            elif kind in ("busy", "rejected"):
                log("ring_rejected", **base, **fields, busy=kind == "busy")
            else:
                log("ring_outcome_unknown", **base, **fields)
            if kind == "busy" and attempt == 1:
                await asyncio.sleep(RING_BUSY_RETRY_S)
                if await self._busy_retry(target, channel, detection_id):
                    return
                why = "the phone was busy and the retry could not be placed (quiet hours or a Telnyx refusal)"
            else:
                why = {"unanswered": "nobody answered", "busy": "the phone was busy",
                       "rejected": "the call was declined", "unknown": "the outcome could not be read from Telnyx"}[kind]
                why += self._cause_text(fields)
                if attempt == 2:
                    why += "; retried once because the operator's line was busy"
            await self._tell_ring_failed(target, masked, why)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - nothing in the ring path raises
            log("ring_outcome_failed", watch_id=target.id, channel=masked, error=type(exc).__name__)

    @staticmethod
    def _cause_parts(fields: dict, *, with_error: bool = False) -> str:
        keys = [("hangup_cause", "hangup_cause"), ("sip_hangup_cause", "SIP"), ("hangup_source", "by"), ("status", "HTTP")]
        if with_error:
            keys += [("title", "title"), ("error", "error")]
        return ", ".join(f"{label} {fields[key]}" for key, label in keys if fields.get(key))

    @classmethod
    def _cause_text(cls, fields: dict) -> str:
        parts = cls._cause_parts(fields)
        return f" ({parts})" if parts else ""

    async def _busy_retry(self, target: Target, channel: str, detection_id: str | None = None, *, after: str = "busy") -> bool:
        """The one retry. True when a second call was placed (its own follower reports it). Quiet hours still
        apply; the cooldown is neither checked nor rewritten: the first ring of the burst holds it."""
        masked = mask_channel(channel)
        fresh = self.registry.get(target.id) or target
        local = self.clock().astimezone(RING_TZ)
        if in_quiet_hours(fresh.sink.ring_quiet_hours, local.hour * 60 + local.minute):
            log("ring_quiet", watch_id=target.id, channel=masked, retry=True)
            self._note(detection_id, target.id, masked, "quiet", "retry suppressed by quiet hours", attempt=2)
            return False
        log("ring_busy_retry", watch_id=target.id, channel=masked, after=after)
        outcome, leg_id, detail = await self._dial(target.id, channel, masked, ring_display_name(fresh.display_name), record=False)
        if outcome != "sent":
            self._note(detection_id, target.id, masked, outcome, detail, attempt=2)
            return False
        row = self._note(detection_id, target.id, masked, "placed", detail, attempt=2, ref=leg_id)
        self._follow(target, channel, leg_id, attempt=2, detection_id=detection_id, row_id=row)
        return True

    async def _retry_after_refusal(self, target: Target, channel: str, detection_id: str | None) -> None:
        """The one retry of a dial Telnyx refused for lack of a free channel. Its own follower reports the
        outcome; when it cannot be placed either, ONE line says so. Never raises."""
        masked = mask_channel(channel)
        try:
            await asyncio.sleep(RING_BUSY_RETRY_S)
            if await self._busy_retry(target, channel, detection_id, after="channel_limit"):
                return
            await self._tell_ring_failed(
                target, masked, "Telnyx had no free channel (the operator was on another call) and the retry could not be "
                                "placed (quiet hours or a Telnyx refusal)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - nothing in the ring path raises
            log("ring_outcome_failed", watch_id=target.id, channel=masked, error=type(exc).__name__)

    async def _tell_ring_failed(self, target: Target, masked: str, why: str) -> None:
        line = f"target-watch: the phone ring for {target.display_name} ({masked}) did not get through: {why}."
        channels = sorted((c for c in target.sink.notify if not is_ring(c) and not is_earshot(c)), key=channel_rank)
        if not channels:
            log("ring_failure_unreported", watch_id=target.id, reason="target has no telegram or ntfy channel")
            return
        for channel in channels:
            if is_ntfy(channel):
                await self._ntfy(channel, f"{target.display_name} - ring did not get through", line, None)
            else:
                await asyncio.to_thread(self._send, channel, line)

    def ring_plan(self, target: Target | None, channel: str, received_at: float | None = None) -> dict:
        connection_id, from_number = self.ring_config()
        timeout_secs, time_limit_secs = ring_timing(channel)
        return {"to": channel[len(RING_PREFIX):], "from": from_number, "connection_id": connection_id,
                "from_display_name": ring_display_name(target.display_name) if target else None,
                "timeout_secs": timeout_secs, "time_limit_secs": time_limit_secs,
                "skipped": self.ring_skip(target, channel, received_at)}

    def _routed(self, detection: dict, action: str, ticket_ref, channels, reason) -> dict:
        data = detection["data"]
        routed_data = {
            "schema_version": 1,
            "watch_id": data["watch_id"],
            "detection_event_id": detection["id"],
            "action": action,
            "ticket_ref": ticket_ref,
            "notify_channels": list(channels),
            "reason": reason,
            "routed_at": env.rfc3339(self.clock()),
        }
        return env.build(
            env.ROUTED, routed_data,
            event_id=env.routed_id(detection["id"], action),
            ordering_key=f"watch:{data['watch_id']}",
            correlationid=detection.get("correlationid"),
            causationid=detection["id"],
            now=self.clock(),
        )

    async def handle(self, detection: dict) -> dict | None:
        """Route one detection. Returns the routed envelope (published, or printed on --dry-run)."""
        if detection.get("type") != env.DETECTED:
            return None
        try:
            self.validate(detection)
        except Exception as exc:  # noqa: BLE001
            raise Permanent(f"detection refused by the contract: {exc}") from exc
        detection_id = detection["id"]
        data = detection["data"]

        if self.dry_run:
            prior = self.state.route(detection_id)
            if prior and prior["published"]:
                self._print({"detection_id": detection_id, "already_routed": prior["action"], "ticket_ref": prior["ticket_ref"]})
                return prior["envelope"]
            plan = self.plan(detection)
            target = self.registry.get(data["watch_id"])
            routed = self._routed(detection, plan.action, plan.ticket_ref, plan.notify, plan.reason)
            self._print({
                "detection_id": detection_id, "action": plan.action, "ticket_ref": plan.ticket_ref,
                "px": {"argv": plan.px_argv, "cwd": plan.px_cwd} if plan.px_argv else None,
                "notify": [self._dry_notify(data, target, plan, c) for c in plan.notify],
                "reason": plan.reason, "routed": routed,
            })
            return routed

        route = self.state.route(detection_id)
        if route and route["published"]:
            log("sink_already_routed", detection_id=detection_id, action=route["action"])
            return route["envelope"]
        if route and route["envelope"]:
            # Acted and notified before; only the publish is missing.
            await self.publisher.publish(route["envelope"])
            self.state.update_route(detection_id, published=1)
            return route["envelope"]

        if route is None:
            self.state.open_route(detection_id, data["watch_id"], conversation_key(data),
                                  text_hash(data["content"].get("text") or ""), epoch(data["received_at"]))
            route = self.state.route(detection_id)

        ticket_url = None  # px's own link, known only in the run that filed or commented the ticket
        if route["action"] is not None:
            # Acted before a crash: reuse the outcome, never act twice.
            action, ticket_ref, reason = route["action"], route["ticket_ref"], route["reason"]
        elif route["px_state"] == "pending":
            # px was running when we stopped and its outcome is unknown. Never re-run it.
            action, ticket_ref = "failed", None
            reason = "interrupted while px was running; outcome unknown, not retried, check the board"
            self.state.update_route(detection_id, px_state="failed", action=action, reason=reason)
        else:
            async with self._px_lock:  # the coalescing read, px and the ticket write are one step (see __init__)
                plan = self.plan(detection)
                action, ticket_ref, reason = plan.action, plan.ticket_ref, plan.reason
                if plan.px_argv:
                    # Intent first: from here a crash can only end in 'failed', never a second create.
                    self.state.update_route(detection_id, px_kind=plan.action, px_state="pending")
                    action, ticket_ref, reason = await self._run_px(plan)
                    ticket_url = plan.ticket_url
                self.state.update_route(
                    detection_id, px_state=("failed" if action == "failed" else "done") if plan.px_argv else "none",
                    action=action, ticket_ref=ticket_ref, reason=reason,
                )

        target = self.registry.get(data["watch_id"])
        channels = list(target.sink.notify) if target and action != "skipped_duplicate" else []
        sent = route["notify_channels"] if route["notified"] else []
        if channels and not route["notified"]:
            actions = None
            if action == AWAITING_BUTTON and target:
                actions = await self._offer_button(detection, target)
                if actions is None:
                    reason = (reason + "; " if reason else "") + "Create Ticket button unavailable (no readable button secret); nothing was filed"
            sent, failed = await self._notify(channels, notify_line(data, action, ticket_ref, button=actions is not None), target,
                                              epoch(data["received_at"]), detection_id=detection_id, title=ntfy_title(data),
                                              click=ticket_url, actions=actions,
                                              spoken=spoken_line(data, content=bool(target and target.sink.earshot_content)))
            if failed:
                reason = (reason + "; " if reason else "") + "notify failed: " + ",".join(mask_channel(c) for c in failed)
            self.state.update_route(detection_id, notified=1, notify_channels=sent, reason=reason)

        routed = self._routed(detection, action, ticket_ref, sent, reason)
        self.validate(routed)
        self.state.update_route(detection_id, envelope=routed)
        await self.publisher.publish(routed)
        self.state.update_route(detection_id, published=1)
        log("sink_routed", detection_id=detection_id, watch_id=data["watch_id"], action=action,
            ticket_ref=ticket_ref, notified=len(sent))
        return routed

    def _dry_notify(self, data: dict, target: Target | None, plan: Plan, channel: str) -> dict:
        if is_ring(channel):
            return {"to": channel, "ring": self.ring_plan(target, channel, epoch(data["received_at"]))}
        text = notify_line(data, plan.action, plan.ticket_ref or "<new>")
        if is_earshot(channel):
            spoken = spoken_line(data, content=bool(target and target.sink.earshot_content))
            return {"to": channel, "spoken": spoken, "argv": self._earshot_argv(channel[len(EARSHOT_PREFIX):], spoken),
                    "skipped": self.ring_skip(target, channel, epoch(data["received_at"]))}
        if is_ntfy(channel):
            out = {"to": channel, "text": text, "title": ntfy_title(data), "priority": "urgent",
                   "url": f"{ntfy_base_url()}/{channel[len(NTFY_PREFIX):]}"}
            if plan.action == AWAITING_BUTTON:
                out["button"] = {"label": btn.BUTTON_LABEL, "method": "POST",
                                 "url": f"{button_base_url()}/<detection id>?exp=<now + 7 days>&sig=<hmac>"}  # signed at send time
            return out
        return {"to": channel, "text": text}

    def _print(self, record: dict) -> None:
        print(json.dumps(record, sort_keys=True, default=str), file=self.out, flush=True)
