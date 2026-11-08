"""Exact BB-34 notification family. No broker replay or phone-delivery claims."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
from urllib.parse import urlsplit
import uuid

IMPLEMENTATION = "bloodbank.game.implementation.started"
PACK = "bloodbank.game.pack.released"
LOW_WATER = "bloodbank.game.concept_pool.breached"
ITEM = "bloodbank.game.item.created"
APP_LAUNCH = "bloodbank.project.app_launch.recorded"
TYPES = frozenset({IMPLEMENTATION, PACK, LOW_WATER, ITEM, APP_LAUNCH})
SUBJECTS = frozenset(t.replace("bloodbank.", "bloodbank.evt.", 1) for t in TYPES)


def matches(event_type, subject=""):
    return ((isinstance(event_type, str) and event_type in TYPES)
            or (isinstance(subject, str) and subject in SUBJECTS))


def suppression_reason(envelope):
    """Notification eligibility is narrower than canonical event validity."""
    if envelope["type"] == LOW_WATER:
        return "lowwater-owner-episode-gate-unavailable"
    if envelope["type"] == APP_LAUNCH:
        data = envelope["data"]
        if not (data["channel"] == "testflight" and data["testflight_eligible"] is True):
            return "app-launch-not-testflight-eligible"
    return ""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def text(value, limit=200):
    """Untrusted display fields never become headers or escaped line breaks."""
    value = str(value).replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")
    value = "".join(" " if unicodedata.category(c)[0] == "C" else c for c in value)
    return " ".join(value.split())[:limit]


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    cutover_accepted: bool = False
    topic: str = "tolt"
    base_url: str = "https://ntfy.delo.sh"
    state_path: Path = Path("/var/lib/ntfy-router/tolt.sqlite3")
    validator_root: Path = Path("/opt/bloodbank")
    link_hosts: tuple[str, ...] = ("tolt.delo.sh",)
    max_attempts: int = 5
    retry_base: float = 10.0
    lease_seconds: float = 30.0
    post_timeout: float = 8.0
    drain_limit: int = 10

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.cutover_accepted) is not bool:
            raise ValueError("family cutover gates must be booleans")
        url = urlsplit(self.base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in ("", "/") or url.query or "#" in self.base_url
                or url.port not in (None, 443)):
            raise ValueError("family ntfy endpoint must be an HTTPS root")
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.topic)
                or self.topic in {"deploys", "game", "project", "bloodbank", "slop"}):
            raise ValueError("family requires a dedicated configured topic")
        if not (1 <= self.max_attempts <= 20 and 1 <= self.drain_limit <= 100
                and 0 < self.post_timeout < self.lease_seconds <= 300
                and 0 < self.retry_base <= 3600):
            raise ValueError("invalid bounded family retry configuration")

    @property
    def authorized(self):
        return self.enabled and self.cutover_accepted

    @property
    def destination(self):
        return self.base_url.rstrip("/") + "/" + self.topic

    @classmethod
    def from_env(cls, env=os.environ):
        return cls(
            enabled=env.get("NTFY_TOLT_ENABLED", "false").lower() == "true",
            cutover_accepted=env.get("NTFY_TOLT_CUTOVER_ACCEPTED", "false").lower() == "true",
            topic=env.get("NTFY_TOLT_TOPIC", "tolt"),
            base_url=env.get("NTFY_URL", "https://ntfy.delo.sh").rstrip("/"),
            state_path=Path(env.get("NTFY_TOLT_STATE", "/var/lib/ntfy-router/tolt.sqlite3")),
            validator_root=Path(env.get("NTFY_FAMILY_VALIDATOR_ROOT", "/opt/bloodbank")),
            link_hosts=tuple(h.strip().lower() for h in env.get("NTFY_TOLT_LINK_HOSTS", "tolt.delo.sh").split(",") if h.strip()),
        )


class ValidatorUnavailable(RuntimeError):
    pass


class CanonicalValidator:
    """Load the installed canonical module by path, never an ambient core import."""
    def __init__(self, root: Path):
        root = root.resolve()
        path = root / "services/agent-hooks/core/validate.py"
        if not (path.is_file() and (root / "docs/event-naming.md").is_file()
                and (root / "schemas").is_dir()):
            raise ValidatorUnavailable("canonical validator tree unavailable")
        spec = importlib.util.spec_from_file_location(
            "_ntfy_canonical_" + hashlib.sha256(str(path).encode()).hexdigest()[:16], path)
        if spec is None or spec.loader is None:
            raise ValidatorUnavailable("canonical validator module unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if module._schemas_root().resolve() != (root / "schemas").resolve():
            raise ValidatorUnavailable("canonical schema override does not match installed tree")
        self.module = module

    def __call__(self, envelope):
        try:
            self.module.validate_envelope(envelope)
        except self.module.ValidationUnavailable as exc:
            raise ValidatorUnavailable("canonical schema validation unavailable") from exc


def action_url(value, allowed_hosts):
    url = urlsplit(value)
    if (url.scheme != "https" or url.hostname not in allowed_hosts
            or url.username or url.password or url.port not in (None, 443)
            or "#" in value or "\\" in value
            or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        return None
    return value


def format_message(envelope, config: Config):
    """Called only after canonical validation; schema rules stay in Bloodbank."""
    kind, d = envelope["type"], envelope["data"]
    lines = []
    if kind == IMPLEMENTATION:
        title = "Game implementation started"
        lines = [f"Implementation: {d['implementation_id']}", f"Job: {text(d['job_id'])}",
                 f"Concept: {d['concept_id']}", "Prepared build phase entered running."]
    elif kind == PACK:
        p = d["pack"]
        title = f"{text(d['title'], 120)} — pack v{p['version']} ready"
        lines = [f"Pack: {p['id']} v{p['version']} ({d['release_kind']})",
                 f"PCK SHA-256: {p['sha256']}", f"Changes: {text(d['changelog'], 500)}"]
        for channel in d["channels"]:
            native = channel["native_build"]
            suffix = (f" · {native['bundle_id']} {native['app_version']} build {native['build_number']}"
                      if native else "")
            lines.append(f"Verified playable: {channel['channel']}{suffix}")
        if d["source_job"]:
            lines.append(f"Source job: {text(d['source_job'])}")
        if d["source_ticket"]:
            lines.append(f"Source ticket: {d['source_ticket']}")
    elif kind == LOW_WATER:
        title = "Forge concepts running low"
        lines = [f"Eligible novel concepts: {d['eligible_count']} (threshold {d['threshold']})",
                 f"Pool: {d['pool_id']}", f"Existing episode: {d['episode_id']}",
                 "Replenish the authorized curated concept pool."]
    elif kind == ITEM:
        title = f"Thing minted: {text(d['item_name'], 120)}"
        lines = [f"Item: {d['item_id']}", "Content ready; required art landed on content main.",
                 f"Registry status: {d['registry_status']} (not inferred from mint)",
                 f"Item SHA-256: {d['item_sha256']}"]
    elif kind == APP_LAUNCH:
        native = d["native_build"]
        title = ("First TestFlight startup" if d["testflight_eligible"] else
                 f"First app startup — {d['channel']}")
        lines = [f"Native bundle: {native['bundle_id']}",
                 f"App version: {native['app_version']} · final iOS build: {native['build_number']}",
                 f"Installation: {d['installation_id']}", f"Channel: {d['channel']}",
                 "Actual native app startup, not deployment or pack launch."]
    else:
        raise ValueError("not a family event")
    lines += [f"Observed: {d['observed_at']}", f"Event: {envelope['id']}"]
    message = {"topic": config.topic, "title": text(title),
               "message": "\n".join(lines), "priority": 4 if kind == LOW_WATER else 3,
               "tags": ["video_game"]}
    if kind == PACK:
        link = action_url(d["playable_url"], config.link_hosts)
        if link:
            message.update(click=link, actions=[{"action": "view", "label": "Play exact pack", "url": link}])
    return message


@dataclass(frozen=True)
class Receipt:
    status: int
    message_id: str | None = None
    retry_after: float | None = None
    # A 5xx is ambiguous unless the transport actually proves preaccept rejection.
    preaccept: bool = False


def make_http_poster(client, config: Config, token: str):
    import httpx

    async def post(request_json):
        try:
            response = await client.post(
                config.base_url.rstrip("/") + "/",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                content=request_json.encode("utf-8"),
                follow_redirects=False,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # No HTTP request could be accepted on a connection never established.
            return Receipt(0, preaccept=True)
        # Read/write timeouts and other transport failures remain ambiguous.
        try:
            body = response.json()
        except (ValueError, UnicodeError):
            body = {}
        message_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(message_id, str) or len(message_id) > 128:
            message_id = None
        try:
            delay = float(response.headers["Retry-After"])
            if not math.isfinite(delay):
                delay = None
        except (KeyError, ValueError):
            delay = None
        return Receipt(response.status_code, message_id, delay,
                       preaccept=response.status_code == 429)

    return post


class DeliveryLedger:
    """Short independent SQLite transactions; no connection survives an await."""
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS delivery (
                    event_id TEXT NOT NULL, destination TEXT NOT NULL,
                    event_type TEXT NOT NULL, envelope_json TEXT NOT NULL,
                    request_json TEXT NOT NULL, digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('pending','sending','accepted','uncertain','exhausted','conflict','suppressed')),
                    reason TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, claim_token TEXT,
                    lease_until REAL, http_status INTEGER, message_id TEXT,
                    conflict_digest TEXT, late_http_status INTEGER, late_message_id TEXT,
                    PRIMARY KEY(event_id,destination)
                );
                CREATE TABLE IF NOT EXISTS destination_pause (
                    destination TEXT PRIMARY KEY, until_time REAL NOT NULL
                );
            """)
        os.chmod(path, 0o600)

    @contextmanager
    def connection(self, transaction=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            if transaction:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if transaction:
                db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def get(self, event_id, destination):
        with self.connection() as db:
            row = db.execute("SELECT * FROM delivery WHERE event_id=? AND destination=?",
                             (event_id, destination)).fetchone()
            return dict(row) if row else None

    def offer(self, envelope, request, destination, suppressed_reason=""):
        env_json, request_json = canonical(envelope), canonical(request)
        digest = hashlib.sha256(canonical([envelope, request]).encode()).hexdigest()
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM delivery WHERE event_id=? AND destination=?",
                             (envelope["id"], destination)).fetchone()
            if row:
                if row["digest"] != digest:
                    db.execute("UPDATE delivery SET state='conflict', reason='immutable-input-conflict', "
                               "conflict_digest=? WHERE event_id=? AND destination=?",
                               (digest, envelope["id"], destination))
                    return False, "conflict"
                return False, row["state"]
            state = "suppressed" if suppressed_reason else "pending"
            db.execute("INSERT INTO delivery "
                       "(event_id,destination,event_type,envelope_json,request_json,digest,state,reason) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (envelope["id"], destination, envelope["type"], env_json,
                        request_json, digest, state, suppressed_reason))
            return True, state

    def recover_expired(self, now):
        with self.connection(True) as db:
            return db.execute("UPDATE delivery SET state='uncertain', reason='claim-expired-no-receipt' "
                              "WHERE state='sending' AND lease_until<=?", (now,)).rowcount

    def suppress_pending(self, event_id, destination, reason):
        with self.connection(True) as db:
            db.execute("UPDATE delivery SET state='suppressed', reason=? "
                       "WHERE event_id=? AND destination=? AND state='pending'",
                       (reason, event_id, destination))
            row = db.execute("SELECT state FROM delivery WHERE event_id=? AND destination=?",
                             (event_id, destination)).fetchone()
            return row["state"] if row else "unavailable"

    def conflict(self, event_id, destination):
        with self.connection(True) as db:
            db.execute("UPDATE delivery SET state='conflict', reason='stored-intent-integrity' "
                       "WHERE event_id=? AND destination=?", (event_id, destination))

    def due(self, destination, now, limit):
        with self.connection() as db:
            return [dict(r) for r in db.execute(
                "SELECT event_id,event_type FROM delivery WHERE destination=? "
                "AND state='pending' AND next_attempt<=? ORDER BY rowid LIMIT ?",
                (destination, now, limit))]

    def pending_launches(self, destination, after_rowid, limit):
        with self.connection() as db:
            return [dict(r) for r in db.execute(
                "SELECT rowid AS scan_id,event_id FROM delivery WHERE destination=? "
                "AND state='pending' AND event_type=? AND rowid>? ORDER BY rowid LIMIT ?",
                (destination, APP_LAUNCH, after_rowid, limit))]

    def claim(self, event_id, destination, now, config):
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM delivery WHERE event_id=? AND destination=?",
                             (event_id, destination)).fetchone()
            pause = db.execute("SELECT until_time FROM destination_pause WHERE destination=?",
                               (destination,)).fetchone()
            if (not row or row["state"] != "pending" or row["next_attempt"] > now
                    or (pause and pause["until_time"] > now)):
                return None
            if row["attempts"] >= config.max_attempts:
                db.execute("UPDATE delivery SET state='exhausted', reason='attempt-limit' "
                           "WHERE event_id=? AND destination=?", (event_id, destination))
                return None
            token = str(uuid.uuid4())
            db.execute("UPDATE delivery SET state='sending', reason='claimed-before-http', "
                       "attempts=attempts+1, claim_token=?, lease_until=? "
                       "WHERE event_id=? AND destination=?",
                       (token, now + config.lease_seconds, event_id, destination))
            return {**dict(row), "claim_token": token, "attempts": row["attempts"] + 1}

    def finish(self, event_id, destination, token, now, receipt, config):
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM delivery WHERE event_id=? AND destination=?",
                             (event_id, destination)).fetchone()
            if not row or row["claim_token"] != token:
                return "uncertain"
            if row["state"] != "sending":
                if receipt:
                    db.execute("UPDATE delivery SET late_http_status=?, late_message_id=? "
                               "WHERE event_id=? AND destination=? AND claim_token=?",
                               (receipt.status, receipt.message_id, event_id, destination, token))
                return row["state"]  # Late callbacks never erase a conflict/hold/uncertainty.
            if row["lease_until"] <= now:
                db.execute("UPDATE delivery SET state='uncertain', reason='late-receipt', "
                           "late_http_status=?, late_message_id=? WHERE event_id=? AND destination=?",
                           (receipt.status if receipt else None, receipt.message_id if receipt else None,
                            event_id, destination))
                return "uncertain"
            state, reason, next_attempt = "uncertain", "unknown-http-outcome", 0
            if receipt and type(receipt.status) is int:
                status = receipt.status
                if 200 <= status < 300:
                    state, reason = "accepted", "http-accepted-not-device-proof"
                elif (status == 429 or (receipt.preaccept is True and (status == 0 or 500 <= status < 600))):
                    delay = min(config.retry_base * 2 ** (row["attempts"] - 1), 3600)
                    if receipt.retry_after is not None and math.isfinite(receipt.retry_after):
                        delay = min(max(delay, receipt.retry_after), 3600)
                    next_attempt = now + delay
                    state = "pending" if row["attempts"] < config.max_attempts else "exhausted"
                    reason = "preaccept-rejection" if state == "pending" else "attempt-limit"
                    db.execute("INSERT INTO destination_pause VALUES (?,?) ON CONFLICT(destination) "
                               "DO UPDATE SET until_time=max(until_time,excluded.until_time)",
                               (destination, next_attempt))
                elif 400 <= status < 500 and status != 408:
                    state, reason = "exhausted", "http-rejected"
            db.execute("UPDATE delivery SET state=?, reason=?, next_attempt=?, http_status=?, "
                       "message_id=? WHERE event_id=? AND destination=? AND state='sending' AND claim_token=?",
                       (state, reason, next_attempt, receipt.status if receipt else None,
                        receipt.message_id if receipt else None, event_id, destination, token))
            return state

    def counts(self):
        with self.connection() as db:
            return {r["state"]: r["n"] for r in db.execute(
                "SELECT state,count(*) AS n FROM delivery GROUP BY state")}


