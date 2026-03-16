"""Bloodbank -> Command Adapter Team Infra dispatcher.

Consumes Plane webhook events already published to Bloodbank (routing key
`webhook.plane.*`), applies Ready-gate rules, de-duplicates updates, and
publishes command events to Bloodbank for routing via the Command Adapter.

This decouples the dispatcher from OpenClaw, allowing any agent to receive
ticket assignments via the pluggable dispatcher system.

Routing priority (highest to lowest):
  1. ``agent:X`` label  →  command.X.assign_ticket
  2. ``comp:X`` label   →  command.X.assign_ticket  (M2-gated)
  3. (no label)         →  command.{default_agent}.examine

Environment variables:
- INFRA_READY_STATES             (default: unstarted)
- INFRA_READY_LABELS             (default: ready,automation:go)
- INFRA_COMPONENT_LABEL_PREFIX   (default: comp:)
- INFRA_AGENT_LABEL_PREFIX       (default: agent:)
- INFRA_DISPATCH_STATE_PATH      (default: .infra_dispatch_state.json)
- INFRA_RUN_CHECKS               (default: true)
- INFRA_CHECK_TIMEOUT_SECONDS    (default: 900)
- INFRA_COMPONENT_CHECKS_JSON    (optional JSON map of component -> {cwd, command})
- INFRA_DEFAULT_AGENT            (default: cack) - agent for tickets without component/agent label
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from event_producers.events.base import EventEnvelope
from event_producers.events.core.consumer import EventConsumer
from event_producers.rabbit import Publisher

logger = logging.getLogger(__name__)

DEFAULT_COMPONENT_CHECKS: dict[str, dict[str, str]] = {
    "bloodbank": {
        "cwd": "/home/delorenj/code/33GOD/bloodbank",
        "command": "mise x -- uv run pytest -q tests/test_infra_dispatcher.py",
    },
    "candystore": {
        "cwd": "/home/delorenj/code/33GOD/candystore",
        "command": "mise x -- uv run pytest -q",
    },
    "candybar": {
        "cwd": "/home/delorenj/code/33GOD/candybar",
        "command": "bun run lint && bun run build",
    },
    "holyfields": {
        "cwd": "/home/delorenj/code/33GOD/holyfields",
        "command": "mise run test:all",
    },
    "pjangler": {
        "cwd": "/home/delorenj/code/pjangler",
        "command": "bun run build",
    },
}


def _split_csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return default
    out = [item.strip().lower() for item in value.split(",") if item.strip()]
    return out or default


def _normalize_token(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _decode_check_output(raw: bytes, limit: int = 1200) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def _load_component_checks(raw_json: str | None) -> dict[str, dict[str, str]]:
    if not raw_json:
        return dict(DEFAULT_COMPONENT_CHECKS)

    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid INFRA_COMPONENT_CHECKS_JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("INFRA_COMPONENT_CHECKS_JSON must be a JSON object")

    checks: dict[str, dict[str, str]] = {}
    for component, cfg in parsed.items():
        key = _normalize_token(str(component))
        if not key or not isinstance(cfg, dict):
            continue

        command = str(cfg.get("command") or "").strip()
        cwd = str(cfg.get("cwd") or "").strip()
        if not command or not cwd:
            continue

        checks[key] = {"command": command, "cwd": cwd}

    return checks


@dataclass(slots=True)
class DispatcherConfig:
    ready_states: tuple[str, ...] = ("unstarted",)
    ready_labels: tuple[str, ...] = ("ready", "automation-go")
    component_label_prefix: str = "comp:"
    agent_label_prefix: str = "agent:"
    state_path: Path = Path(".infra_dispatch_state.json")
    run_checks: bool = True
    check_timeout_seconds: int = 900
    component_checks: dict[str, dict[str, str]] = field(default_factory=dict)
    default_agent: str = "cack"

    @classmethod
    def from_env(cls) -> "DispatcherConfig":
        ready_states = tuple(_split_csv(os.getenv("INFRA_READY_STATES"), ["unstarted"]))
        ready_labels = tuple(
            _split_csv(os.getenv("INFRA_READY_LABELS"), ["ready", "automation:go"])
        )
        ready_labels = tuple(_normalize_token(x) for x in ready_labels if x)

        prefix = os.getenv("INFRA_COMPONENT_LABEL_PREFIX", "comp:").strip().lower()
        agent_prefix = (
            os.getenv("INFRA_AGENT_LABEL_PREFIX", "agent:").strip().lower() or "agent:"
        )
        state_path = Path(
            os.getenv("INFRA_DISPATCH_STATE_PATH", ".infra_dispatch_state.json")
        ).expanduser()
        run_checks = os.getenv("INFRA_RUN_CHECKS", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        timeout_raw = os.getenv("INFRA_CHECK_TIMEOUT_SECONDS", "900").strip()
        try:
            check_timeout_seconds = max(30, int(timeout_raw))
        except ValueError:
            check_timeout_seconds = 900

        component_checks = _load_component_checks(
            os.getenv("INFRA_COMPONENT_CHECKS_JSON")
        )
        default_agent = (
            os.getenv("INFRA_DEFAULT_AGENT", "cack").strip().lower() or "cack"
        )

        return cls(
            ready_states=ready_states,
            ready_labels=ready_labels,
            component_label_prefix=prefix,
            agent_label_prefix=agent_prefix,
            state_path=state_path,
            run_checks=run_checks,
            check_timeout_seconds=check_timeout_seconds,
            component_checks=component_checks,
            default_agent=default_agent,
        )


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._seen: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._seen = {str(k): str(v) for k, v in data.items()}
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to load state file %s: %s", self.path, exc)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._seen, indent=2, sort_keys=True), encoding="utf-8"
        )

    def mark_if_new(self, issue_id: str, fingerprint: str) -> bool:
        """Returns True if this fingerprint is new for the issue."""
        existing = self._seen.get(issue_id)
        if existing == fingerprint:
            return False
        self._seen[issue_id] = fingerprint
        self._save()
        return True


def _unwrap_plane_body(raw_payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract Plane webhook body from Bloodbank envelope payload."""
    payload = raw_payload

    try:
        envelope = EventEnvelope.model_validate(raw_payload)
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
    except Exception:
        # allow raw payloads in tests/manual publish paths
        pass

    if not isinstance(payload, dict):
        return None

    if payload.get("provider") == "plane" and isinstance(payload.get("body"), dict):
        return payload["body"]

    if payload.get("event") == "issue":
        return payload

    return None


