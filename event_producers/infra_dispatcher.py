"""Bloodbank -> OpenClaw Team Infra dispatcher.

Consumes Plane webhook events already published to Bloodbank (routing key
`webhook.plane.*`), applies Ready-gate rules, de-duplicates updates, and
forwards qualifying tickets to OpenClaw's `/hooks/agent` endpoint so the
orchestrator can delegate to infra workers.

Environment variables:
- OPENCLAW_HOOK_TOKEN            (required)
- OPENCLAW_HOOK_URL              (default: http://127.0.0.1:18789/hooks/agent)
- OPENCLAW_HOOK_DELIVER          (default: false)
- INFRA_READY_STATES             (default: unstarted)
- INFRA_READY_LABELS             (default: ready,automation:go)
- INFRA_COMPONENT_LABEL_PREFIX   (default: comp:)
- INFRA_DISPATCH_STATE_PATH      (default: .infra_dispatch_state.json)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from event_producers.events.base import EventEnvelope
from event_producers.events.core.consumer import EventConsumer

logger = logging.getLogger(__name__)


def _split_csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return default
    out = [item.strip().lower() for item in value.split(",") if item.strip()]
    return out or default


def _normalize_token(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


@dataclass(slots=True)
class DispatcherConfig:
    hook_token: str
    hook_url: str = "http://127.0.0.1:18789/hooks/agent"
    hook_deliver: bool = False
    ready_states: tuple[str, ...] = ("unstarted",)
    ready_labels: tuple[str, ...] = ("ready", "automation-go")
    component_label_prefix: str = "comp:"
    state_path: Path = Path(".infra_dispatch_state.json")

    @classmethod
    def from_env(cls) -> "DispatcherConfig":
        token = os.getenv("OPENCLAW_HOOK_TOKEN", "").strip()
        if not token:
            raise RuntimeError("OPENCLAW_HOOK_TOKEN is required")

        hook_url = os.getenv("OPENCLAW_HOOK_URL", "http://127.0.0.1:18789/hooks/agent").strip()
        hook_deliver = os.getenv("OPENCLAW_HOOK_DELIVER", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        ready_states = tuple(_split_csv(os.getenv("INFRA_READY_STATES"), ["unstarted"]))
        ready_labels = tuple(
            _split_csv(os.getenv("INFRA_READY_LABELS"), ["ready", "automation:go"])
        )
        ready_labels = tuple(_normalize_token(x) for x in ready_labels if x)

        prefix = os.getenv("INFRA_COMPONENT_LABEL_PREFIX", "comp:").strip().lower()
        state_path = Path(os.getenv("INFRA_DISPATCH_STATE_PATH", ".infra_dispatch_state.json")).expanduser()

        return cls(
            hook_token=token,
            hook_url=hook_url,
            hook_deliver=hook_deliver,
            ready_states=ready_states,
            ready_labels=ready_labels,
            component_label_prefix=prefix,
            state_path=state_path,
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
        self.path.write_text(json.dumps(self._seen, indent=2, sort_keys=True), encoding="utf-8")

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
        candidates.extend([
            state_detail.get("name"),
            state_detail.get("slug"),
            state_detail.get("key"),
        ])

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


def _ticket_ref(issue: dict[str, Any]) -> str:
    identifier = issue.get("identifier")
    if identifier:
        return str(identifier)

    seq = issue.get("sequence_id")
    project = issue.get("project_detail") if isinstance(issue.get("project_detail"), dict) else {}
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
        "component": _extract_component(labels, component_prefix),
        "url": issue.get("url") or issue.get("issue_url") or "",
        "fingerprint": fingerprint,
        "workspace_id": str(body.get("workspace_id") or ""),
    }


def build_dispatch_message(ticket: dict[str, Any]) -> str:
    component = ticket.get("component")
    routing = component if component else "UNKNOWN"

    lines = [
        "Team Infra dispatch event from Bloodbank/Plane.",
        "",
        f"Ticket: {ticket['ticket_ref']}",
        f"Title: {ticket['title']}",
        f"State: {ticket['state']}",
        f"Labels: {', '.join(ticket['labels']) if ticket['labels'] else '(none)'}",
        f"Component route: {routing}",
        f"Plane URL: {ticket.get('url') or '(not provided)'}",
        "",
        "Dispatch policy:",
        "1) If component route is missing, return a routing error summary: add label comp:<component>.",
        "2) If route exists, delegate to matching Team Infra worker (or spawn focused subagents).",
        "3) Keep this run internal (no external chat send in this hook turn); return concise dispatch summary only.",
    ]
    return "\n".join(lines)


async def send_to_openclaw(config: DispatcherConfig, ticket: dict[str, Any]) -> None:
    payload = {
        "message": build_dispatch_message(ticket),
        "name": "TeamInfraDispatch",
        "sessionKey": f"hook:infra:{ticket['issue_id']}",
        "wakeMode": "now",
        "deliver": config.hook_deliver,
        "timeoutSeconds": 180,
    }

    headers = {
        "Authorization": f"Bearer {config.hook_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(config.hook_url, headers=headers, json=payload)
        resp.raise_for_status()


async def run_dispatcher(config: DispatcherConfig) -> None:
    store = StateStore(config.state_path)
    consumer = EventConsumer("infra-dispatcher")

    logger.info(
        "Starting infra dispatcher (ready_states=%s, ready_labels=%s)",
        config.ready_states,
        config.ready_labels,
    )

    async def handle_event(raw_payload: dict[str, Any]) -> None:
        body = _unwrap_plane_body(raw_payload)
        if not body:
            return

        ticket = evaluate_ready_issue(
            body,
            ready_states=config.ready_states,
            ready_labels=config.ready_labels,
            component_prefix=config.component_label_prefix,
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
        try:
            await send_to_openclaw(config, ticket)
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed forwarding ticket %s: %s", ticket["ticket_ref"], exc)

    await consumer.start(handle_event, routing_keys=["webhook.plane.#"])

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await consumer.close()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    config = DispatcherConfig.from_env()
    asyncio.run(run_dispatcher(config))


if __name__ == "__main__":
    main()
