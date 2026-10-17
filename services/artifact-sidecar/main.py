"""artifact-sidecar runtime: durable JetStream consumer on the agent bus.

Consumes bloodbank.evt.agent.tool.completed and
bloodbank.conversation.turn.completed from BLOODBANK_EVENTS with an
explicit-ack durable consumer, captures qualifying artifact bytes into the
content-addressed store, resolves the project through the canonical pjangler
registry, validates and enqueues bloodbank.project.artifact.created events,
and publishes them from the durable outbox through the REAL awaited
JetStream transport (Nats-Msg-Id = stable event id, so server-side dedupe
collapses replays).

Ack contract (durable-outbox-after-commit): the source message is acked only
once its capture is durably enqueued, held, duplicate, or terminally
unqualified — the outbox, not the ack, owns publication. Publication retries
on the idle timer (not only on the next incoming event) with exponential
backoff and bounded attempts; exhausted rows stay visible in --status
(nonzero exit) until an operator re-arms them with --retry-outbox.

Delivery policy: a NEW durable is created with DeliverPolicy.NEW (no
seven-day vault dump); backfill over retained history is the explicit
ARTIFACT_SIDECAR_BACKFILL=1 operator choice at first start.

Usage:
  python3 main.py                 # run the consumer loop
  python3 main.py --status        # state/outbox/holds snapshot (nonzero when exhausted/unresolved)
  python3 main.py --hold-show     # list held captures awaiting attribution
  python3 main.py --hold-resolve <hold_id> <project-slug>
  python3 main.py --hold-publish <hold_id>  # enqueue an operator-resolved hold
  python3 main.py --retry-outbox  # re-arm exhausted outbox rows

Environment:
  ARTIFACT_SIDECAR_NATS_URL     default nats://127.0.0.1:4222
  ARTIFACT_SIDECAR_STATE_DIR    default ~/.local/state/artifact-sidecar
  ARTIFACT_SIDECAR_STORE        default <state dir>/store
  ARTIFACT_SIDECAR_REGISTRY_URL default http://127.0.0.1:8764/v1/registry
  ARTIFACT_SIDECAR_REGISTRY_FILE optional JSON file snapshot of the registry
  ARTIFACT_SIDECAR_REGISTRY_REFRESH_S registry re-fetch period, default 300 (0 disables)
  ARTIFACT_SIDECAR_DURABLE      default bloodbank-artifact-sidecar
  ARTIFACT_SIDECAR_BACKFILL     "1" => new durable created with DeliverPolicy.ALL
  ARTIFACT_SIDECAR_OUTBOX_RETRY_S idle-timer outbox retry period, default 5
  ARTIFACT_SIDECAR_CAPTURE_ROOTS ':'-separated ABSOLUTE roots under which an
                                explicitly delivered local file (MEDIA:/path
                                token in a producer-delivered reply, or a
                                deliverables[] path) may be read and captured.
                                EMPTY by default: delivered-file capture is a
                                strict operator opt-in; every delivered path
                                outside these roots (and every path matching
                                the denylist: .hermes, .git, config/, …) is
                                refused, never silently captured.

NOT activated anywhere: no compose service, no systemd unit is installed by
this file. See ops/systemd/ for the deployment templates the parent reviews.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "services" / "agent-hooks")
)

import nats  # nats-py: same client the hermes-gateway and ntfy-router use
import nats.js.errors
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.aio.msg import Msg

import artifact_lib
import discovery
from artifact_lib import ProjectRegistry, RegistryError, ensure_store_root
from sidecar import Disposition, Sidecar, SidecarState

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [artifact-sidecar] %(levelname)s %(message)s",
)
log = logging.getLogger("artifact-sidecar")

EVENTS_STREAM = "BLOODBANK_EVENTS"
SOURCE_SUBJECTS = (
    "bloodbank.evt.agent.tool.completed",
    "bloodbank.evt.conversation.turn.completed",
)

# Disposition kinds that are durable terminal states for the source message:
# the capture is committed to the store and its outcome is durably recorded
# (enqueued / held / duplicate / skipped / unsupported), so acking is safe.
_TERMINAL_KINDS = frozenset({"enqueued", "duplicate", "held", "skipped", "unsupported"})


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _default_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "artifact-sidecar"


def load_registry() -> ProjectRegistry:
    file_path = _env("ARTIFACT_SIDECAR_REGISTRY_FILE", "")
    if file_path:
        payload = json.loads(Path(file_path).read_text())
        return ProjectRegistry.from_payload(payload)
    url = _env("ARTIFACT_SIDECAR_REGISTRY_URL", "http://127.0.0.1:8764/v1/registry")
    return ProjectRegistry.from_http(url)


def build_sidecar() -> tuple[Sidecar, SidecarState, Path]:
    state_dir = Path(
        _env("ARTIFACT_SIDECAR_STATE_DIR", str(_default_state_dir()))
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    store_root = ensure_store_root(
        Path(_env("ARTIFACT_SIDECAR_STORE", str(state_dir / "store")))
    )
    state = SidecarState(state_dir / "state.sqlite3")
    registry = load_registry()
    return (
        Sidecar(state=state, store_root=store_root, registry=registry),
        state,
        store_root,
    )


class Runtime:
    def __init__(self, sidecar: Sidecar):
        self.sidecar = sidecar
        self._nc: Any = None
        self._js: Any = None
        self._stop = asyncio.Event()

    async def connect(self, nats_url: str, durable: str, *, backfill: bool = False) -> None:
        self._nc = await nats.connect(
            nats_url,
            name=f"bloodbank-{durable}",
            connect_timeout=5,
            max_reconnect_attempts=-1,  # reconnect forever, like the router
            reconnect_time_wait=2,
        )
        self._js = self._nc.jetstream()
        try:
            await self._js.consumer_info(EVENTS_STREAM, durable)
            existing = True
        except (nats.js.errors.NotFoundError, KeyError, Exception):  # noqa: BLE001
            existing = False
        # Delivery policy for a NEW durable only: NEW unless backfill is the
        # explicit operator choice (DeliverPolicy.ALL would replay the whole
        # retained window — a seven-day vault dump is never the default).
        config = None
        if not existing:
            config = ConsumerConfig(
                durable_name=durable,
                description="Project artifact discovery sidecar (tool/turn source events)",
                deliver_policy=DeliverPolicy.ALL if backfill else DeliverPolicy.NEW,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=30,
                max_deliver=10,
                filter_subjects=list(SOURCE_SUBJECTS),
                max_waiting=1,
                max_ack_pending=32,
            )
        self._sub = await self._js.pull_subscribe(
            SOURCE_SUBJECTS[0],
            durable=durable,
            stream=EVENTS_STREAM,
            config=config,
        )
        log.info(
            "consumer %s (%s, deliver=%s)",
            durable,
            "existing" if existing else "created",
            "ALL(backfill)" if (not existing and backfill) else "NEW" if not existing else "bound",
        )

    async def aclose(self) -> None:
        """Close the transport cleanly (SIGTERM path and tests)."""
        nc, self._nc, self._js = self._nc, None, None
        if nc is not None:
            try:
                await nc.drain()
            finally:
                await nc.close()

    async def _publish(self, envelope: dict[str, Any]) -> None:
        """Publish through the real awaited JetStream transport.

        Nats-Msg-Id carries the STABLE event id: JetStream's server-side
        dedupe window collapses outbox replays of the same event id instead
        of producing duplicates after a crash.
        """
        subject = envelope["subject"]
        ack = await self._js.publish(
            subject,
            json.dumps(envelope).encode("utf-8"),
            timeout=5,
            headers={"Nats-Msg-Id": str(envelope["id"])},
        )
        log.debug("published %s -> seq %s", envelope["id"], getattr(ack, "seq", "?"))

    async def _drain_outbox(self) -> int:
        """Publish due outbox rows through JetStream; returns published count.

        Publication errors are recorded on the row (bounded attempts,
        exponential backoff) and never mark anything published — a row is
        'published' only when the transport accepted it.
        """
        published = 0
        for row in self.sidecar.state.due():
            envelope = json.loads(row["envelope_json"])
            try:
                await self._publish(envelope)
            except Exception as exc:  # noqa: BLE001
                self.sidecar.state.record_attempt(
                    row["outbox_id"], f"{type(exc).__name__}: {exc}"
                )
                log.warning(
                    "outbox publish failed event=%s attempts=%s",
                    row["event_id"], row["attempts"] + 1,
                )
                continue
            self.sidecar.state._finish_publish(row)
            published += 1
        return published

    async def _handle(self, msg: Msg) -> None:
        # Source validation: subject must be one we subscribed; the envelope
        # type must match the subject's entity (defends against cross-wired
        # publishers).
        try:
            envelope = json.loads(msg.data.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("malformed message on %s: %s; acking", msg.subject, exc)
            await msg.ack()
            return
        if not isinstance(envelope, dict):
            log.warning("non-object message on %s; acking", msg.subject)
            await msg.ack()
            return
        expected = {
            "bloodbank.evt.agent.tool.completed": "bloodbank.agent.tool.completed",
            "bloodbank.evt.conversation.turn.completed": "bloodbank.conversation.turn.completed",
        }.get(msg.subject)
        if expected and envelope.get("type") != expected:
            log.warning(
                "subject/type mismatch on %s: type=%r; acking foreign envelope",
                msg.subject, envelope.get("type"),
            )
            await msg.ack()
            return
        try:
            dispositions = self.sidecar.process(envelope)
        except Exception as exc:  # noqa: BLE001
            log.exception("processing failed for %s; nak", msg.subject)
            await msg.nak(delay=5)
            return
        for d in dispositions:
            log.info(
                "disposition kind=%s artifact=%s rev=%s :: %s",
                d.kind, d.artifact_id, d.revision, d.detail,
            )
        # Durable work for this message is done when every disposition is a
        # durable terminal state. Candidate errors are contained: a
        # deterministic validation failure is poison (ack, loudly logged,
        # never publishable); anything else NAKs for redelivery with the
        # bytes/holds preserved.
        kinds = {d.kind for d in dispositions}
        if kinds and kinds <= _TERMINAL_KINDS:
            try:
                await self._drain_outbox()
            except Exception:  # noqa: BLE001 - drain errors don't uncommit the capture
                log.exception("outbox drain after handle failed; idle timer will retry")
            await msg.ack()
            return
        for d in dispositions:
            if d.kind == "error":
                if "envelope failed validation" in d.detail:
                    log.error("poison candidate (deterministic): %s; acking", d.detail)
                else:
                    log.warning("candidate error (retryable): %s; nak", d.detail)
        await msg.nak(delay=5)

    async def _refresh_registry(self) -> None:
        """Bounded registry re-fetch; keeps last-good on failure."""
        try:
            registry = await asyncio.get_running_loop().run_in_executor(
                None, load_registry
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("registry refresh failed (keeping last-good): %s", exc)
            return
        self.sidecar.registry = registry
        log.info("registry refreshed (%d projects)", len(registry._projects))

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)
        retry_period = float(_env("ARTIFACT_SIDECAR_OUTBOX_RETRY_S", "5"))
        refresh_period = float(_env("ARTIFACT_SIDECAR_REGISTRY_REFRESH_S", "300"))
        last_drain = time.monotonic()
        last_refresh = time.monotonic()
        while not self._stop.is_set():
            try:
                msgs = await self._sub.fetch(batch=8, timeout=2)
            except TimeoutError:
                # Idle timer: outbox retries must not depend on new traffic.
                now = time.monotonic()
                if now - last_drain >= retry_period:
                    try:
                        await self._drain_outbox()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("idle outbox drain failed: %s", exc)
                    last_drain = now
                if refresh_period > 0 and now - last_refresh >= refresh_period:
                    await self._refresh_registry()
                    last_refresh = now
                continue
            except Exception as exc:  # noqa: BLE001 - reconnect storms etc.
                if self._stop.is_set():
                    break
                log.warning("fetch failed: %s: %s", type(exc).__name__, exc)
                await asyncio.sleep(2)
                continue
            last_drain = time.monotonic()
            for msg in msgs:
                await self._handle(msg)


async def amain(args: argparse.Namespace) -> int:
    sidecar, state, store_root = build_sidecar()
    runtime: Runtime | None = None
    try:
        if args.status:
            pending = state.pending()
            exhausted = state.exhausted()
            holds = state.holds()
            print(json.dumps({
                "store_root": str(store_root),
                "artifacts": _artifact_summary(state),
                "pending_outbox": len(pending),
                "pending_detail": [
                    {"event_id": r["event_id"], "attempts": r["attempts"], "last_error": r["last_error"]}
                    for r in pending
                ],
                "exhausted_outbox": len(exhausted),
                "exhausted_detail": [
                    {"event_id": r["event_id"], "attempts": r["attempts"], "last_error": r["last_error"]}
                    for r in exhausted
                ],
                "unresolved_holds": len(holds),
            }, indent=2))
            # Health signal: exhausted publication attempts and unresolved
            # holds are operator-visible states; nonzero exit makes them
            # impossible to miss in scripts and monitors.
            return 1 if (exhausted or holds) else 0
        if args.hold_show:
            print(json.dumps(state.holds(include_resolved=args.all), indent=2))
            return 0
        if args.hold_resolve:
            hold_id, slug = args.hold_resolve
            if not state.resolve_hold(hold_id, slug):
                print(f"no hold with id {hold_id}", file=sys.stderr)
                return 1
            print(f"resolved {hold_id} -> {slug}")
            return 0
        if args.hold_publish:
            d = sidecar.publish_resolution(args.hold_publish[0])
            print(json.dumps({"kind": d.kind, "detail": d.detail, "artifact_id": d.artifact_id, "revision": d.revision}))
            return 0 if d.kind in ("enqueued", "published", "duplicate") else 1
        if args.retry_outbox:
            re_armed = state.retry_exhausted()
            print(json.dumps({"re_armed": re_armed}))
            return 0

        nats_url = _env("ARTIFACT_SIDECAR_NATS_URL", "nats://127.0.0.1:4222")
        durable = _env("ARTIFACT_SIDECAR_DURABLE", "bloodbank-artifact-sidecar")
        backfill = _env("ARTIFACT_SIDECAR_BACKFILL", "") == "1"
        runtime = Runtime(sidecar)
        log.info("connecting %s durable=%s subjects=%s", nats_url, durable, SOURCE_SUBJECTS)
        await runtime.connect(nats_url, durable, backfill=backfill)
        # Crash catch-up: publish anything the outbox still holds through the
        # REAL transport (awaited JetStream publish, Nats-Msg-Id dedupe).
        try:
            await runtime._drain_outbox()
        except Exception as exc:  # noqa: BLE001
            log.warning("startup outbox drain failed (idle timer will retry): %s", exc)
        log.info("subscribed; consuming")
        await runtime.run()
        return 0
    finally:
        if runtime is not None:
            try:
                await runtime.aclose()
            except Exception:  # noqa: BLE001 - teardown must not mask errors
                pass
        state.close()


def _artifact_summary(state: SidecarState) -> dict[str, Any]:
    cur = state._db.execute(
        "SELECT project_slug, COUNT(DISTINCT artifact_id) AS artifacts, MAX(revision) AS max_rev "
        "FROM artifacts GROUP BY project_slug"
    )
    return {row["project_slug"]: {"artifacts": row["artifacts"], "max_revision": row["max_rev"]} for row in cur}


_DOC_LINES = (__doc__ or "artifact-sidecar").splitlines()


def main() -> int:
    parser = argparse.ArgumentParser(description=_DOC_LINES[0])
    parser.add_argument("--status", action="store_true", help="state/outbox/holds snapshot (nonzero on exhausted/unresolved)")
    parser.add_argument("--hold-show", action="store_true", help="list held captures")
    parser.add_argument("--all", action="store_true", help="include resolved holds")
    parser.add_argument("--hold-resolve", nargs=2, metavar=("HOLD_ID", "SLUG"))
    parser.add_argument("--hold-publish", nargs=1, metavar=("HOLD_ID",))
    parser.add_argument("--retry-outbox", action="store_true", help="re-arm exhausted outbox rows")
    args = parser.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