def _extract_state_slug(issue: dict[str, Any]) -> str:
    candidates: list[Any] = []

    state_detail = issue.get("state_detail")
    if isinstance(state_detail, dict):
        candidates.extend(
            [
                state_detail.get("name"),
                state_detail.get("slug"),
                state_detail.get("key"),
            ]
        )

    state = issue.get("state")
    if isinstance(state, dict):
        candidates.extend([state.get("name"), state.get("slug"), state.get("key")])
    elif isinstance(state, str):
        candidates.append(state)

    for c in candidates:
        slug = _normalize_token(str(c))
        if slug:
            return slug
    return ""


def _extract_labels(issue: dict[str, Any]) -> list[str]:
    raw = issue.get("labels")
    if not isinstance(raw, list):
        return []

    labels: list[str] = []
    for item in raw:
        if isinstance(item, str):
            token = _normalize_token(item)
            if token:
                labels.append(token)
            continue
        if isinstance(item, dict):
            for key in ("name", "label", "slug"):
                token = _normalize_token(str(item.get(key) or ""))
                if token:
                    labels.append(token)
                    break
    return labels


def _extract_component(labels: list[str], prefix: str) -> str | None:
    normalized_prefix = _normalize_token(prefix.replace(":", "-"))
    if not normalized_prefix:
        normalized_prefix = "comp"

    for label in labels:
        if label.startswith(f"{normalized_prefix}-"):
            return label.removeprefix(f"{normalized_prefix}-")
    return None


def _extract_agent_label(labels: list[str], prefix: str) -> str | None:
    normalized_prefix = _normalize_token(prefix.replace(":", "-"))
    if not normalized_prefix:
        normalized_prefix = "agent"

    for label in labels:
        if label.startswith(f"{normalized_prefix}-"):
            return label.removeprefix(f"{normalized_prefix}-")
    return None


def _ticket_ref(issue: dict[str, Any]) -> str:
    identifier = issue.get("identifier")
    if identifier:
        return str(identifier)

    seq = issue.get("sequence_id")
    project = (
        issue.get("project_detail")
        if isinstance(issue.get("project_detail"), dict)
        else {}
    )
    proj_ident = project.get("identifier")
    if proj_ident and seq is not None:
        return f"{proj_ident}-{seq}"

    issue_id = issue.get("id")
    return str(issue_id) if issue_id else "(unknown-ticket)"


