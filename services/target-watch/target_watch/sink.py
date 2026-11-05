"""`target-watch sink`: watch.target.detected in; a ticket, a notification and watch.target.routed out.

Safety rules, each paid for elsewhere:
- A ticket create is never retried blindly. The intent is written to sqlite
  before px runs; a redelivery that finds an intent without an outcome marks
  the detection failed and notifies, instead of filing a second ticket.
- px success is read from its FIRST matching output line ('created JIMB-450 ...',
  'commented on JIMB-450'), never from the last line (the URL).
- Everything is idempotent per detection id: a redelivered detection that was
  already routed only republishes its stored routed event.
- A "ring:<E.164>" channel places a Telnyx Call Control call so the operator's phone
  rings like an incoming call. It never carries speech, never logs the API key, rings at
  most once per cooldown per (watch, channel), and goes after the Telegram line.
"""
from __future__ import annotations

import json
import re
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

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
NOTIFY_TIMEOUT_S = 30
HERMES_PROFILE = os.environ.get("TARGET_WATCH_HERMES_PROFILE") or "33god-pm"
NOTIFY_CWD = os.environ.get("TARGET_WATCH_NOTIFY_CWD") or os.path.expanduser("~")

RING_PREFIX = "ring:"
TELNYX_CALLS_URL = "https://api.telnyx.com/v2/calls"
# The ring stops after 20 s, before the cell carrier's no-answer forward (about 25-30 s on Verizon).
# That forward sends an unanswered call back to the same Telnyx number, which would start a loop.
# Keep this <= 22 and never raise it without changing the cell's forwarding first.
RING_TIMEOUT_SECS = 20
RING_TIME_LIMIT_SECS = 40  # an answered call is silent and ends here
RING_HTTP_TIMEOUT_S = 15
OP_TIMEOUT_S = 20
RING_TZ = ZoneInfo("America/New_York")
DEFAULT_KEY_REF = "op://DeLoSecrets/74k7nz3xkryqns4lpf65a323ue/credential"
# Cloudflare-fronted APIs refuse Python-urllib's default User-Agent.
RING_USER_AGENT = "target-watch/0.1 (bloodbank)"
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


def mask_channel(channel: str) -> str:
    """Log form of a channel: a ring target's number is personal data, so keep the last 4 digits only."""
    return f"{RING_PREFIX}...{channel[-4:]}" if is_ring(channel) else channel


