"""Bloodbank task triage dispatcher.

Consumes `task.inbox.issue.created` events and forwards a deterministic TASK_EVENT
message to OpenClaw hooks so Cack can run 33god-task-triage automatically.

Environment variables:
- OPENCLAW_HOOK_TOKEN            (required)
- OPENCLAW_HOOK_URL              (default: http://127.0.0.1:18790/hooks/agent)
- OPENCLAW_HOOK_DELIVER          (default: false)
- TASK_TRIAGE_STATE_PATH         (default: .task_triage_dispatch_state.json)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from event_producers.events.base import EventEnvelope
from event_producers.events.core.consumer import EventConsumer

logger = logging.getLogger(__name__)


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._seen = {str(x) for x in data if str(x).strip()}
            elif isinstance(data, dict):
                self._seen = {str(x) for x in data.get("seenIssueIds", []) if str(x).strip()}
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed loading state file %s: %s", self.path, exc)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"seenIssueIds": sorted(self._seen)}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def mark_if_new(self, issue_id: str) -> bool:
        if issue_id in self._seen:
            return False
        self._seen.add(issue_id)
        self._save()
        return True


def _load_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _build_task_event(issue: dict[str, Any]) -> str:
    return "\n".join(
        [
            "TASK_EVENT",
            "event_type: task.inbox.issue.created",
            "source: plane.intake",
            "workspace: 33god",
            f"project_id: {issue.get('project_id', '')}",
            f"issue_id: {issue.get('issue_id', '')}",
            f"issue_ref: {issue.get('issue_ref', '')}",
            f"title: {issue.get('title', '')}",
            f"url: {issue.get('url', '')}",
            f"created_at: {issue.get('created_at', '')}",
            f"state: {issue.get('state', '')}",
        ]
    )


async def _send_to_openclaw(
    *,
    hook_url: str,
    hook_token: str,
    hook_deliver: bool,
    hook_model: str | None,
    issue: dict[str, Any],
) -> None:
    payload = {
        "name": "TaskInboxIssueCreated",
        "sessionKey": f"hook:task-inbox:{issue.get('issue_id', 'unknown')}",
        "wakeMode": "now",
        "deliver": hook_deliver,
        "timeoutSeconds": 180,
        "message": _build_task_event(issue),
    }
    if hook_model:
        payload["model"] = hook_model

    headers = {
        "Authorization": f"Bearer {hook_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(hook_url, headers=headers, json=payload)
        resp.raise_for_status()


def _unwrap_issue(raw_payload: dict[str, Any]) -> dict[str, Any] | None:
    payload = raw_payload
    try:
        envelope = EventEnvelope.model_validate(raw_payload)
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
    except Exception:
        pass

    if not isinstance(payload, dict):
        return None

    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return None

    issue_id = str(issue.get("issue_id") or "").strip()
    if not issue_id:
        return None

    return issue


async def run_dispatcher() -> None:
    hook_token = _load_required_env("OPENCLAW_HOOK_TOKEN")
    hook_url = os.getenv("OPENCLAW_HOOK_URL", "http://127.0.0.1:18790/hooks/agent").strip()
    hook_deliver = os.getenv("OPENCLAW_HOOK_DELIVER", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    hook_model = os.getenv("TASK_TRIAGE_HOOK_MODEL", "openai-codex/gpt-5.3-codex").strip() or None
    state_path = Path(os.getenv("TASK_TRIAGE_STATE_PATH", ".task_triage_dispatch_state.json")).expanduser()

    store = StateStore(state_path)
    consumer = EventConsumer("task-triage-dispatcher")

    logger.info("Starting task triage dispatcher")

    async def handle_event(raw_payload: dict[str, Any]) -> None:
        issue = _unwrap_issue(raw_payload)
        if not issue:
            return

        issue_id = str(issue.get("issue_id") or "").strip()
        if not store.mark_if_new(issue_id):
            logger.info("Duplicate intake issue ignored: %s", issue_id)
            return

        logger.info("Forwarding intake issue to OpenClaw: %s", issue.get("issue_ref") or issue_id)
        try:
            await _send_to_openclaw(
                hook_url=hook_url,
                hook_token=hook_token,
                hook_deliver=hook_deliver,
                hook_model=hook_model,
                issue=issue,
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed forwarding intake issue %s: %s", issue_id, exc)

    await consumer.start(handle_event, routing_keys=["task.inbox.issue.created"])

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
    asyncio.run(run_dispatcher())


if __name__ == "__main__":
    main()
