"""bloodbank turn-enricher (issue #271).

Async enrichment lane for conversation turns. Subscribes to
`bloodbank.evt.conversation.turn.completed` on the bloodbank NATS bus, runs
each turn's reply through two extractors, and publishes the result as a
`bloodbank.conversation.turn.annotated` event so every downstream consumer
(roll-ups, hindsight, daily report, Grafana) reads typed facts off the bus
instead of re-extracting.

Per turn:

  1. Jev gate  — POST https://jev.delo.sh/typesafe/v1/systemone (the Jev
     SystemOne ingress, a LiteLLM pass-through; the OpenRouter decisions
     endpoint is retired), model jev-latest, one noul question `signal`: does
     this turn contain substantive work output? answers.signal.noul < 0.5 ->
     noise: counted, skipped. Jev failing never skips a turn (the gate is an
     optimization); Clairic still runs.

  2. Clairic   — POST https://api.cloudflare.com/client/v4/accounts/
     {CF_ACCOUNT_ID}/ai/run/@cf/cloudflare/clairic, fields action (text),
     artifacts (list of text), blocker (text). A field is accepted iff its
     p_null < 0.5. Clairic failing publishes with facts absent, never drops
     the event. CLAIRIC_ENABLED=false turns this step off explicitly (a host
     with no Cloudflare account): no CF credentials are required, the step is
     skipped, and `extractors` on the event lists only the extractors that ran.

Extractor pushback: a 429 or a transport error pauses enrichment for
Retry-After, else 60s. Events arriving during the pause are counted, not
queued. Any other non-2xx (401, 422 budget_exceeded, 5xx) means the extractor
answered without facts: the turn is still published, with signal null when Jev
was the one that failed.

Secrets come from the environment (JEV_API_KEY, and CF_API_KEY + CF_ACCOUNT_ID
when Clairic is enabled); compose wires them from .env.op (op:// refs) or, on
a host with the interim store, env-op-resolve. Never committed.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import os
import signal
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# agent-hooks owns the envelope builder; this service is a producer in the
# same contract family, so it imports the builder directly (as the issue's
# design with the PM specifies).
_SERVICES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_SERVICES_DIR, "agent-hooks"))
from core.envelope import build_envelope  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
# httpx logs every request at INFO; at bus volume that buries everything else.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("turn-enricher")

NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
SUBJECT = os.environ.get("SUBJECT_FILTER", "bloodbank.evt.conversation.turn.completed")
PUBLISH_SUBJECT = os.environ.get("PUBLISH_SUBJECT", "bloodbank.evt.conversation.turn.annotated")
ANNOTATED_TYPE = "bloodbank.conversation.turn.annotated"

JEV_API_KEY = os.environ.get("JEV_API_KEY", "")
JEV_URL = os.environ.get("JEV_URL", "https://jev.delo.sh/typesafe/v1/systemone")
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-latest")
CF_API_KEY = os.environ.get("CF_API_KEY", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_MODEL = os.environ.get("CF_MODEL", "@cf/cloudflare/clairic")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


# Explicit opt-out for hosts with no Cloudflare account. Default on: the
# extractor is part of the contract, so turning it off is a visible decision
# (startup log + `extractors` on every event), never a silent dummy key.
CLAIRIC_ENABLED = _env_flag("CLAIRIC_ENABLED", True)

SIGNAL_THRESHOLD = float(os.environ.get("SIGNAL_THRESHOLD", "0.5"))
P_NULL_THRESHOLD = float(os.environ.get("P_NULL_THRESHOLD", "0.5"))
GATE_TEXT_CHARS = int(os.environ.get("GATE_TEXT_CHARS", "4000"))
EXTRACT_TEXT_CHARS = int(os.environ.get("EXTRACT_TEXT_CHARS", "6000"))
MAX_ARTIFACTS = 4
BACKOFF_SECONDS = 60.0

EXTRACTORS = {"jev": JEV_MODEL, "clairic": CF_MODEL}

JEV_QUESTION = "Does this turn contain substantive work output (a decision, result, artifact, or blocker)?"
JEV_CRITERIA = {
    "true": "completed decision, verifiable artifact or path, test/verification result, or concrete blocker stated",
    "false": "acknowledgment, status ping, question back, empty or canned reply",
}

# Clairic field shapes: text fields take type/instructions/unit/null_threshold;
# list fields take type/items/instructions/null_threshold (learned 2026-10-20:
# 'description' is rejected with a 422).
CLAIRIC_FIELDS = {
    "action": {"type": "text", "instructions": "The single main action completed, as a 2-4 word verb phrase"},
    "artifacts": {"type": "list", "items": "text",
                  "instructions": "Named artifacts: versions, ticket ids, file paths, build numbers, model names"},
    "blocker": {"type": "text", "instructions": "The blocking issue if any, else none"},
}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def retry_after_seconds(value: str | None, now: float | None = None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(when.timestamp() - (time.time() if now is None else now), 0.0)


def jev_payload(reply_text: str) -> dict[str, Any]:
    """Body of one Jev decision call."""
    return {
        "model": JEV_MODEL,
        "questions": {
            "signal": {
                "type": "noul",
                "instructions": JEV_QUESTION,
                "criteria": JEV_CRITERIA,
            }
        },
        "state": "Agent turn reply:\n" + reply_text[:GATE_TEXT_CHARS],
    }


def clairic_payload(reply_text: str) -> dict[str, Any]:
    """Body of one Clairic extraction call."""
    return {
        "fields": CLAIRIC_FIELDS,
        "state": "Agent turn reply:\n" + reply_text[:EXTRACT_TEXT_CHARS],
    }


def clairic_url(account_id: str, model: str = CF_MODEL) -> str:
    return f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"


def jev_signal(body: Any) -> float | None:
    """answers.signal.noul as a float, or None when the response lacks it."""
    try:
        value = _dict(body).get("answers", {}).get("signal", {}).get("noul")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def clairic_facts(body: Any, p_null_threshold: float = P_NULL_THRESHOLD) -> dict[str, Any]:
    """Extract action/artifacts/blocker from a Clairic response, accepting a
    field iff its p_null < p_null_threshold. Anything missing or malformed
    comes back None / []."""
    result = _dict(_dict(body).get("result") or body)
    answers = _dict(result.get("answers") or result.get("fields") or result)
    facts: dict[str, Any] = {"action": None, "artifacts": [], "blocker": None}
    action = _dict(answers.get("action"))
    if action and action.get("p_null", 1.0) < p_null_threshold and isinstance(action.get("value"), str):
        facts["action"] = action["value"]
    artifacts = _dict(answers.get("artifacts"))
    # Lists are collections: Clairic omits p_null on them; absence means accept.
    if artifacts and artifacts.get("p_null", 0.0) < p_null_threshold and isinstance(artifacts.get("value"), list):
        facts["artifacts"] = [str(a) for a in artifacts["value"] if a][:MAX_ARTIFACTS]
    blocker = _dict(answers.get("blocker"))
    if blocker and blocker.get("p_null", 1.0) < p_null_threshold and isinstance(blocker.get("value"), str):
        value = blocker["value"].strip()
        facts["blocker"] = value or None
    return facts


def annotated_envelope(
    source: dict[str, Any],
    signal: float | None,
    facts: dict[str, Any],
    extractors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The bloodbank.conversation.turn.annotated event for one source turn:
    causationid = source event id, correlationid preserved, actor copied,
    ordering_key = source event id + '.annotated'. `extractors` names the
    extractors that actually ran (defaults to both)."""
    source_id = source.get("id") or ""
    data = _dict(source.get("data"))
    return build_envelope(
        ce_type=ANNOTATED_TYPE,
        kind="event",
        source="hermes://service/turn-enricher",
        producer="turn-enricher",
        service="turn-enricher",
        actor=source.get("actor") or {"type": "service", "agent_id": "turn-enricher"},
        correlation_id=source.get("correlationid") or source_id,
        causation_id=source_id,
        ordering_key=f"{source_id}.annotated",
        data={
            "target_event_id": source_id,
            "thread_id": data.get("thread_id"),
            "turn_id": data.get("turn_id"),
            "outcome": data.get("outcome"),
            "signal": signal,
            "action": facts.get("action"),
            "artifacts": facts.get("artifacts", [])[:MAX_ARTIFACTS],
            "blocker": facts.get("blocker"),
            "extractors": dict(EXTRACTORS if extractors is None else extractors),
            "working_directory": data.get("working_directory"),
        },
    )