def evaluate_ready_issue(
    body: dict[str, Any],
    *,
    ready_states: tuple[str, ...],
    ready_labels: tuple[str, ...],
    component_prefix: str,
    agent_prefix: str = "agent:",
) -> dict[str, Any] | None:
    event = str(body.get("event") or "").lower()
    action = str(body.get("action") or "").lower()
    if event != "issue" or action not in {"create", "update"}:
        return None

    issue = body.get("data")
    if not isinstance(issue, dict):
        return None

    issue_id = str(issue.get("id") or "")
    if not issue_id:
        return None

    state_slug = _extract_state_slug(issue)
    labels = _extract_labels(issue)

    if state_slug not in set(ready_states):
        return None

    if ready_labels and not any(label in set(ready_labels) for label in labels):
        return None

    updated_at = str(issue.get("updated_at") or issue.get("created_at") or "")
    fingerprint = f"{action}:{updated_at}:{state_slug}:{','.join(sorted(labels))}"

    return {
        "issue_id": issue_id,
        "ticket_ref": _ticket_ref(issue),
        "title": str(issue.get("name") or "(untitled)").strip(),
        "updated_at": updated_at,
        "state": state_slug,
        "labels": labels,
        "agent_label": _extract_agent_label(labels, agent_prefix),
        "component": _extract_component(labels, component_prefix),
        "url": issue.get("url") or issue.get("issue_url") or "",
        "fingerprint": fingerprint,
        "workspace_id": str(body.get("workspace_id") or ""),
    }


async def run_component_check(
    config: DispatcherConfig, ticket: dict[str, Any]
) -> dict[str, Any]:
    if not config.run_checks:
        return {"status": "disabled"}

    component = _normalize_token(ticket.get("component"))
    if not component:
        return {"status": "skipped", "reason": "missing_component"}

    checks = config.component_checks or {}
    check = checks.get(component)
    if not check:
        return {
            "status": "missing",
            "reason": "no_check_config",
            "component": component,
        }

    command = str(check.get("command") or "").strip()
    cwd_raw = str(check.get("cwd") or "").strip()
    cwd = Path(cwd_raw).expanduser()

    if not command:
        return {
            "status": "missing",
            "reason": "missing_command",
            "component": component,
        }
    if not cwd.exists():
        return {
            "status": "missing",
            "reason": "missing_cwd",
            "component": component,
            "cwd": str(cwd),
            "command": command,
        }

    started = time.monotonic()
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=float(config.check_timeout_seconds),
        )
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return {
            "status": "timeout",
            "component": component,
            "cwd": str(cwd),
            "command": command,
            "timeout_seconds": config.check_timeout_seconds,
        }

    duration = round(time.monotonic() - started, 2)
    return {
        "status": "passed" if proc.returncode == 0 else "failed",
        "component": component,
        "cwd": str(cwd),
        "command": command,
        "exit_code": proc.returncode,
        "duration_seconds": duration,
        "stdout_tail": _decode_check_output(stdout),
        "stderr_tail": _decode_check_output(stderr),
    }


def build_dispatch_message(ticket: dict[str, Any]) -> str:
    agent_label = ticket.get("agent_label")
    component = ticket.get("component")
    routing = agent_label or component or "UNKNOWN"
    check = ticket.get("m2_check") if isinstance(ticket.get("m2_check"), dict) else None

    lines = [
        "Team Infra dispatch event from Bloodbank/Plane.",
        "",
        f"Ticket: {ticket['ticket_ref']}",
        f"Title: {ticket['title']}",
        f"State: {ticket['state']}",
        f"Labels: {', '.join(ticket['labels']) if ticket['labels'] else '(none)'}",
        f"Agent label: {agent_label or '(not set)'}",
        f"Component route: {component or '(not set)'}",
        f"Effective route: {routing}",
        f"Plane URL: {ticket.get('url') or '(not provided)'}",
    ]

    if check:
        lines.extend(
            [
                "",
                "M2 Test Gate:",
                f"- Status: {check.get('status', 'unknown')}",
                f"- Command: {check.get('command', '(none)')}",
                f"- CWD: {check.get('cwd', '(none)')}",
            ]
        )
        if check.get("duration_seconds") is not None:
            lines.append(f"- Duration: {check['duration_seconds']}s")
        if check.get("exit_code") is not None:
            lines.append(f"- Exit code: {check['exit_code']}")
        if check.get("reason"):
            lines.append(f"- Reason: {check['reason']}")

        status = str(check.get("status") or "")
        if status in {"failed", "timeout", "missing"}:
            stderr_tail = str(check.get("stderr_tail") or "").strip()
            stdout_tail = str(check.get("stdout_tail") or "").strip()
            if stderr_tail:
                lines.extend(["", "stderr tail:", stderr_tail])
            elif stdout_tail:
                lines.extend(["", "stdout tail:", stdout_tail])

    lines.extend(
        [
            "",
            "Dispatch policy:",
            "1) If component route is missing, return a routing error summary: add label comp:<component>.",
            "2) If M2 test gate failed/timeout/missing, return blocked summary with failing check context.",
            "3) If route + test gate pass, delegate to matching Team Infra worker (or spawn focused subagents).",
            "4) Keep this run internal (no external chat send in this hook turn); return concise dispatch summary only.",
        ]
    )
    return "\n".join(lines)


