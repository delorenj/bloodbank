"""bloodbank ntfy-router.

Subscribes to `bloodbank.evt.>` on the bloodbank NATS bus and turns the events
a person would want to hear about into ntfy pushes on ntfy.delo.sh.

Why direct NATS (no Dapr): the router's job is wildcard fan-in. Dapr's pub/sub
model wants per-topic subscriptions; a NATS core subscribe on `bloodbank.evt.>`
is one line, gets everything that crosses the broker, and auto-reconnects.

Why it filters (2026-09-22). The bus is not a stream of human-sized events any
more. hook-hub republishes its state as `bloodbank.agent.hook.updated` on every
hook it sees (~9/s in a busy session: 523 of 559 events in a sampled minute),
and every tool call is a `tool.requested` + `tool.completed` pair. Forwarding
all of it one POST per event exceeded the ntfy request limit two to one, so
ntfy answered ~65% of the router's posts with 429 -- and because every
publisher on this host shares that limit, it also 429'd n8n's `lifecycle`
pushes (the skip notifications nobody received). The router was
denial-of-servicing the notification server for everyone, itself included.

So every event now gets exactly one of three dispositions:

  mute    State pulses a person never wants routed (NTFY_ROUTER_MUTE_TYPES).
          Counted, never posted. Holocene/Candystore are their read side.
  digest  High-volume but worth a glance (NTFY_ROUTER_DIGEST_TYPES). Counted and
          rolled into one low-priority summary notification every DIGEST_SECONDS.
  route   Everything else: one push each, through a token bucket
          (ROUTE_RATE_PER_MIN, ROUTE_BURST). Overflow joins the digest.
          Each event type also has its own smaller bucket
          (ROUTE_PER_TYPE_PER_MIN, ROUTE_PER_TYPE_BURST), so one chatty type
          (a burst of agent.invocation.started, say) cannot spend the shared
          budget and silence everything else; its excess joins the digest.
          Loud types (NTFY_ROUTER_LOUD_TYPES, default the project deployment
          events) skip both buckets, so a deploy never queues behind a burst
          of llm.usage events and never lands in the digest.

ntfy pushback is respected: a 429 (or 5xx) pauses posting for Retry-After, or
an exponential backoff when the header is absent, and everything that arrives
during the pause, loud types included, is counted into the digest rather than
retried. Toasts are ephemeral; a route that did not go out is a count, never a
queue.

Links become buttons (2026-10-09, mobile-deploy-hub v1.1). An event whose
`data.links` holds a usable link ({rel, label, url} with an https:// or
itms-services:// URL) is published as JSON to the ntfy root, with up to three
`view` actions in the event's order (a `run` link gives way when three others
exist), a Click URL (the `page` link, else `run`, else the first https link)
and a UTF-8 title. A deployment also gets a plain title ("Tower of Lost Things
0.2.2 deployed"), one body line per device, the APK's size and the run, and a
priority by outcome. Events without links keep the header publish they always
had, byte for byte. There is deliberately no Attach header: ntfy-android will
not open an APK attachment, so "Install APK" hands the URL to the browser,
whose download installs as an update.

Every non-muted event still logs one line (`routed:`, `digested:`,
`rate-limited:`, `duplicate:`), so `docker logs bloodbank-ntfy-router` remains
the "did my event reach the broker" check. Muted types show in the per-minute
stats line instead.

`python main.py --render [APK_BYTES] < envelope.json` prints the request the
router would send for one envelope, token redacted, and sends nothing.
"""

from __future__ import annotations

import asyncio
import email.utils
import fnmatch
import json
import logging
import os
import re
import signal
import sys
import time
import unicodedata
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
# httpx logs every request at INFO; at bus volume that buried everything else.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ntfy-router")

NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
SUBJECT = os.environ.get("SUBJECT_FILTER", "bloodbank.evt.>")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.delo.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "bloodbank")
NTFY_PRIORITY = os.environ.get("NTFY_PRIORITY", "5")  # 1=min, 5=max (loud)
NTFY_TAGS = os.environ.get("NTFY_TAGS", "drop_of_blood,zap")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
MAX_BODY_CHARS = int(os.environ.get("MAX_BODY_CHARS", "400"))
SLOP_TOPIC = "slop"

DEFAULT_MUTE = "bloodbank.agent.hook.updated,bloodbank.system.hook.updated"
DEFAULT_DIGEST = "bloodbank.agent.tool.*"
# Deploys and their catch-up installs: rare, and wanted the moment they happen.
DEFAULT_LOUD = "bloodbank.project.deployment.*"

DEPLOYMENT_COMPLETED = "bloodbank.project.deployment.completed"
DEPLOYMENT_FAILED = "bloodbank.project.deployment.failed"

# data.links, as schemas/bloodbank/project/deployment.*.json define it. Any
# event type may carry links; the router treats them the same way for all.
MAX_ACTIONS = 3  # ntfy refuses a fourth
MAX_LABEL = 32
MAX_URL = 600
MAX_LINKS_READ = 12  # the schema allows 6
LINK_URL = re.compile(r'(?:https|itms-services)://[^\s"<>]+')
DEPLOY_BODY_CHARS = 1000
SIZE_TIMEOUT = 3.0  # seconds for the HEAD that sizes a deploy's APK


def _globs(raw: str | None, default: str) -> tuple[str, ...]:
    """Comma list of fnmatch globs; unset means the default, "" means none."""
    value = default if raw is None else raw
    return tuple(g.strip() for g in value.split(",") if g.strip())


@dataclass(frozen=True)
class Policy:
    mute: tuple[str, ...]
    digest: tuple[str, ...]
    # Loud types route one push each without spending the per-type cap or the
    # shared bucket. ntfy's own pushback still pauses them.
    loud: tuple[str, ...] = (DEFAULT_LOUD,)

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ[str] = os.environ) -> "Policy":
        return cls(
            mute=_globs(env.get("NTFY_ROUTER_MUTE_TYPES"), DEFAULT_MUTE),
            digest=_globs(env.get("NTFY_ROUTER_DIGEST_TYPES"), DEFAULT_DIGEST),
            loud=_globs(env.get("NTFY_ROUTER_LOUD_TYPES"), DEFAULT_LOUD),
        )

    def classify(self, event_type: str) -> str:
        if any(fnmatch.fnmatchcase(event_type, g) for g in self.mute):
            return "mute"
        if any(fnmatch.fnmatchcase(event_type, g) for g in self.digest):
            return "digest"
        return "route"

    def is_loud(self, event_type: str) -> bool:
        return any(fnmatch.fnmatchcase(event_type, g) for g in self.loud)


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


def format_route(envelope: dict[str, Any], raw_subject: str) -> tuple[str, str]:
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


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def topic_for(event_type: str) -> str:
    """review.slop events have their own topic; everything else goes to NTFY_TOPIC."""
    return SLOP_TOPIC if "review.slop" in event_type else NTFY_TOPIC


def header_text(value: str) -> str:
    """A Title header httpx can send. It encodes header values as ASCII, so one
    non-ASCII character used to raise and lose the event. Plain ASCII passes
    unchanged; accents fold (Grüvato -> Gruvato) and anything else is dropped."""
    if value.isascii() and value.isprintable():
        return value
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(folded.split()) or "bloodbank"


def headers_for(title: str, priority: str, tags: str) -> dict[str, str]:
    """The header publish every event without links has always had."""
    return {"Title": header_text(title), "Priority": priority, "Tags": tags}


# -- links -> ntfy actions ------------------------------------------------------


