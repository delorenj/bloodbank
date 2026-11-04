"""`target-watch sink`: watch.target.detected in; a ticket, a notification and watch.target.routed out.

Safety rules, each paid for elsewhere:
- A ticket create is never retried blindly. The intent is written to sqlite
  before px runs; a redelivery that finds an intent without an outcome marks
  the detection failed and notifies, instead of filing a second ticket.
- px success is read from its FIRST matching output line ('created JIMB-450 ...',
  'commented on JIMB-450'), never from the last line (the URL).
- Everything is idempotent per detection id: a redelivered detection that was
  already routed only republishes its stored routed event.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import envelope as env
from .bus import Permanent
from .log import log
from .matcher import epoch, text_hash
from .state import State
from .targets import Target, TargetRegistry

CREATED = re.compile(r"^created ([A-Z][A-Z0-9]*-[0-9]+)\b")
COMMENTED = re.compile(r"^commented on ([A-Z][A-Z0-9]*-[0-9]+)\b")
TEXT_DUPLICATE_S = 60.0
PX_TIMEOUT_S = 90
NOTIFY_TIMEOUT_S = 30
FOOTER = "Filed automatically by target-watch from a {provider} message. Do not invent a ticket number."


def run_command(argv: list[str], cwd: str | None = None, timeout: float = 60) -> tuple[int, str, str]:
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "") if isinstance(exc.stdout, str) else "", "timeout"
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}"
    return done.returncode, done.stdout or "", done.stderr or ""


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
                 px: str = "px", hermes: str = "hermes", dry_run: bool = False, validate=env.validate, clock=None, out=None):
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

    # --- planning (pure: reads state, writes nothing) -------------------------

    def plan(self, detection: dict) -> Plan:
        data = detection["data"]
        detection_id = detection["id"]
        conv_key = conversation_key(data)
        target = self.registry.get(data["watch_id"])
        if target is None:
            return Plan(detection_id, data["watch_id"], "failed", conv_key,
                        reason="no target file defines this watch_id")
        notify = list(target.sink.notify)
        digest = text_hash(data["content"].get("text") or "")
        if digest:
            twin = self.state.recent_text_route(data["watch_id"], digest, epoch(data["received_at"]), detection_id, TEXT_DUPLICATE_S)
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

    def _notify(self, channels: list[str], line: str) -> tuple[list[str], list[str]]:
        sent, failed = [], []
        for channel in channels:
            code, _, _ = self.runner([self.hermes, "send", "--to", channel, "--quiet", "--", line], cwd=None, timeout=NOTIFY_TIMEOUT_S)
            (sent if code == 0 else failed).append(channel)
        return sent, failed

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
            routed = self._routed(detection, plan.action, plan.ticket_ref, plan.notify, plan.reason)
            self._print({
                "detection_id": detection_id, "action": plan.action, "ticket_ref": plan.ticket_ref,
                "px": {"argv": plan.px_argv, "cwd": plan.px_cwd} if plan.px_argv else None,
                "notify": [{"to": c, "text": notify_line(data, plan.action, plan.ticket_ref or "<new>")} for c in plan.notify],
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
            sent, failed = self._notify(channels, notify_line(data, action, ticket_ref))
            if failed:
                reason = (reason + "; " if reason else "") + "notify failed: " + ",".join(failed)
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
        import json

        print(json.dumps(record, sort_keys=True, default=str), file=self.out, flush=True)
