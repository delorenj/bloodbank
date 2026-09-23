"""bloodbank event-toaster.

Subscribes to `bloodbank.evt.>` on the bloodbank NATS bus and turns the events
a person would want to hear about into ntfy toasts on ntfy.delo.sh.

Why direct NATS (no Dapr): the toaster's job is wildcard fan-in. Dapr's pub/sub
model wants per-topic subscriptions; a NATS core subscribe on `bloodbank.evt.>`
is one line, gets everything that crosses the broker, and auto-reconnects.

Why it filters (2026-09-22). The bus is not a stream of human-sized events any
more. hook-hub republishes its state as `bloodbank.agent.hook.updated` on every
hook it sees (~9/s in a busy session: 523 of 559 events in a sampled minute),
and every tool call is a `tool.requested` + `tool.completed` pair. Forwarding
all of it one POST per event exceeded the ntfy request limit two to one, so
ntfy answered ~65% of the toaster's posts with 429 -- and because every
publisher on this host shares that limit, it also 429'd n8n's `lifecycle`
pushes (the skip notifications nobody received). The toaster was
denial-of-servicing the notification server for everyone, itself included.

So every event now gets exactly one of three dispositions:

  mute    State pulses a person never wants toasted (TOASTER_MUTE_TYPES).
          Counted, never posted. Holocene/Candystore are their read side.
  digest  High-volume but worth a glance (TOASTER_DIGEST_TYPES). Counted and
          rolled into one low-priority summary toast every DIGEST_SECONDS.
  toast   Everything else: one toast each, through a token bucket
          (TOAST_RATE_PER_MIN, TOAST_BURST). Overflow joins the digest.

ntfy pushback is respected: a 429 (or 5xx) pauses posting for Retry-After, or
an exponential backoff when the header is absent, and everything that arrives
during the pause is counted into the digest rather than retried. Toasts are
ephemeral; a toast that did not go out is a count, never a queue.

Every non-muted event still logs one line (`toasted:`, `digested:`,
`rate-limited:`), so `docker logs bloodbank-event-toaster` remains the
"did my event reach the broker" check. Muted types show in the per-minute
stats line instead.
"""

from __future__ import annotations

import asyncio
import email.utils
import fnmatch
import json
import logging
import os
import signal
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
# httpx logs every request at INFO; at bus volume that buried everything else.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("event-toaster")

NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
SUBJECT = os.environ.get("SUBJECT_FILTER", "bloodbank.evt.>")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.delo.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "bloodbank")
NTFY_PRIORITY = os.environ.get("NTFY_PRIORITY", "5")  # 1=min, 5=max (loud)
NTFY_TAGS = os.environ.get("NTFY_TAGS", "drop_of_blood,zap")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
MAX_BODY_CHARS = int(os.environ.get("MAX_BODY_CHARS", "400"))

DEFAULT_MUTE = "bloodbank.agent.hook.updated,bloodbank.system.hook.updated"
DEFAULT_DIGEST = "bloodbank.agent.tool.*"


def _globs(raw: str | None, default: str) -> tuple[str, ...]:
    """Comma list of fnmatch globs; unset means the default, "" means none."""
    value = default if raw is None else raw
    return tuple(g.strip() for g in value.split(",") if g.strip())


@dataclass(frozen=True)
class Policy:
    mute: tuple[str, ...]
    digest: tuple[str, ...]

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ[str] = os.environ) -> "Policy":
        return cls(
            mute=_globs(env.get("TOASTER_MUTE_TYPES"), DEFAULT_MUTE),
            digest=_globs(env.get("TOASTER_DIGEST_TYPES"), DEFAULT_DIGEST),
        )

    def classify(self, event_type: str) -> str:
        if any(fnmatch.fnmatchcase(event_type, g) for g in self.mute):
            return "mute"
        if any(fnmatch.fnmatchcase(event_type, g) for g in self.digest):
            return "digest"
        return "toast"


class TokenBucket:
    """Classic token bucket: `burst` tokens, refilled at `rate_per_sec`."""

    def __init__(self, rate_per_sec: float, burst: int, clock: Callable[[], float] = time.monotonic):
        self.rate = max(rate_per_sec, 0.0)
        self.capacity = max(float(burst), 1.0)
        self.tokens = self.capacity
        self.clock = clock
        self.stamp = clock()

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


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