def usable_links(data: Any, event_type: str = "") -> list[dict[str, str]]:
    """data.links cut down to what a notification can open: a label (at most 32
    characters) and an https:// or itms-services:// URL. Anything else is
    dropped and logged. The event's order is kept."""
    raw = _dict(data).get("links")
    if not isinstance(raw, list):
        return []
    links: list[dict[str, str]] = []
    for item in raw[:MAX_LINKS_READ]:
        item = _dict(item)
        label = item.get("label")
        label = " ".join(label.split())[:MAX_LABEL].rstrip() if isinstance(label, str) else ""
        url = item.get("url")
        if not label or not isinstance(url, str) or len(url) > MAX_URL or not LINK_URL.fullmatch(url):
            log.warning(
                "dropped link on %s: label=%r url=%r (a button needs a label and an https:// or itms-services:// URL)",
                event_type, label or item.get("label"), str(url)[:120],
            )
            continue
        rel = item.get("rel")
        links.append({"rel": rel if isinstance(rel, str) else "", "label": label, "url": url})
    return links


def link_actions(links: list[dict[str, str]]) -> list[dict[str, Any]]:
    """At most three ntfy view actions, in the event's order. A `run` link gives
    way when three others exist. A tap clears the notification."""
    spare = max(MAX_ACTIONS - sum(1 for link in links if link["rel"] != "run"), 0)
    chosen: list[dict[str, str]] = []
    for link in links:
        if link["rel"] == "run":
            if spare == 0:
                continue
            spare -= 1
        chosen.append(link)
        if len(chosen) == MAX_ACTIONS:
            break
    return [{"action": "view", "label": link["label"], "url": link["url"], "clear": True} for link in chosen]


def link_click(links: list[dict[str, str]]) -> str | None:
    """What a tap on the notification itself opens: the `page` link, else the
    `run` link, else the first https link (never an itms-services one)."""
    https = [link for link in links if link["url"].startswith("https://")]
    for rel in ("page", "run"):
        for link in https:
            if link["rel"] == rel:
                return link["url"]
    return https[0]["url"] if https else None


# -- deployments ----------------------------------------------------------------


def megabytes(size: int) -> str:
    value = size / 1_000_000
    return f"{value:.0f} MB" if value >= 10 else f"{value:.1f} MB"


def _deliveries(data: dict[str, Any]) -> list[dict[str, Any]]:
    """data.deliveries, or one entry made from target and artifact for a deploy
    that names a single target."""
    items = data.get("deliveries")
    deliveries = [entry for entry in items if isinstance(entry, dict)] if isinstance(items, list) else []
    if deliveries:
        return deliveries
    target, artifact = _dict(data.get("target")), _dict(data.get("artifact"))
    code = artifact.get("version_code")
    return [{
        "target": target.get("name") or "target",
        "version": artifact.get("version"),
        "build": None if code is None else str(code),
        "verified": data.get("verified") is True,
    }]


def _device(delivery: dict[str, Any]) -> str:
    return str(delivery.get("device") or delivery.get("target") or "device")


def _status(delivery: dict[str, Any]) -> str:
    """deliveries[].status; a delivery from before the field existed is failed
    when it names a stage or reason, else installed."""
    status = delivery.get("status")
    if isinstance(status, str) and status:
        return status
    return "failed" if delivery.get("stage") or delivery.get("reason") else "installed"


def delivery_line(delivery: dict[str, Any]) -> str:
    """One body line per device: 'S26: pending (not on big-chungus's adb)',
    'iPad: installed 0.2.2 (4), verified'."""
    name, status = _device(delivery), _status(delivery)
    if status == "installed":
        line = f"{name}: installed"
        if delivery.get("version"):
            line += f" {delivery['version']}"
        if delivery.get("build"):
            line += f" ({delivery['build']})"
        by = delivery.get("by")
        line += {"catch-up": " by catch-up", "manual": " by hand"}.get(by, "") if isinstance(by, str) else ""
        return line + (", verified" if delivery.get("verified") is True else "")
    if status == "failed" and delivery.get("stage"):
        status = f"failed at {delivery['stage']}"
    reason = delivery.get("reason")
    return f"{name}: {status}" + (f" ({reason})" if reason else "")


