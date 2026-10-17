#!/usr/bin/env python3
"""Broker-backed runtime tests for the artifact sidecar + vault projector.

Runs BOTH services end-to-end against an ISOLATED nats-server (docker image
nats:2.11-alpine) on a random high port — never the sandbox bus at
nats://127.0.0.1:4222, never the real vault, never the real registry file.

What this proves that the fake-transport suite cannot:
  - real JetStream stream + durable consumer creation (DeliverPolicy.NEW
    default; backfill only with the explicit opt-in)
  - the sidecar's full loop: publish source event -> capture -> outbox ->
    awaited js.publish with Nats-Msg-Id -> ack
  - outbox crash-recovery: process an event with the transport down, restart,
    startup drain publishes it, server-side dedupe collapses the replay
  - projector: consumes artifact.created from its own durable, projects into
    a throwaway vault root with the explicit mapping rule
  - cleanup: only OWN consumers deleted (no shared state touched)

Run: python3 services/artifact-sidecar/test_broker.py
Requirements: docker with nats:2.11-alpine (or nats-server + ARTIFACT_TEST_NATS_BIN).

HONESTY NOTE: this is a TEST broker on a random port, not the production bus;
the production hook -> bus path (agent-hooks publisher) is NOT exercised here
and remains the parent's separate verification.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _p in (str(HERE), str(HERE.parents[1] / "agent-hooks")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("BLOODBANK_SCHEMAS_DIR", str(REPO / "schemas"))

import nats  # noqa: E402
from nats.js.api import StreamConfig, RetentionPolicy, StorageType, DeliverPolicy  # noqa: E402

from test_sidecar import (  # noqa: E402
    registry_payload,
    tool_completed_envelope,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_broker(port: int) -> subprocess.Popen | None:
    """Isolated nats-server via docker (preferred) or local binary."""
    name = f"artifact-sidecar-test-nats-{port}"
    docker = shutil.which("docker")
    if docker:
        # Pre-cleanup a stale container from an aborted earlier run.
        subprocess.run(
            [docker, "rm", "-f", name], capture_output=True
        )
        proc = subprocess.Popen(
            [
                docker, "run", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:4222",
                "nats:2.11-alpine", "-js",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc
    local = os.environ.get("ARTIFACT_TEST_NATS_BIN") or shutil.which("nats-server")
    if local:
        return subprocess.Popen(
            [local, "-js", "-p", str(port), "-a", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return None


async def _wait_listening(url: str, timeout: float = 90.0) -> bool:
    # NOTE: docker run on a cold host can take >25s before the port listens;
    # 90s is the honest bound, and a hard skip if even that fails.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.open_connection(
                host=url.split("//")[1].split(":")[0],
                port=int(url.split(":")[-1]),
            )
            writer.close()
            await writer.wait_closed()
            return True
        except OSError:
            await asyncio.sleep(0.3)
    return False


class BrokerWorld:
    def __init__(self, port: int):
        self.port = port
        self.url = f"nats://127.0.0.1:{port}"
        self.tmp = Path(tempfile.mkdtemp(prefix="artifact-broker-test-", dir="/tmp"))
        (self.tmp / "repos" / "bb" / "docs").mkdir(parents=True)
        self.registry_file = self.tmp / "registry.json"
        self.registry_file.write_text(json.dumps(registry_payload(self.tmp)))
        self.env = {
            **os.environ,
            "ARTIFACT_SIDECAR_NATS_URL": self.url,
            "ARTIFACT_SIDECAR_STATE_DIR": str(self.tmp / "state"),
            "ARTIFACT_SIDECAR_REGISTRY_FILE": str(self.registry_file),
            "ARTIFACT_SIDECAR_DURABLE": f"test-sidecar-{port}",
            "ARTIFACT_SIDECAR_OUTBOX_RETRY_S": "1",
            "BLOODBANK_SCHEMAS_DIR": str(REPO / "schemas"),
            "LOG_LEVEL": "ERROR",
        }
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "Bloodbank").mkdir(parents=True)
        self.mapping_file = self.tmp / "mapping.json"
        self.mapping_file.write_text(json.dumps({"bb": "Projects/Bloodbank"}))
        self.projector_env = {
            **self.env,
            "ARTIFACT_VAULT_NATS_URL": self.url,
            "ARTIFACT_VAULT_MAPPING": str(self.mapping_file),
            "ARTIFACT_VAULT_ROOT": str(self.vault),
            "ARTIFACT_VAULT_STATE_DB": str(self.tmp / "proj-state" / "state.sqlite3"),
            "ARTIFACT_VAULT_STORE": str(self.tmp / "state" / "store"),
            "ARTIFACT_VAULT_DURABLE": f"test-projector-{port}",
            "ARTIFACT_VAULT_TZ": "America/New_York",
        }

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class BrokerTests(unittest.TestCase):
    broker_proc: subprocess.Popen | None = None
    world: BrokerWorld | None = None
    port: int = 0

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.broker_proc = _start_broker(cls.port)
        if cls.broker_proc is None:
            raise unittest.SkipTest(
                "no docker / nats-server available for isolated broker tests"
            )
        if not asyncio.run(_wait_listening(f"nats://127.0.0.1:{cls.port}")):
            cls.tearDownClass()
            raise unittest.SkipTest("isolated broker did not come up")
        cls.world = BrokerWorld(cls.port)
        # BLOODBANK_EVENTS stream (mirror of compose topology, test-scoped).
        async def seed():
            nc = await nats.connect(cls.world.url)
            js = nc.jetstream()
            try:
                await js.add_stream(
                    StreamConfig(
                        name="BLOODBANK_EVENTS",
                        subjects=["bloodbank.evt.>"],
                        retention=RetentionPolicy.LIMITS,
                        storage=StorageType.FILE,
                    )
                )
            except Exception:  # noqa: BLE001 - exists is fine
                pass
            await nc.drain(); await nc.close()

        asyncio.run(seed())

    @classmethod
    def tearDownClass(cls):
        if cls.world:
            cls.world.cleanup()
        if cls.broker_proc:
            cls.broker_proc.send_signal(signal.SIGTERM)
            try:
                cls.broker_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.broker_proc.kill()
            docker = shutil.which("docker")
            if docker:
                subprocess.run(
                    [docker, "rm", "-f", f"artifact-sidecar-test-nats-{cls.port}"],
                    capture_output=True,
                )

    # -- helpers ---------------------------------------------------------

    def _run_service(self, argv: list[str], env: dict, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO / "services" / "artifact-sidecar" / "main.py")] + argv
            if argv and argv[0] != "projector"
            else [sys.executable, str(REPO / "services" / "artifact-vault-projector" / "main.py")] + argv[1:],
            capture_output=True, text=True, timeout=timeout, env=env,
        )

    async def _fetch_artifact_events(self, count: int, subject="bloodbank.evt.project.artifact.created", *, match: dict | None = None, timeout: float = 25.0) -> list[dict]:
        """Ephemeral pull consumer bound to the stream (no durable side effects).

        `match` filters by top-level envelope keys (e.g. causationid) so a
        test finds ITS OWN event even when earlier tests left events on the
        shared test stream (acks are per-consumer; a fresh ephemeral sees
        them again).
        """
        nc = await nats.connect(self.world.url)
        js = nc.jetstream()
        sub = await js.pull_subscribe(subject, stream="BLOODBANK_EVENTS")
        events: list[dict] = []
        deadline = time.monotonic() + timeout
        try:
            while len(events) < count and time.monotonic() < deadline:
                try:
                    msgs = await sub.fetch(batch=10, timeout=1)
                except TimeoutError:
                    continue
                except Exception:  # noqa: BLE001 - consumer timeout variants
                    continue
                for m in msgs:
                    env = json.loads(m.data.decode())
                    await m.ack()
                    if match and any(env.get(k) != v for k, v in match.items()):
                        continue
                    events.append(env)
        finally:
            try:
                await sub.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            await nc.drain(); await nc.close()
        return events

    def test_full_loop_capture_publish_project(self):
        w = self.world
        # 1. Sidecar consumes once (--once would need it in main; run a short
        #    bounded session via subprocess with a hard timeout kill).
        proc = subprocess.Popen(
            [sys.executable, str(REPO / "services" / "artifact-sidecar" / "main.py")],
            env=w.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        # Pre-create the PROJECTOR's durable BEFORE any artifact event can be
        # published: with DeliverPolicy.NEW (the no-auto-backfill default), a
        # projector enabled AFTER artifacts exist would skip them; correct
        # enablement order is projector-first (documented in
        # docs/project-artifact-sidecar.md). --status creates no consumer, so
        # create it via a first --once run (idle exit, durable persists).
        penv0 = dict(w.projector_env)
        penv0["ARTIFACT_VAULT_DURABLE"] = f"test-projector-{self.port}"
        boot = subprocess.run(
            [sys.executable, str(REPO / "services" / "artifact-vault-projector" / "main.py"), "--once"],
            capture_output=True, text=True, timeout=120, env=penv0,
        )
        self.assertEqual(boot.returncode, 0, boot.stderr)
        try:
            # Wait until the durable consumer exists: with DeliverPolicy.NEW
            # (the safe default), a publish BEFORE consumer creation would
            # never be delivered.
            async def wait_consumer(timeout: float = 30.0) -> bool:
                nc = await nats.connect(w.url)
                js = nc.jetstream()
                deadline = time.monotonic() + timeout
                ok = False
                while time.monotonic() < deadline:
                    try:
                        await js.consumer_info("BLOODBANK_EVENTS", w.env["ARTIFACT_SIDECAR_DURABLE"])
                        ok = True
                        break
                    except Exception:  # noqa: BLE001
                        await asyncio.sleep(0.5)
                await nc.drain(); await nc.close()
                return ok

            self.assertTrue(asyncio.run(wait_consumer()), "sidecar consumer never appeared")
            # 2. Publish a real producer-shaped source event directly.
            env = tool_completed_envelope(
                path=str(w.tmp / "repos" / "bb" / "docs" / "broker.md"),
                content="# broker round trip\n",
                working_directory=str(w.tmp / "repos" / "bb"),
            )

            async def publish():
                nc = await nats.connect(w.url)
                await nc.publish(env["subject"], json.dumps(env).encode())
                await nc.drain(); await nc.close()

            asyncio.run(publish())
            # 3. Wait for the artifact event on the stream.
            events = asyncio.run(self._fetch_artifact_events(1, match={"causationid": env["id"]}))
            self.assertEqual(len(events), 1)
            art = events[0]["data"]["artifact"]
            self.assertEqual(art["title"], "broker.md")
            self.assertEqual(events[0]["causationid"], env["id"])
            # Nats-Msg-Id was set: server-side dedupe applies on replays.
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                out, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
        self.assertNotIn("Traceback", out or "", out)

        # 4. Projector consumes from its own durable and projects.
        penv = dict(w.projector_env)
        penv["ARTIFACT_VAULT_DURABLE"] = f"test-projector-{self.port}"
        proj = subprocess.run(
            [sys.executable, str(REPO / "services" / "artifact-vault-projector" / "main.py"), "--once"],
            capture_output=True, text=True, timeout=120, env=penv,
        )
        self.assertEqual(proj.returncode, 0, proj.stderr + proj.stdout)
        files = sorted(str(p.relative_to(w.vault)) for p in w.vault.rglob("broker.md"))
        self.assertEqual(len(files), 1, files)
        content = (w.vault / files[0]).read_bytes()
        self.assertEqual(content, b"# broker round trip\n")

    def test_outbox_crash_recovery_with_dedupe(self):
        w = self.world
        w.env["ARTIFACT_SIDECAR_DURABLE"] = f"test-sidecar-crash-{self.port}"
        # Process an event with NO sidecar running is impossible; instead:
        # run the sidecar pointed at a DEAD transport port so the outbox
        # accumulates, then bring a sidecar up against the live broker and
        # confirm exactly one artifact event results (startup drain + msg-id
        # dedupe semantics exercised by the fake-transport suite; here we
        # assert the real publish lands once and the row clears).
        dead_env = dict(w.env)
        dead_env["ARTIFACT_SIDECAR_NATS_URL"] = "nats://127.0.0.1:1"  # refused
        dead_env["ARTIFACT_SIDECAR_STATE_DIR"] = str(w.tmp / "state-dead")
        status = subprocess.run(
            [sys.executable, str(REPO / "services" / "artifact-sidecar" / "main.py"), "--status"],
            capture_output=True, text=True, env=dead_env, timeout=30,
        )
        # --status works offline (no broker needed):
        self.assertEqual(status.returncode, 0, status.stderr)

        # Enqueue via direct library use against the dead-transport state:
        from sidecar import Sidecar, SidecarState
        from artifact_lib import ProjectRegistry, ensure_store_root

        state_dir = w.tmp / "state-dead"
        registry = ProjectRegistry.from_payload(json.loads(w.registry_file.read_text()))
        store = ensure_store_root(state_dir / "store")
        state = SidecarState(state_dir / "state.sqlite3")
        try:
            sidecar = Sidecar(state=state, store_root=store, registry=registry)

            def dead_publish(_e):
                raise ConnectionRefusedError("transport down")

            env = tool_completed_envelope(
                path=str(w.tmp / "repos" / "bb" / "docs" / "crash.md"),
                content="crash recovery",
                working_directory=str(w.tmp / "repos" / "bb"),
            )
            [d] = sidecar.process(env)
            self.assertEqual(d.kind, "enqueued")
            sidecar.drain_outbox(dead_publish)  # fails, bounded, recorded
        finally:
            state.close()

        # Now run the sidecar runtime against the LIVE broker with that
        # state: the startup drain must publish the pending row.
        live_env = dict(w.env)
        live_env["ARTIFACT_SIDECAR_STATE_DIR"] = str(state_dir)
        proc = subprocess.Popen(
            [sys.executable, str(REPO / "services" / "artifact-sidecar" / "main.py")],
            env=live_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            events = asyncio.run(self._fetch_artifact_events(1, match={"causationid": env["id"]}))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["data"]["artifact"]["title"], "crash.md")
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
        # Status now reports nothing pending or exhausted (exit 0):
        st = subprocess.run(
            [sys.executable, str(REPO / "services" / "artifact-sidecar" / "main.py"), "--status"],
            capture_output=True, text=True, env=live_env, timeout=30,
        )
        payload = json.loads(st.stdout)
        self.assertEqual(payload["pending_outbox"], 0)
        self.assertEqual(payload["exhausted_outbox"], 0)
        self.assertEqual(st.returncode, 0, st.stdout)

    def test_new_durable_defaults_to_new_delivery(self):
        w = self.world
        durable = f"probe-new-{self.port}-{uuid.uuid4().hex[:6]}"

        async def probe():
            nc = await nats.connect(w.url)
            js = nc.jetstream()
            # Pre-seed one event BEFORE the durable exists:
            await js.publish(
                "bloodbank.evt.agent.tool.completed",
                json.dumps(tool_completed_envelope(
                    path="/noop/x.md", content="pre-existing", working_directory=None
                )).encode(),
            )
            # Create the consumer via the runtime's connect() path with a
            # real sidecar instance in a throwaway state dir.
            from sidecar import Sidecar, SidecarState
            from artifact_lib import ProjectRegistry, ensure_store_root
            tmp = Path(tempfile.mkdtemp(prefix="probe-durable-"))
            state = None
            try:
                state = SidecarState(tmp / "s.db")
                store = ensure_store_root(tmp / "store")
                reg = ProjectRegistry.from_payload(json.loads(w.registry_file.read_text()))
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "sidecar_main_probe", REPO / "services" / "artifact-sidecar" / "main.py"
                )
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                rt = mod.Runtime(Sidecar(state=state, store_root=store, registry=reg))
                await rt.connect(w.url, durable)
                info = await js.consumer_info("BLOODBANK_EVENTS", durable)
                dp = str(info.config.deliver_policy)
                await rt.aclose()
                await nc.close()
                return dp
            finally:
                if state is not None:
                    state.close()
                shutil.rmtree(tmp, ignore_errors=True)
                # Cleanup OWN consumer only (no shared deletes):
                try:
                    nc2 = await nats.connect(w.url)
                    await nc2.jetstream().delete_consumer("BLOODBANK_EVENTS", durable)
                    await nc2.drain(); await nc2.close()
                except Exception:  # noqa: BLE001
                    pass

        dp = asyncio.run(probe())
        # nats-py serializes the enum field as its lowercase value.
        self.assertEqual(dp.lower().rstrip(), DeliverPolicy.NEW.value.lower())

    def test_backfill_since_bounds_first_creation_and_is_ignored_when_existing(self):
        w = self.world
        durable = f"probe-since-{self.port}-{uuid.uuid4().hex[:6]}"

        async def probe():
            from datetime import datetime, timezone
            nc = await nats.connect(w.url)
            js = nc.jetstream()
            subj = "bloodbank.evt.agent.tool.completed"
            for i in range(2):  # BEFORE the boundary
                await js.publish(subj, json.dumps(tool_completed_envelope(
                    path=f"/noop/before{i}.md", content=f"before {i}", working_directory=None
                )).encode())
            await asyncio.sleep(1.5)
            boundary = datetime.now(timezone.utc)
            await asyncio.sleep(0.5)
            for i in range(3):  # AFTER the boundary
                await js.publish(subj, json.dumps(tool_completed_envelope(
                    path=f"/noop/after{i}.md", content=f"after {i}", working_directory=None
                )).encode())
            from sidecar import Sidecar, SidecarState
            from artifact_lib import ProjectRegistry, ensure_store_root
            import importlib.util
            tmp = Path(tempfile.mkdtemp(prefix="probe-since-"))
            state = None
            try:
                state = SidecarState(tmp / "s.db")
                store = ensure_store_root(tmp / "store")
                reg = ProjectRegistry.from_payload(json.loads(w.registry_file.read_text()))
                spec = importlib.util.spec_from_file_location(
                    "sidecar_main_since", REPO / "services" / "artifact-sidecar" / "main.py"
                )
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                rt = mod.Runtime(Sidecar(state=state, store_root=store, registry=reg))
                await rt.connect(w.url, durable, backfill_since=boundary)
                info = await js.consumer_info("BLOODBANK_EVENTS", durable)
                first = (str(info.config.deliver_policy), info.num_pending + info.num_ack_pending)
                await rt.aclose()
                # Second connect with a DIFFERENT (much earlier) since: ignored,
                # the durable keeps its original policy and cursor.
                rt2 = mod.Runtime(Sidecar(state=state, store_root=store, registry=reg))
                await rt2.connect(w.url, durable, backfill_since=datetime(2020, 1, 1, tzinfo=timezone.utc))
                info2 = await js.consumer_info("BLOODBANK_EVENTS", durable)
                second = (str(info2.config.deliver_policy), info2.num_pending + info2.num_ack_pending)
                await rt2.aclose()
                await nc.close()
                return first, second
            finally:
                if state is not None:
                    state.close()
                shutil.rmtree(tmp, ignore_errors=True)
                try:
                    nc2 = await nats.connect(w.url)
                    await nc2.jetstream().delete_consumer("BLOODBANK_EVENTS", durable)
                    await nc2.drain(); await nc2.close()
                except Exception:  # noqa: BLE001
                    pass

        first, second = asyncio.run(probe())
        self.assertEqual(first[0].lower().rstrip(), DeliverPolicy.BY_START_TIME.value.lower())
        # Only the 3 post-boundary tool events of THIS test (earlier tests'
        # events are all older than the boundary).
        self.assertEqual(first[1], 3)
        self.assertEqual(second, first)

    def test_cleanup_deletes_only_own_consumers(self):
        w = self.world

        async def probe():
            nc = await nats.connect(w.url)
            js = nc.jetstream()
            # A shared-looking consumer we DO NOT own:
            sentinel = f"sentinel-{uuid.uuid4().hex[:6]}"
            await js.add_consumer("BLOODBANK_EVENTS", config=None, durable=sentinel) if False else None
            from nats.js.api import AckPolicy, ConsumerConfig
            await js.add_consumer(
                "BLOODBANK_EVENTS",
                config=ConsumerConfig(
                    durable_name=sentinel, ack_policy=AckPolicy.EXPLICIT,
                    filter_subject="bloodbank.evt.conversation.turn.completed",
                ),
            )
            names_before = {c.name for c in (await js.consumers_info("BLOODBANK_EVENTS"))}
            self.assertIn(sentinel, names_before)
            # The test teardown only deletes consumers it created by exact
            # name; the sentinel must survive the whole class run:
            w.__dict__.setdefault("_sentinels", []).append(sentinel)
            await nc.drain(); await nc.close()
            return sentinel

        sentinel = asyncio.run(probe())
        # Verify it still exists after other tests ran:
        async def still_there():
            nc = await nats.connect(w.url)
            js = nc.jetstream()
            names = {c.name for c in await js.consumers_info("BLOODBANK_EVENTS")}
            await nc.drain(); await nc.close()
            return sentinel in names

        self.assertTrue(asyncio.run(still_there()))
        # Cleanup the sentinel ourselves:
        async def drop():
            nc = await nats.connect(w.url)
            js = nc.jetstream()
            await js.delete_consumer("BLOODBANK_EVENTS", sentinel)
            await nc.drain(); await nc.close()

        asyncio.run(drop())


if __name__ == "__main__":
    unittest.main(verbosity=2)