def format_toast(envelope: dict[str, Any], raw_subject: str) -> tuple[str, str]:
    """Return (title, body) for one envelope."""
    event_type = envelope.get("type") or raw_subject or "unknown"
    source = envelope.get("source") or "unknown"
    data = envelope.get("data") or {}

    # Title: ASCII only (HTTP headers can't carry raw UTF-8 in httpx; emoji
    # comes from the Tags header instead, rendered client-side by ntfy).
    title = event_type

    # Body: surface the most useful 1-2 fields from `data`, fall back to a
    # truncated JSON dump.
    line: str
    if isinstance(data, dict):
        for key in ("tool_name", "command", "prompt", "summary", "message", "reason"):
            if key in data and data[key]:
                line = f"{key}: {str(data[key])[:200]}"
                break
        else:
            line = json.dumps(data, default=str)[:MAX_BODY_CHARS]
    else:
        line = str(data)[:MAX_BODY_CHARS]

    return title, f"src: {source}\n{line}"


def _short(event_type: str) -> str:
    return event_type.removeprefix("bloodbank.")


# (status, retry_after_header) for one POST; status 0 means a transport error.
Poster = Callable[[str, str, dict[str, str]], Awaitable[tuple[int, str | None]]]


@dataclass
class Toaster:
    policy: Policy
    post: Poster
    bucket: TokenBucket
    clock: Callable[[], float] = time.monotonic
    priority: str = NTFY_PRIORITY
    tags: str = NTFY_TAGS
    digest_priority: str = "2"
    digest_tags: str = "drop_of_blood,bar_chart"
    digest_seconds: float = 300.0
    backoff_min: float = 10.0
    backoff_max: float = 300.0

    digested: Counter = field(default_factory=Counter)
    overflow: Counter = field(default_factory=Counter)
    muted: Counter = field(default_factory=Counter)
    stats: Counter = field(default_factory=Counter)
    paused_until: float = 0.0
    backoff: float = 0.0

    # -- ntfy pushback --------------------------------------------------------

    def paused(self) -> bool:
        return self.clock() < self.paused_until

    def _pushback(self, status: int, retry_after: str | None) -> None:
        wait = retry_after_seconds(retry_after)
        if wait is None:
            self.backoff = min(max(self.backoff * 2, self.backoff_min), self.backoff_max)
            wait = self.backoff
        self.paused_until = self.clock() + wait
        log.warning("ntfy answered %s; pausing toasts for %.0fs", status or "a transport error", wait)

    async def _send(self, title: str, body: str, priority: str, tags: str) -> bool:
        headers = {"Title": title, "Priority": priority, "Tags": tags}
        status, retry_after = await self.post(title, body, headers)
        if 200 <= status < 300:
            self.backoff = 0.0
            self.stats["posted"] += 1
            return True
        self.stats[f"http_{status}"] += 1
        if status == 429 or status >= 500 or status == 0:
            self._pushback(status, retry_after)
        else:
            log.warning("ntfy answered %s for %s", status, title)
        return False

    # -- per event ------------------------------------------------------------

    async def handle(self, envelope: dict[str, Any], subject: str) -> str:
        event_type = envelope.get("type") or subject or "unknown"
        self.stats["received"] += 1
        disposition = self.policy.classify(event_type)
        if disposition == "mute":
            self.muted[event_type] += 1
            self.stats["muted"] += 1
            return "muted"
        if disposition == "digest":
            self.digested[event_type] += 1
            self.stats["digested"] += 1
            log.info("digested: %s", event_type)
            return "digested"
        if self.paused() or not self.bucket.take():
            self.overflow[event_type] += 1
            self.stats["rate_limited"] += 1
            log.info("rate-limited: %s", event_type)
            return "rate-limited"
        title, body = format_toast(envelope, subject)
        if await self._send(title, body, self.priority, self.tags):
            self.stats["toasted"] += 1
            log.info("toasted: %s", event_type)
            return "toasted"
        self.overflow[event_type] += 1
        return "failed"

    # -- periodic -------------------------------------------------------------

    def digest_text(self) -> tuple[str, str] | None:
        total = sum(self.digested.values()) + sum(self.overflow.values())
        if not total:
            return None
        minutes = max(round(self.digest_seconds / 60), 1)
        lines = [f"{_short(t)} x{n}" for t, n in self.digested.most_common(8)]
        if self.overflow:
            lines.append("not toasted individually (rate limit / ntfy pushback):")
            lines += [f"  {_short(t)} x{n}" for t, n in self.overflow.most_common(8)]
        if self.muted:
            muted = ", ".join(f"{_short(t)} x{n}" for t, n in self.muted.most_common(3))
            lines.append(f"muted: {muted}")
        return f"bloodbank digest: {total} events in {minutes}m", "\n".join(lines)

    async def flush_digest(self) -> bool:
        text = self.digest_text()
        if text is None:
            self.muted.clear()  # muted counts alone never earn a toast
            return False
        if self.paused():
            return False  # counts carry over into the next digest
        title, body = text
        if await self._send(title, body, self.digest_priority, self.digest_tags):
            self.digested.clear()
            self.overflow.clear()
            self.muted.clear()
            self.stats["digests"] += 1
            log.info("toasted: %s", title)
            return True
        return False  # counts carry over into the next digest

    def stats_line(self) -> str:
        """Cumulative counters since start, plus what the next digest holds."""
        parts = [f"{k}={v}" for k, v in sorted(self.stats.items())]
        parts.append(f"digest_pending={sum(self.digested.values())}")
        parts.append(f"overflow_pending={sum(self.overflow.values())}")
        if self.paused():
            parts.append(f"paused_for={self.paused_until - self.clock():.0f}s")
        return " ".join(parts)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