def format_deployment(
    event_type: str, data: dict[str, Any], links: list[dict[str, str]], apk_bytes: int | None = None,
) -> tuple[str, str, int, list[str]] | None:
    """(title, body, priority, tags) for a project deployment event, None for any
    other type.

      deploy completed    "<app> <version> deployed"                5 package
      catch-up completed  "<app> <version> installed on <device>"   4 white_check_mark
      deploy failed       "<app> <version> deploy failed at <stage>"  5 rotating_light
      catch-up failed     "<app> <version> catch-up failed on <device>"  4 warning

    A completed body is one line per device, the APK's size and the run; a
    failed one is the stage, the reason and the run."""
    if event_type not in (DEPLOYMENT_COMPLETED, DEPLOYMENT_FAILED):
        return None
    project, artifact = _dict(data.get("project")), _dict(data.get("artifact"))
    name = project.get("name") or artifact.get("name") or project.get("slug") or "App"
    app = f"{name} {artifact['version']}" if artifact.get("version") else str(name)
    deliveries = _deliveries(data)
    device = _device(deliveries[0])
    catch_up = data.get("trigger") == "catch-up"
    run_url = next((link["url"] for link in links if link["rel"] == "run"), None)
    if event_type == DEPLOYMENT_COMPLETED:
        lines = [delivery_line(delivery) for delivery in deliveries]
        if apk_bytes and any(link["rel"] == "apk" for link in links):
            lines.append(f"APK {megabytes(apk_bytes)}")
        if run_url:
            lines.append(f"Run: {run_url}")
        body = "\n".join(lines)[:DEPLOY_BODY_CHARS]
        if catch_up:
            return f"{app} installed on {device}", body, 4, ["white_check_mark"]
        return f"{app} deployed", body, 5, ["package"]
    stage, reason = data.get("stage"), data.get("reason")
    lines = [f"{stage}: {reason}" if stage and reason else str(reason or stage or "failed")]
    run_url = run_url or _dict(data.get("run")).get("url")
    if run_url:
        lines.append(f"Run: {run_url}")
    body = "\n".join(lines)[:DEPLOY_BODY_CHARS]
    if catch_up:
        return f"{app} catch-up failed on {device}", body, 4, ["warning"]
    return (f"{app} deploy failed at {stage}" if stage else f"{app} deploy failed"), body, 5, ["rotating_light"]


# -- what one routed event becomes ----------------------------------------------

_PRIORITY_NAMES = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}


def priority_number(value: str) -> int:
    """ntfy's JSON publish takes the priority as a number; headers also take names."""
    text = str(value).strip().lower()
    if text.isdigit() and 1 <= int(text) <= 5:
        return int(text)
    return _PRIORITY_NAMES.get(text, 3)


def render_links(
    envelope: dict[str, Any], subject: str, links: list[dict[str, str]], *,
    priority: str = NTFY_PRIORITY, tags: str = NTFY_TAGS, apk_bytes: int | None = None,
) -> dict[str, Any]:
    """The JSON object published to the ntfy root for an event with usable links:
    {topic, title, message, priority, tags, click, actions}. Never `attach`."""
    event_type = envelope.get("type") or subject or "unknown"
    deployment = format_deployment(event_type, _dict(envelope.get("data")), links, apk_bytes)
    if deployment is None:
        title, body = format_route(envelope, subject)
        number, tag_list = priority_number(priority), [tag.strip() for tag in tags.split(",") if tag.strip()]
    else:
        title, body, number, tag_list = deployment
    message: dict[str, Any] = {
        "topic": topic_for(event_type),
        "title": " ".join(str(title).split()),
        "message": body,
        "priority": number,
        "tags": tag_list,
    }
    click = link_click(links)
    if click:
        message["click"] = click
    message["actions"] = link_actions(links)
    return message