# One extractor call: (status, body_json, retry_after). status 0 = transport
# error; body None on any non-2xx.
JevGate = Callable[[str], Awaitable[tuple[int, "dict[str, Any] | None", str | None]]]
ClairicExtract = Callable[[str], Awaitable[tuple[int, "dict[str, Any] | None", str | None]]]
Publisher = Callable[[str, bytes], Awaitable[None]]


def make_jev_gate(client: Any, api_key: str) -> JevGate:
    import httpx

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def gate(reply_text: str) -> tuple[int, dict[str, Any] | None, str | None]:
        try:
            resp = await client.post(JEV_URL, headers=headers, json=jev_payload(reply_text))
        except httpx.HTTPError as exc:
            log.error("jev http error: %s", exc)
            return 0, None, None
        body = resp.json() if 200 <= resp.status_code < 300 else None
        return resp.status_code, body, resp.headers.get("Retry-After")

    return gate


def make_clairic(client: Any, api_key: str, account_id: str) -> ClairicExtract:
    import httpx

    url = clairic_url(account_id)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def extract(reply_text: str) -> tuple[int, dict[str, Any] | None, str | None]:
        try:
            resp = await client.post(url, headers=headers, json=clairic_payload(reply_text))
        except httpx.HTTPError as exc:
            log.error("clairic http error: %s", exc)
            return 0, None, None
        body = resp.json() if 200 <= resp.status_code < 300 else None
        return resp.status_code, body, resp.headers.get("Retry-After")

    return extract