def telnyx_http(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, bytes]:
    """POST; (status, body) for any HTTP answer. Timeouts and connection errors raise OSError."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def telnyx_error_title(body: bytes) -> str | None:
    try:
        errors = json.loads(body or b"").get("errors")
        return str(errors[0].get("title"))[:120] if errors else None
    except (ValueError, AttributeError, IndexError, TypeError):
        return None


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


def notify_line(data: dict, action: str, ticket_ref: str | None) -> str:
    tail = f"{action} {ticket_ref}" if ticket_ref else action
    return f"{data['target_display_name']} via {data['provider']}: {headline(data, 120)} -> {tail}"


class SinkService:
    def __init__(self, registry: TargetRegistry, state: State, publisher, *, runner=run_command,
                 px: str = "px", hermes: str = "hermes", dry_run: bool = False, validate=env.validate, clock=None, out=None,
                 http=telnyx_http, op: str | None = None):
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
        self.op = op or os.environ.get("TARGET_WATCH_OP") or os.path.expanduser("~/.local/bin/op")
        self._ring_key: str | None = None  # resolved at first use, held in memory only

    # --- planning (pure: reads state, writes nothing) -------------------------

    def plan(self, detection: dict) -> Plan:
        data = detection["data"]
        detection_id = detection["id"]
        conv_key = conversation_key(data)
        target = self.registry.get(data["watch_id"])
        if target is None:
            return Plan(detection_id, data["watch_id"], "failed", conv_key,
                        reason="no target file defines this watch_id")
        notify = sorted(target.sink.notify, key=is_ring)  # telegram first, then the ring
        digest = text_hash(data["content"].get("text") or "")
        if digest:
            twin = self.state.recent_text_route(data["watch_id"], digest, epoch(data["received_at"]), detection_id, TEXT_DUPLICATE_S,
                                                 conv_key, CARRIER_TWINS)
            if twin:
                return Plan(detection_id, target.id, "skipped_duplicate", conv_key,
                            reason=f"same text as detection {twin} within {int(TEXT_DUPLICATE_S)}s")
        if target.sink.kind == "notify_only":
            return Plan(detection_id, target.id, "notified_only", conv_key, notify=notify)
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

    def _run_px(self, plan: Plan, data: dict) -> tuple[str, str | None, str | None]:
        """(action, ticket_ref, reason). Never retried: an unclear outcome is a failure."""
        code, stdout, stderr = self.runner(plan.px_argv, cwd=plan.px_cwd, timeout=PX_TIMEOUT_S)
        if plan.action == "ticket_created":
            ref = first_match(CREATED, stdout)
            if ref:
                self.state.record_ticket(plan.watch_id, plan.conv_key, ref, self.clock().timestamp())
                return "ticket_created", ref, None if code == 0 else f"px exited {code} after creating {ref}"
            return "failed", None, f"px task create did not confirm a ticket (exit {code}); not retried, check the board"
        ref = first_match(COMMENTED, stdout)
        if ref:
            return "ticket_commented", ref, None
        return "failed", plan.ticket_ref, f"px comment did not confirm (exit {code}); not retried"

    def _notify(self, channels: list[str], line: str, target: Target | None = None) -> tuple[list[str], list[str]]:
        """(sent, failed). Telegram first, then the ring: the line the operator reads after the phone
        rings must already be there. A throttled or quiet ring is neither sent nor failed."""
        # Name the hermes profile, as Candybar's pager does: without -p, hermes
        # resolves a profile from its surroundings, and from a service process on
        # 2026-11-04 it found one with no Telegram token and exited 1. Success is
        # the JSON verdict, not the exit code alone.
        sent, failed = [], []
        for channel in sorted(channels, key=is_ring):
            if is_ring(channel):
                outcome = self._ring(target, channel)
                if outcome == "sent":
                    sent.append(channel)
                elif outcome == "failed":
                    failed.append(channel)
                continue
            argv = [self.hermes, "-p", HERMES_PROFILE, "send", "--to", channel, "--json", "--", line]
            code, stdout, stderr = self.runner(argv, cwd=NOTIFY_CWD, timeout=NOTIFY_TIMEOUT_S)
            if code == 0 and confirmed(stdout):
                sent.append(channel)
            else:
                failed.append(channel)
                log("notify_failed", channel=channel, exit=code, profile=HERMES_PROFILE,
                    error=notify_error(stdout) or (stderr or "").strip()[-300:])
        return sent, failed

    # --- ring ---------------------------------------------------------------------

    @staticmethod
    def ring_config() -> tuple[str | None, str | None]:
        return os.environ.get("TARGET_WATCH_TELNYX_CONNECTION_ID") or None, os.environ.get("TARGET_WATCH_RING_FROM") or None

    def ring_skip(self, target: Target | None, channel: str) -> str | None:
        """'quiet' or 'throttled' when this ring must not be placed now, else None. Reads state, writes nothing."""
        if target is None:
            return None
        now = self.clock()
        local = now.astimezone(RING_TZ)
        if in_quiet_hours(target.sink.ring_quiet_hours, local.hour * 60 + local.minute):
            return "quiet"
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

    def _ring(self, target: Target | None, channel: str) -> str:
        """Place the call. 'sent' | 'failed' | 'throttled' | 'quiet'."""
        watch_id = target.id if target else "?"
        masked = mask_channel(channel)
        skip = self.ring_skip(target, channel)
        if skip:
            log(f"ring_{skip}", watch_id=watch_id, channel=masked)
            return skip
        connection_id, from_number = self.ring_config()
        if not connection_id or not from_number:
            log("ring_failed", watch_id=watch_id, channel=masked,
                reason="TARGET_WATCH_TELNYX_CONNECTION_ID and TARGET_WATCH_RING_FROM must both be set")
            return "failed"
        key = self._key()
        if key is None:
            return "failed"
        body = json.dumps({
            "connection_id": connection_id, "to": channel[len(RING_PREFIX):], "from": from_number,
            "timeout_secs": RING_TIMEOUT_SECS, "time_limit_secs": RING_TIME_LIMIT_SECS,
        }).encode()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": RING_USER_AGENT}
        try:
            status, answer = self.http(TELNYX_CALLS_URL, headers, body, RING_HTTP_TIMEOUT_S)
        except OSError as exc:  # timeouts, DNS, TLS: the type only, never the request
            log("ring_failed", watch_id=watch_id, channel=masked, reason="transport", error=type(exc).__name__)
            return "failed"
        call_id = None
        if 200 <= status < 300:
            try:
                call_id = (json.loads(answer or b"").get("data") or {}).get("call_control_id")
            except (ValueError, AttributeError):
                call_id = None
        if not call_id:
            if status in (401, 403):
                self._ring_key = None  # a rotated key is re-read next time
            log("ring_failed", watch_id=watch_id, channel=masked, status=status, title=telnyx_error_title(answer),
                reason=None if status >= 300 else "no call_control_id in the answer")
            return "failed"
        self.state.record_ring(watch_id, channel, self.clock().timestamp())
        log("ring_placed", watch_id=watch_id, channel=masked, call_control_id=call_id)
        return "sent"

    def ring_plan(self, target: Target | None, channel: str) -> dict:
        connection_id, from_number = self.ring_config()
        return {"to": channel[len(RING_PREFIX):], "from": from_number, "connection_id": connection_id,
                "timeout_secs": RING_TIMEOUT_SECS, "time_limit_secs": RING_TIME_LIMIT_SECS,
                "skipped": self.ring_skip(target, channel)}

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
                "notify": [
                    {"to": c, "ring": self.ring_plan(target, c)} if is_ring(c)
                    else {"to": c, "text": notify_line(data, plan.action, plan.ticket_ref or "<new>")}
                    for c in plan.notify
                ],
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

        if route["action"] is not None:
            # Acted before a crash: reuse the outcome, never act twice.
            action, ticket_ref, reason = route["action"], route["ticket_ref"], route["reason"]
        elif route["px_state"] == "pending":
            # px was running when we stopped and its outcome is unknown. Never re-run it.
            action, ticket_ref = "failed", None
            reason = "interrupted while px was running; outcome unknown, not retried, check the board"
            self.state.update_route(detection_id, px_state="failed", action=action, reason=reason)
        else:
            plan = self.plan(detection)
            action, ticket_ref, reason = plan.action, plan.ticket_ref, plan.reason
            if plan.px_argv:
                # Intent first: from here a crash can only end in 'failed', never a second create.
                self.state.update_route(detection_id, px_kind=plan.action, px_state="pending")
                action, ticket_ref, reason = self._run_px(plan, data)
            self.state.update_route(
                detection_id, px_state=("failed" if action == "failed" else "done") if plan.px_argv else "none",
                action=action, ticket_ref=ticket_ref, reason=reason,
            )

        target = self.registry.get(data["watch_id"])
        channels = list(target.sink.notify) if target and action != "skipped_duplicate" else []
        sent = route["notify_channels"] if route["notified"] else []
        if channels and not route["notified"]:
            sent, failed = self._notify(channels, notify_line(data, action, ticket_ref), target)
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

    def _print(self, record: dict) -> None:
        print(json.dumps(record, sort_keys=True, default=str), file=self.out, flush=True)