def publication(
    envelope: dict[str, Any], subject: str, links: list[dict[str, str]], *,
    priority: str, tags: str, apk_bytes: int | None = None,
) -> tuple[str, str, dict[str, str], dict[str, Any] | None]:
    """(title, body, headers, message) for one routed event. Without usable links
    it is the header publish to the event's topic it always was (message None);
    with links it is a JSON publish to the ntfy root, so the title may be UTF-8
    and the actions need no header escaping."""
    if not links:
        title, body = format_route(envelope, subject)
        return title, body, headers_for(title, priority, tags), None
    message = render_links(envelope, subject, links, priority=priority, tags=tags, apk_bytes=apk_bytes)
    return message["title"], message["message"], {}, message


def ntfy_request(
    event_type: str, body: str, headers: dict[str, str], message: dict[str, Any] | None = None,
) -> tuple[str, dict[str, str], bytes]:
    """URL, headers and body of one publish, before the Authorization header."""
    if message is None:
        return f"{NTFY_URL}/{topic_for(event_type)}", headers, body.encode("utf-8")
    content = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{NTFY_URL}/", {"Content-Type": "application/json"}, content


# One POST to ntfy: (event_type, title, body, headers, message). message None is a
# header publish to the event's topic; a dict is the JSON object published to the
# ntfy root. Returns (status, Retry-After); status 0 means a transport error.
Poster = Callable[[str, str, str, dict[str, str], "dict[str, Any] | None"], Awaitable[tuple[int, str | None]]]
Sizer = Callable[[str], Awaitable["int | None"]]


def make_poster(client: Any, token: str) -> Poster:
    import httpx

    auth = {"Authorization": f"Bearer {token}"}

    async def post(
        event_type: str, title: str, body: str, headers: dict[str, str], message: dict[str, Any] | None = None,
    ) -> tuple[int, str | None]:
        url, request_headers, content = ntfy_request(event_type, body, headers, message)
        try:
            resp = await client.post(url, headers={**request_headers, **auth}, content=content)
        except httpx.HTTPError as exc:
            log.error("ntfy http error: %s", exc)
            return 0, None
        return resp.status_code, resp.headers.get("Retry-After")

    return post


def make_sizer(client: Any, timeout: float = SIZE_TIMEOUT) -> Sizer:
    """The APK's size for a deploy's body: a HEAD on its public link, because the
    event carries no size. No credentials go with it; any failure means the body
    simply has no size line."""
    import httpx

    async def size(url: str) -> int | None:
        try:
            resp = await client.head(url, timeout=timeout)
        except httpx.HTTPError as exc:
            log.warning("apk size lookup failed: %s", exc)
            return None
        length = resp.headers.get("Content-Length", "")
        if 200 <= resp.status_code < 300 and length.isdigit() and int(length) > 0:
            return int(length)
        return None

    return size