@dataclass
class TurnEnricher:
    gate: JevGate
    extract: ClairicExtract | None
    publish: Publisher
    clock: Callable[[], float] = time.monotonic
    stats: Counter = field(default_factory=Counter)
    paused_until: float = 0.0
    # Extractors named on every published event. None = whatever is wired:
    # Jev always, Clairic only when `extract` is set.
    extractors: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.extractors is None:
            self.extractors = dict(EXTRACTORS)
            if self.extract is None:
                self.extractors.pop("clairic", None)

    def paused(self) -> bool:
        return self.clock() < self.paused_until

    def _pushback(self, status: int, retry_after: str | None) -> None:
        wait = retry_after_seconds(retry_after)
        if wait is None:
            wait = BACKOFF_SECONDS
        self.paused_until = self.clock() + wait
        log.warning("extractor answered %s; pausing enrichment for %.0fs", status or "a transport error", wait)

    def _pushback_status(self, status: int) -> bool:
        """True when the status is pushback (429 or transport error): pause and
        skip this turn. Other non-2xx (4xx, 5xx) just means the extractor
        answered without facts."""
        if status == 429 or status == 0:
            self.stats[f"http_{status}"] += 1
            return True
        return False

    async def handle(self, envelope: dict[str, Any]) -> str:
        self.stats["received"] += 1
        data = _dict(envelope.get("data"))
        if data.get("platform") == "subagent":
            self.stats["skipped_subagent"] += 1
            return "skipped_subagent"
        reply_text = data.get("reply_text")
        if not isinstance(reply_text, str) or not reply_text.strip():
            self.stats["skipped_no_reply"] += 1
            return "skipped_no_reply"
        if self.paused():
            self.stats["skipped_paused"] += 1
            return "skipped_paused"

        # 1. Jev gate — an optimization; failure never skips the turn.
        signal_value: float | None = None
        try:
            status, body, retry_after = await self.gate(reply_text)
            if self._pushback_status(status):
                self._pushback(status, retry_after)
                self.stats["skipped_paused"] += 1
                return "skipped_paused"
            if 200 <= status < 300:
                signal_value = jev_signal(body)
                if signal_value is not None and signal_value < SIGNAL_THRESHOLD:
                    self.stats["noise"] += 1
                    log.info("noise: %s (signal=%.3f)", envelope.get("id"), signal_value)
                    return "noise"
            else:
                log.warning("jev answered %s; continuing without a signal", status)
        except Exception as exc:  # noqa: BLE001 -- gate is optional
            log.warning("jev gate failed (%s); continuing without a signal", exc)

        # 2. Clairic extract — failure publishes with facts absent. Skipped
        # outright when Clairic is explicitly disabled (no extract wired).
        facts: dict[str, Any] = {"action": None, "artifacts": [], "blocker": None}
        if self.extract is not None:
            try:
                status, body, retry_after = await self.extract(reply_text)
                if self._pushback_status(status):
                    self._pushback(status, retry_after)
                    self.stats["skipped_paused"] += 1
                    return "skipped_paused"
                if 200 <= status < 300 and body is not None:
                    facts = clairic_facts(body)
                else:
                    log.warning("clairic answered %s; publishing without facts", status)
            except Exception as exc:  # noqa: BLE001 -- facts are optional
                log.warning("clairic extract failed (%s); publishing without facts", exc)

        event = annotated_envelope(envelope, signal_value, facts, self.extractors)
        await self.publish(PUBLISH_SUBJECT, json.dumps(event, separators=(",", ":")).encode("utf-8"))
        self.stats["annotated"] += 1
        log.info(
            "annotated: %s signal=%s action=%r artifacts=%d blocker=%r",
            envelope.get("id"),
            f"{signal_value:.3f}" if signal_value is not None else "-",
            facts.get("action"), len(facts.get("artifacts") or []), facts.get("blocker"),
        )
        return "annotated"

    def stats_line(self) -> str:
        parts = [f"{k}={v}" for k, v in sorted(self.stats.items())]
        if self.paused():
            parts.append(f"paused_for={self.paused_until - self.clock():.0f}s")
        return " ".join(parts)