def build_command_payload(ticket: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticket_ref": ticket["ticket_ref"],
        "issue_id": ticket["issue_id"],
        "title": ticket["title"],
        "state": ticket["state"],
        "labels": ticket["labels"],
        "agent_label": ticket.get("agent_label"),
        "component": ticket.get("component"),
        "url": ticket.get("url") or "",
        "m2_check": ticket.get("m2_check"),
        "workspace_id": ticket.get("workspace_id"),
    }


async def publish_command_event(
    publisher: Publisher, config: DispatcherConfig, ticket: dict[str, Any]
) -> None:
    agent_label = ticket.get("agent_label")
    component = ticket.get("component")

    if agent_label:
        target_agent = agent_label
        action = "assign_ticket"
    elif component:
        target_agent = component
        action = "assign_ticket"
    else:
        target_agent = config.default_agent
        action = "examine"

    routing_key = f"command.{target_agent}.{action}"

    command_id = str(uuid.uuid4())
    command_payload = build_command_payload(ticket)

    envelope = {
        "command_id": command_id,
        "issued_by": "infra-dispatcher",
        "priority": "normal",
        "ttl_ms": 86400000,
        "idempotency_key": f"infra:{ticket['issue_id']}:{ticket.get('fingerprint', '')}",
        "command_payload": command_payload,
    }

    await publisher.publish(
        routing_key=routing_key, body=envelope, message_id=command_id
    )
    logger.info(
        "Published command.%s.%s for ticket %s",
        target_agent,
        action,
        ticket["ticket_ref"],
    )


async def run_dispatcher(config: DispatcherConfig) -> None:
    store = StateStore(config.state_path)
    consumer = EventConsumer("infra-dispatcher")
    publisher = Publisher()

    logger.info(
        "Starting infra dispatcher (ready_states=%s, ready_labels=%s, default_agent=%s)",
        config.ready_states,
        config.ready_labels,
        config.default_agent,
    )

    await publisher.start()

    async def handle_event(raw_payload: dict[str, Any]) -> None:
        body = _unwrap_plane_body(raw_payload)
        if not body:
            return

        ticket = evaluate_ready_issue(
            body,
            ready_states=config.ready_states,
            ready_labels=config.ready_labels,
            component_prefix=config.component_label_prefix,
            agent_prefix=config.agent_label_prefix,
        )
        if not ticket:
            return

        if not store.mark_if_new(ticket["issue_id"], ticket["fingerprint"]):
            logger.info("Duplicate ticket update ignored: %s", ticket["ticket_ref"])
            return

        logger.info(
            "Dispatching ticket %s (component=%s)",
            ticket["ticket_ref"],
            ticket.get("component") or "unknown",
        )

        check_result = await run_component_check(config, ticket)
        ticket["m2_check"] = check_result
        logger.info(
            "M2 check for %s: status=%s component=%s",
            ticket["ticket_ref"],
            check_result.get("status"),
            check_result.get("component") or ticket.get("component") or "unknown",
        )

        try:
            await publish_command_event(publisher, config, ticket)
        except Exception as exc:  # pragma: no cover
            logger.exception(
                "Failed publishing command for ticket %s: %s", ticket["ticket_ref"], exc
            )

    await consumer.start(handle_event, routing_keys=["webhook.plane.#"])

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await consumer.close()
        await publisher.close()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    config = DispatcherConfig.from_env()
    asyncio.run(run_dispatcher(config))


if __name__ == "__main__":
    main()