class FamilyRoute:
    def __init__(self, config, ledger, validator, post, clock=time.time):
        self.config, self.ledger, self.validator, self.post, self.clock = config, ledger, validator, post, clock
        self._launch_scan_cursor = 0

    @classmethod
    def from_env(cls, client, token):
        config = Config.from_env()
        ledger = DeliveryLedger(config.state_path)
        try:
            validator = CanonicalValidator(config.validator_root)
        except Exception:
            validator = None
        return cls(config, ledger, validator, make_http_poster(client, config, token))

    async def handle(self, envelope, subject=""):
        if self.validator is None:
            return "family-unavailable"
        try:
            if envelope.get("type") not in TYPES or (subject and subject != envelope.get("subject")):
                return "family-invalid"
            await asyncio.to_thread(self.validator, envelope)
            request = format_message(envelope, self.config)
            if len(canonical(envelope).encode()) > 262144:
                return "family-invalid"
        except ValidatorUnavailable:
            return "family-unavailable"
        except Exception:
            return "family-invalid"
        reason = suppression_reason(envelope) or ("cutover-not-accepted" if not self.config.authorized else "")
        try:
            new, state = await asyncio.to_thread(
                self.ledger.offer, envelope, request, self.config.destination, reason)
            if state != "pending":
                return "family-duplicate" if state == "accepted" else "family-" + state
            if reason:
                state = await asyncio.to_thread(
                    self.ledger.suppress_pending, envelope["id"], self.config.destination, reason)
                return "family-" + state
            return await self.deliver(envelope["id"])
        except (sqlite3.Error, OSError):
            return "family-unavailable"

    async def deliver(self, event_id, *, allow_post=True):
        if self.validator is None:
            return "family-unavailable"
        row = await asyncio.to_thread(self.ledger.get, event_id, self.config.destination)
        if row is None:
            return "family-unavailable"
        if row["state"] != "pending":
            return "family-" + row["state"]
        if row["event_type"] == LOW_WATER:
            await asyncio.to_thread(self.ledger.suppress_pending, event_id, self.config.destination,
                                    "lowwater-owner-episode-gate-unavailable")
            return "family-suppressed"
        # Pending restart work is still checked by the installed validator.
        try:
            envelope, request = json.loads(row["envelope_json"]), json.loads(row["request_json"])
            digest = hashlib.sha256(canonical([envelope, request]).encode()).hexdigest()
            if (digest != row["digest"] or envelope["id"] != event_id
                    or envelope["type"] != row["event_type"] or request["topic"] != self.config.topic):
                await asyncio.to_thread(self.ledger.conflict, event_id, self.config.destination)
                return "family-conflict"
            await asyncio.to_thread(self.validator, envelope)
        except Exception:
            return "family-unavailable"
        reason = suppression_reason(envelope)
        if reason:
            state = await asyncio.to_thread(
                self.ledger.suppress_pending, event_id, self.config.destination, reason)
            return "family-" + state
        if not self.config.authorized:
            return "family-disabled"
        if not allow_post:
            return "family-pending"
        claim = await asyncio.to_thread(
            self.ledger.claim, event_id, self.config.destination, self.clock(), self.config)
        if claim is None:
            return "family-" + (await asyncio.to_thread(self.ledger.get, event_id, self.config.destination))["state"]
        receipt = None
        try:
            receipt = await asyncio.wait_for(self.post(claim["request_json"]), timeout=self.config.post_timeout)
            if not isinstance(receipt, Receipt):
                receipt = None
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(
                self.ledger.finish, event_id, self.config.destination, claim["claim_token"],
                self.clock(), None, self.config))
            raise
        except Exception:
            pass  # No request, response body or exception text is logged.
        state = await asyncio.to_thread(
            self.ledger.finish, event_id, self.config.destination, claim["claim_token"],
            self.clock(), receipt, self.config)
        return "family-" + state

    async def drain(self):
        await asyncio.to_thread(self.ledger.recover_expired, self.clock())
        if self.validator is None:
            return []
        # Scan saved launches even when posting is disabled or a retry is not due.
        # Advance a bounded cursor so future eligible TF rows cannot starve holds.
        launches = await asyncio.to_thread(
            self.ledger.pending_launches, self.config.destination,
            self._launch_scan_cursor, self.config.drain_limit)
        if not launches and self._launch_scan_cursor:
            self._launch_scan_cursor = 0
            launches = await asyncio.to_thread(
                self.ledger.pending_launches, self.config.destination, 0, self.config.drain_limit)
        results = []
        for launch in launches:
            result = await self.deliver(launch["event_id"], allow_post=False)
            self._launch_scan_cursor = launch["scan_id"]
            if result not in ("family-pending", "family-disabled"):
                results.append(result)
        if not self.config.authorized:
            return results
        rows = await asyncio.to_thread(
            self.ledger.due, self.config.destination, self.clock(), self.config.drain_limit)
        return results + [await self.deliver(row["event_id"]) for row in rows]