async def _every(seconds: float, fn: Callable[[], Awaitable[Any]], stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            try:
                await fn()
            except Exception:  # noqa: BLE001
                log.exception("periodic task failed")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


async def run() -> None:
    import httpx
    import nats
    from nats.aio.msg import Msg

    log.info("turn-enricher starting: subject=%s publish=%s jev=%s model=%s",
             SUBJECT, PUBLISH_SUBJECT, JEV_URL, JEV_MODEL)

    if not JEV_API_KEY:
        raise SystemExit(
            "JEV_API_KEY is required (op:// ref in .env.op, or the interim store via env-op-resolve). "
            "Without it every extraction would 401 silently."
        )
    if CLAIRIC_ENABLED and not (CF_API_KEY and CF_ACCOUNT_ID):
        raise SystemExit(
            "CF_API_KEY and CF_ACCOUNT_ID are required while Clairic is enabled (op://DeLoSecrets refs in .env.op). "
            "Set CLAIRIC_ENABLED=false to run Jev-only on a host with no Cloudflare account."
        )
    if not CLAIRIC_ENABLED:
        log.warning("clairic DISABLED (CLAIRIC_ENABLED=false): annotating with the Jev signal only; "
                    "events carry extractors=%s", {"jev": JEV_MODEL})

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        nc = await nats.connect(
            NATS_URL,
            name="bloodbank-turn-enricher",
            max_reconnect_attempts=-1,  # forever
            reconnect_time_wait=2,
        )
        log.info("nats connected to %s", NATS_URL)

        async def publish(subject: str, payload: bytes) -> None:
            await nc.publish(subject, payload)

        enricher = TurnEnricher(
            gate=make_jev_gate(http_client, JEV_API_KEY),
            extract=make_clairic(http_client, CF_API_KEY, CF_ACCOUNT_ID) if CLAIRIC_ENABLED else None,
            publish=publish,
        )

        async def cb(msg: Msg) -> None:
            try:
                envelope = json.loads(msg.data.decode("utf-8", errors="replace"))
            except Exception as exc:  # noqa: BLE001
                log.warning("bad json on %s: %s", msg.subject, exc)
                return
            if isinstance(envelope, dict):
                try:
                    await enricher.handle(envelope)
                except Exception:  # noqa: BLE001 -- one bad turn never kills the lane
                    log.exception("handle failed on %s", msg.subject)

        async def stats() -> None:
            log.info("stats %s", enricher.stats_line())

        sub = await nc.subscribe(SUBJECT, cb=cb)
        log.info("subscribed: %s", SUBJECT)
        periodic = [asyncio.create_task(_every(_env_float("STATS_SECONDS", 60), stats, stop))]

        try:
            await stop.wait()
        finally:
            log.info("draining subscription")
            for task in periodic:
                task.cancel()
            await sub.unsubscribe()
            await nc.drain()
            log.info("done")


if __name__ == "__main__":
    asyncio.run(run())
