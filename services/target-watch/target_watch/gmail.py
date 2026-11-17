"""`target-watch gmail-poll`: target-scoped email ingress through the gog CLI.

One run (Candybar fires it every five minutes): search the operator's Gmail for
mail from any watched target's email address, publish each message not seen
before as bloodbank.messaging.email.received, exactly per that schema. Only
target senders are read, on purpose ("I don't care about all email").
"""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from . import envelope as env
from .handles import norm_email
from .log import log
from .targets import Target

DEFAULT_ACCOUNT = "jaradd@gmail.com"
BODY_LIMIT = 8000
SEARCH_TIMEOUT_S = 90
GET_TIMEOUT_S = 60


def build_query(emails) -> str | None:
    addresses = sorted({e for e in emails if e})
    if not addresses:
        return None
    return f"from:({' OR '.join(addresses)}) newer_than:2d in:anywhere -in:sent -in:drafts"


def target_emails(targets: list[Target]) -> set[str]:
    return {address for target in targets for address in target.email}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.skip += 1
        elif tag in {"br", "p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain_text(body: str) -> str:
    if re.search(r"<(html|body|div|p|br|table)\b", body[:2000], re.IGNORECASE):
        parser = _Text()
        parser.feed(body)
        body = "".join(parser.parts)
    body = body.replace("\r\n", "\n")
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(body) > BODY_LIMIT:
        body = body[: BODY_LIMIT - 1].rstrip() + "…"
    return body


def _received_at(message: dict, headers: dict) -> str:
    internal = (message.get("message") or {}).get("internalDate")
    if isinstance(internal, str) and internal.isdigit():
        return env.rfc3339(datetime.fromtimestamp(int(internal) / 1000, UTC))
    try:
        return env.rfc3339(parsedate_to_datetime(headers.get("date", "")))
    except (TypeError, ValueError):
        return env.rfc3339()


def email_envelope(message: dict, account: str) -> dict:
    """bloodbank.messaging.email.received for one `gog gmail get -j` result."""
    headers = {k.lower(): v for k, v in (message.get("headers") or {}).items() if isinstance(v, str)}
    raw = message.get("message") or {}
    message_id, thread_id = raw.get("id"), raw.get("threadId")
    if not message_id or not thread_id:
        raise ValueError("gog message has no id/threadId")
    name, sender = parseaddr(headers.get("from", ""))
    sender = norm_email(sender)
    if not sender:
        raise ValueError("message has no sender address")
    recipients = [norm_email(a) for _, a in getaddresses([headers.get("to", "")])]
    recipient = next((r for r in recipients if r), account)
    subject = headers.get("subject", "").strip()
    body = plain_text(message.get("body") or "")
    text = f"{subject}\n\n{body}" if subject and body else (subject or body)
    sender_obj = {"handle": sender}
    if name.strip():
        sender_obj["display_name"] = name.strip()
    data = {
        "schema_version": 1,
        "direction": "inbound",
        "sender": sender_obj,
        "recipient": {"handle": recipient},
        "content": {"text": text, "format": "plain"},
        "received_at": _received_at(message, headers),
        "message_id": message_id,
        "thread_id": thread_id,
    }
    event_id = env.gmail_event_id(message_id)
    return env.build(env.EMAIL_RECEIVED, data, event_id=event_id, ordering_key=f"messaging:email:{thread_id}")


class Gog:
    def __init__(self, runner, account: str | None = None, binary: str | None = None):
        self.runner = runner
        self.account = account or os.environ.get("TARGET_WATCH_GMAIL_ACCOUNT", DEFAULT_ACCOUNT)
        self.binary = binary or os.environ.get("TARGET_WATCH_GOG", "gog")

    def _json(self, argv: list[str], timeout: float):
        code, stdout, stderr = self.runner(argv, cwd=None, timeout=timeout)
        if code != 0:
            raise RuntimeError(f"{' '.join(argv[:3])} exited {code}: {stderr.strip()[:200]}")
        try:
            return json.loads(stdout)
        except ValueError as exc:
            raise RuntimeError(f"{' '.join(argv[:3])} printed no JSON") from exc

    def search(self, query: str) -> list[dict]:
        out = self._json([self.binary, "gmail", "messages", "search", query, "-a", self.account, "-j", "--all"], SEARCH_TIMEOUT_S)
        messages = out.get("messages") if isinstance(out, dict) else out
        return [m for m in (messages or []) if isinstance(m, dict) and m.get("id")]

    def get(self, message_id: str) -> dict:
        out = self._json([self.binary, "gmail", "get", message_id, "-a", self.account, "-j"], GET_TIMEOUT_S)
        if not isinstance(out, dict):
            raise RuntimeError("gog gmail get printed an unexpected document")
        return out


async def poll(targets: list[Target], gog: Gog, state, publisher, *, validate=env.validate, dry_run: bool = False) -> dict:
    """One pass. Returns counts; 'failed' > 0 makes the run exit non-zero."""
    summary = {"query_matches": 0, "new": 0, "published": 0, "skipped": 0, "failed": 0}
    emails = target_emails(targets)
    query = build_query(emails)
    if query is None:
        summary["note"] = "no target has an email handle"
        return summary
    for hit in gog.search(query):
        summary["query_matches"] += 1
        message_id = hit["id"]
        if state.gmail_seen(message_id):
            continue
        summary["new"] += 1
        try:
            message = gog.get(message_id)
            envelope = email_envelope(message, gog.account)
        except (RuntimeError, ValueError) as exc:
            summary["failed"] += 1
            log("gmail_fetch_failed", message_id=message_id, error=str(exc)[:200])
            continue
        if envelope["data"]["sender"]["handle"] not in emails:
            # Gmail's from: is looser than an address match; never publish a non-target's mail.
            summary["skipped"] += 1
            if not dry_run:
                state.mark_gmail(message_id, None, "not_a_target")
            continue
        try:
            validate(envelope)
            await publisher.publish(envelope)
        except Exception as exc:  # noqa: BLE001 - not marked seen, so the next run retries it
            summary["failed"] += 1
            log("gmail_publish_failed", message_id=message_id, error=f"{type(exc).__name__}: {str(exc)[:200]}")
            continue
        summary["published"] += 1
        if not dry_run:
            state.mark_gmail(message_id, envelope["id"], "published")
    return summary