@dataclass
class NtfyRouter:
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
    # Per-event-type cap in front of the shared bucket; 0 turns it off.
    per_type_rate_per_min: float = 0.0
    per_type_burst: int = 3
    max_tracked_types: int = 256
    # Sizes a deploy's APK for its body; None leaves the size line out.
    sizer: Sizer | None = None
    max_remembered_installs: int = 512

    digested: Counter = field(default_factory=Counter)
    overflow: Counter = field(default_factory=Counter)
    muted: Counter = field(default_factory=Counter)
    stats: Counter = field(default_factory=Counter)
    paused_until: float = 0.0
    backoff: float = 0.0
    type_buckets: dict[str, TokenBucket] = field(default_factory=dict)
    # (app, version, run id, device model) of every install already announced.
    installs: OrderedDict = field(default_factory=OrderedDict)

    # -- ntfy pushback --------------------------------------------------------

    def paused(self) -> bool:
        return self.clock() < self.paused_until

    def _pushback(self, status: int, retry_after: str | None) -> None:
        wait = retry_after_seconds(retry_after)
        if wait is None:
            self.backoff = min(max(self.backoff * 2, self.backoff_min), self.backoff_max)
            wait = self.backoff
        self.paused_until = self.clock() + wait
        log.warning("ntfy answered %s; pausing routes for %.0fs", status or "a transport error", wait)

    async def _send(
        self, event_type: str, title: str, body: str, headers: dict[str, str], message: dict[str, Any] | None = None,
    ) -> bool:
        status, retry_after = await self.post(event_type, title, body, headers, message)
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

    def _type_allows(self, event_type: str) -> bool:
        if self.per_type_rate_per_min <= 0:
            return True
        bucket = self.type_buckets.get(event_type)
        if bucket is None:
            if len(self.type_buckets) >= self.max_tracked_types:
                # Evict the oldest; a bucket untouched that long is full anyway.
                self.type_buckets.pop(next(iter(self.type_buckets)))
            bucket = TokenBucket(self.per_type_rate_per_min / 60.0, self.per_type_burst, clock=self.clock)
            self.type_buckets[event_type] = bucket
        return bucket.take()

    # -- deployments ----------------------------------------------------------

    def _install_keys(self, data: dict[str, Any]) -> list[tuple[str, str, str, str]]:
        """(app, version, run id, device model) for each device a deployment
        event reports installed."""
        slug = _dict(data.get("project")).get("slug")
        run_id = _dict(data.get("run")).get("id")
        version = _dict(data.get("artifact")).get("version")
        if not (slug and run_id):
            return []
        return [
            (str(slug), str(delivery.get("version") or version), str(run_id), str(delivery["target"]))
            for delivery in _deliveries(data)
            if _status(delivery) == "installed" and delivery.get("target")
        ]

    def _already_announced(self, event_type: str, data: dict[str, Any]) -> bool:
        """A catch-up install of a device this router already announced installed
        in the same run: by the deploy itself, or by an earlier copy of the
        catch-up event. The catch-up units should never send one; this keeps a
        retried event from buzzing the phone twice."""
        if event_type != DEPLOYMENT_COMPLETED or data.get("trigger") != "catch-up":
            return False
        keys = self._install_keys(data)
        return bool(keys) and all(key in self.installs for key in keys)

    def _remember_installs(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type != DEPLOYMENT_COMPLETED:
            return
        for key in self._install_keys(data):
            self.installs[key] = True
            self.installs.move_to_end(key)
        while len(self.installs) > self.max_remembered_installs:
            self.installs.popitem(last=False)

    async def _apk_bytes(self, event_type: str, links: list[dict[str, str]]) -> int | None:
        if self.sizer is None or event_type != DEPLOYMENT_COMPLETED:
            return None
        url = next((link["url"] for link in links if link["rel"] == "apk" and link["url"].startswith("https://")), None)
        if url is None:
            return None
        try:
            return await self.sizer(url)
        except Exception as exc:  # noqa: BLE001 -- a size line is never worth a lost route
            log.warning("apk size lookup failed: %s", exc)
            return None

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
        data = _dict(envelope.get("data"))
        if self._already_announced(event_type, data):
            self.stats["duplicate"] += 1
            log.info("duplicate: %s", event_type)
            return "duplicate"
        loud = self.policy.is_loud(event_type)
        if self.paused() or not (loud or (self._type_allows(event_type) and self.bucket.take())):
            self.overflow[event_type] += 1
            self.stats["rate_limited"] += 1
            log.info("rate-limited: %s", event_type)
            return "rate-limited"
        links = usable_links(data, event_type)
        apk_bytes = await self._apk_bytes(event_type, links)
        title, body, headers, message = publication(
            envelope, subject, links, priority=self.priority, tags=self.tags, apk_bytes=apk_bytes,
        )
        if await self._send(event_type, title, body, headers, message):
            self._remember_installs(event_type, data)
            self.stats["routed"] += 1
            log.info("routed: %s", event_type)
            if message is not None:
                log.info("buttons: %s", " | ".join(action["label"] for action in message["actions"]))
            return "routed"
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
            lines.append("not routed individually (rate limit / ntfy pushback):")
            lines += [f"  {_short(t)} x{n}" for t, n in self.overflow.most_common(8)]
        if self.muted:
            muted = ", ".join(f"{_short(t)} x{n}" for t, n in self.muted.most_common(3))
            lines.append(f"muted: {muted}")
        return f"bloodbank digest: {total} events in {minutes}m", "\n".join(lines)

    async def flush_digest(self) -> bool:
        text = self.digest_text()
        if text is None:
            self.muted.clear()  # muted counts alone never earn a route
            return False
        if self.paused():
            return False  # counts carry over into the next digest
        title, body = text
        if await self._send("digest", title, body, headers_for(title, self.digest_priority, self.digest_tags)):
            self.digested.clear()
            self.overflow.clear()
            self.muted.clear()
            self.stats["digests"] += 1
            log.info("routed: %s", title)
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


def preview(envelope: dict[str, Any], subject: str = "", apk_bytes: int | None = None) -> dict[str, Any]:
    """The request the router would send for one envelope, token redacted.
    Nothing is posted, no limit is spent and no size is looked up (pass the
    APK's bytes to see the size line)."""
    event_type = envelope.get("type") or subject or "unknown"
    links = usable_links(envelope.get("data"), event_type)
    _, body, headers, message = publication(
        envelope, subject, links, priority=NTFY_PRIORITY, tags=NTFY_TAGS, apk_bytes=apk_bytes,
    )
    url, request_headers, content = ntfy_request(event_type, body, headers, message)
    return {
        "method": "POST",
        "url": url,
        "headers": {**request_headers, "Authorization": "Bearer <NTFY_TOKEN>"},
        "body": message if message is not None else content.decode("utf-8"),
    }


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
        "ntfy-router starting: subject=%s topic=%s mute=%s digest=%s loud=%s",
        SUBJECT, NTFY_TOPIC, ",".join(policy.mute) or "-", ",".join(policy.digest) or "-",
        ",".join(policy.loud) or "-",
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # ntfy runs auth-default-access=deny-all, so an absent token is not a
    # degraded mode -- it is a silent total outage. The compose file supplies
    # this as ${BLOODBANK_NTFY_ROUTER_NTFY_TOKEN:-}, which resolves to the empty
    # string in any shell that does not export it. On 2026-08-28 a redeploy
    # from a shell without the variable produced 346 consecutive 403s over ~4
    # minutes with nothing in the logs to say why. Refuse to start instead.
    if not NTFY_TOKEN:
        raise SystemExit(
            "NTFY_TOKEN is empty. ntfy denies unauthenticated publishes, so every "
            "route would 403 silently. Supply BLOODBANK_NTFY_ROUTER_NTFY_TOKEN "
            "(op://DeLoSecrets/ntfy Access Token/credential) before starting."
        )

    async with httpx.AsyncClient(timeout=5.0) as http_client:
        router = NtfyRouter(
            policy=policy,
            post=make_poster(http_client, NTFY_TOKEN),
            bucket=TokenBucket(
                _env_float("ROUTE_RATE_PER_MIN", 20) / 60.0,
                int(_env_float("ROUTE_BURST", 10)),
            ),
            digest_seconds=_env_float("DIGEST_SECONDS", 300),
            per_type_rate_per_min=_env_float("ROUTE_PER_TYPE_PER_MIN", 6),
            per_type_burst=int(_env_float("ROUTE_PER_TYPE_BURST", 3)),
            sizer=make_sizer(http_client),
        )

        nc = await nats.connect(
            NATS_URL,
            name="bloodbank-ntfy-router",
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
                await router.handle(envelope, msg.subject)

        async def stats() -> None:
            log.info("stats %s", router.stats_line())

        sub = await nc.subscribe(SUBJECT, cb=cb)
        log.info("subscribed: %s", SUBJECT)
        periodic = [
            asyncio.create_task(_every(router.digest_seconds, router.flush_digest, stop)),
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
    if sys.argv[1:2] == ["--render"]:
        # python main.py --render [APK_BYTES] < envelope.json
        size = int(sys.argv[2]) if len(sys.argv) > 2 else None
        print(json.dumps(preview(json.load(sys.stdin), apk_bytes=size), indent=2, ensure_ascii=False))
    else:
        asyncio.run(run())