async def _every(seconds: float, fn: Callable[[], Awaitable[Any]], stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            try:
                await fn()
            except Exception:  # noqa: BLE001
                log.exception("periodic task failed")


async def run() -> None:
    import httpx
    import nats
    from nats.aio.msg import Msg

    policy = Policy.from_env()
    log.info(
        "event-toaster starting: subject=%s topic=%s mute=%s digest=%s",
        SUBJECT, NTFY_TOPIC, ",".join(policy.mute) or "-", ",".join(policy.digest) or "-",
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # ntfy runs auth-default-access=deny-all, so an absent token is not a
    # degraded mode -- it is a silent total outage. The compose file supplies
    # this as ${BLOODBANK_TOASTER_NTFY_TOKEN:-}, which resolves to the empty
    # string in any shell that does not export it. On 2026-08-28 a redeploy
    # from a shell without the variable produced 346 consecutive 403s over ~4
    # minutes with nothing in the logs to say why. Refuse to start instead.
    if not NTFY_TOKEN:
        raise SystemExit(
            "NTFY_TOKEN is empty. ntfy denies unauthenticated publishes, so every "
            "toast would 403 silently. Supply BLOODBANK_TOASTER_NTFY_TOKEN "
            "(op://DeLoSecrets/ntfy Access Token/credential) before starting."
        )

    url = f"{NTFY_URL}/{NTFY_TOPIC}"
    auth = {"Authorization": f"Bearer {NTFY_TOKEN}"}

    async with httpx.AsyncClient(timeout=5.0) as http_client:

        async def post(title: str, body: str, headers: dict[str, str]) -> tuple[int, str | None]:
            try:
                resp = await http_client.post(url, headers={**headers, **auth}, content=body.encode("utf-8"))
            except httpx.HTTPError as exc:
                log.error("ntfy http error: %s", exc)
                return 0, None
            return resp.status_code, resp.headers.get("Retry-After")

        toaster = Toaster(
            policy=policy,
            post=post,
            bucket=TokenBucket(
                _env_float("TOAST_RATE_PER_MIN", 20) / 60.0,
                int(_env_float("TOAST_BURST", 10)),
            ),
            digest_seconds=_env_float("DIGEST_SECONDS", 300),
        )

        nc = await nats.connect(
            NATS_URL,
            name="bloodbank-event-toaster",
            max_reconnect_attempts=-1,  # forever
            reconnect_time_wait=2,
        )
        log.info("nats connected to %s", NATS_URL)

        async def cb(msg: Msg) -> None:
            try:
                envelope = json.loads(msg.data.decode("utf-8", errors="replace"))
            except Exception as exc:  # noqa: BLE001
                log.warning("bad json on %s: %s", msg.subject, exc)
                return
            if isinstance(envelope, dict):
                await toaster.handle(envelope, msg.subject)

        async def stats() -> None:
            log.info("stats %s", toaster.stats_line())

        sub = await nc.subscribe(SUBJECT, cb=cb)
        log.info("subscribed: %s", SUBJECT)
        periodic = [
            asyncio.create_task(_every(toaster.digest_seconds, toaster.flush_digest, stop)),
            asyncio.create_task(_every(_env_float("STATS_SECONDS", 60), stats, stop)),
        ]

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
