"""Offline contract/race model; fake receipts, scratch source and SQLite only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

import main
import tolt_family as family
import lowwater_ownership as owner
from test_tolt_family import Clock, FakePost, ROOT
from tolt_contract_fixtures import envelope_for
from core.validate import game_event_id, game_ordering_key

BARRIER = "b614d990-146c-4cd5-8442-c6a643149631"
LEGACY = "70f28232-7cc8-41a8-b251-188034cf2628"


def event_for(episode=None):
    e = envelope_for("low_water")
    e.update(source="urn:33god:cli:ltf-concept-alerts", producer="ltf-concept-alerts",
             service="lost-things-forge", actor={"type": "service", "agent_id": "ltf-concept-alerts"})
    e["data"]["project_slug"] = owner.PROJECT
    e["data"]["episode_id"] = episode or e["data"]["episode_id"]
    e["data"]["provenance"]["record_ref"] = (
        "repo://lost-things-forge/controller/concept-low-water/" + e["data"]["episode_id"])
    e["time"] = e["data"]["observed_at"]
    e["id"] = game_event_id(e["type"], e["data"])
    e["ordering_key"] = game_ordering_key(e["type"], e["data"])
    return e


def handoff_for(e, destination="https://ntfy.delo.sh/tolt"):
    body = owner.canonical(e)
    sha = hashlib.sha256(body.encode()).hexdigest()
    grant = {"schema": "bloodbank.tolt-lowwater-grant/1", "grant_id": "",
             "fence_id": owner.fence_id(e["data"]["episode_id"]), "barrier_id": BARRIER,
             "admission_barrier_id": BARRIER, "project_slug": owner.PROJECT, "pool_id": owner.POOL,
             "episode_id": e["data"]["episode_id"], "event_id": e["id"], "destination": destination,
             "observed_at": e["time"], "eligible_count": e["data"]["eligible_count"],
             "envelope_sha256": sha, "fence": {"owner": "bloodbank", "direct_state": "pending",
                                              "never_attempted": True, "permanent": True}}
    grant["grant_id"] = owner.grant_id(grant)
    return {"schema": "bloodbank.tolt-lowwater-handoff/1", "grant": grant, "envelope_body": body,
            "publication": {"kind": "jetstream_puback", "stream": "BLOODBANK_EVENTS", "sequence": 1,
                            "message_id": e["id"], "subject": e["subject"], "envelope_sha256": sha}}


def save_json(path, value):
    path.write_text(owner.canonical(value))
    path.chmod(0o600)


class SourceRaceModel:
    """Specification model only. Forge's owner must implement its actual lock/fence."""
    def __init__(self, root):
        self.root = root
        self.path = root / "source.json"
        if not self.path.exists():
            save_json(self.path, {"barrier": None, "episodes": {}})

    def change(self, operation):
        with (self.root / "source.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self.path.read_text())
            result = operation(state)
            temp = self.root / "source.next"
            with temp.open("w") as f:
                f.write(owner.canonical(state))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
            fd = os.open(self.root, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return result

    def barrier(self):
        def activate(s):
            s["barrier"] = {"id": BARRIER, "excluded": sorted(s["episodes"])}
        self.change(activate)

    def birth(self, eid, state="pending", attempted=False):
        def create(s):
            s["episodes"][eid] = {"state": state, "attempted": attempted, "owner": "direct",
                                  "admitted": s["barrier"]["id"] if s["barrier"] else None}
        self.change(create)

    def direct(self, eid):
        def claim(s):
            e = s["episodes"][eid]
            if e["owner"] != "direct" or e["state"] != "pending":
                return False
            e.update(state="sending", attempted=True)
            return True
        return self.change(claim)

    def transfer(self, eid):
        def fence(s):
            e = s["episodes"][eid]
            if e["owner"] == "bloodbank":
                return True
            if (not s["barrier"] or e["admitted"] != s["barrier"]["id"]
                    or eid in s["barrier"]["excluded"] or e["state"] != "pending" or e["attempted"]):
                return False
            e["owner"] = "bloodbank"  # Durably saved before caller may export or publish.
            return True
        return self.change(fence)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bb34-owner-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / "control"
        self.control.mkdir(mode=0o700)
        (self.control / "grants").mkdir(mode=0o700)
        self.barrier = {"schema": "bloodbank.tolt-lowwater-barrier/1", "barrier_id": BARRIER,
                        "project_slug": owner.PROJECT, "pool_id": owner.POOL,
                        "destination": "https://ntfy.delo.sh/tolt", "excluded_episode_ids": [LEGACY]}
        save_json(self.control / "barrier.json", self.barrier)
        self.cfg = family.Config(enabled=True, cutover_accepted=True, state_path=self.root / "ledger.sqlite3",
                                 validator_root=ROOT, ownership_dir=self.control, ownership_uid=os.getuid(),
                                 retry_base=1, lease_seconds=5, post_timeout=0.5)
        self.clock, self.post = Clock(), FakePost()
        self.validator = family.CanonicalValidator(ROOT)
        self.event = event_for()
        self.handoff = handoff_for(self.event)

    def route(self, config=None, post=None):
        cfg = config or self.cfg
        return family.FamilyRoute(cfg, family.DeliveryLedger(cfg.state_path),
                                  self.validator, post or self.post, self.clock)

    def export(self, value=None):
        value = value or self.handoff
        name = value["grant"]["grant_id"] + ".json"
        save_json(self.control / "grants" / name, value)
        return name

    def handle(self, route, event=None):
        event = event or self.event
        return asyncio.run(route.handle(event, event["subject"]))

    def test_event_before_grant_waits_then_import_drains_once(self):
        r = self.route()
        self.assertEqual(self.handle(r), "family-pending")
        self.assertEqual(r.ledger.get(self.event["id"], self.cfg.destination)["reason"], owner.WAITING)
        self.assertEqual(self.post.raw, [])
        self.export()
        asyncio.run(r.drain())
        self.assertEqual(len(self.post.raw), 1)
        self.assertEqual(self.handle(r), "family-duplicate")

    def test_grant_before_event_and_missed_core_delivery_recover_without_publish(self):
        self.export()
        r = self.route()
        acks = r.ownership.scan(10)
        self.assertEqual(acks[0]["status"], "accepted")
        self.assertTrue(acks[0]["durable_import"])
        self.assertEqual(self.post.raw, [])  # Import ACK is not HTTP.
        asyncio.run(self.route().drain())
        self.assertEqual(len(self.post.raw), 1)
        self.assertEqual(self.handle(r), "family-duplicate")

    def test_restart_after_import_before_post_and_lost_import_ack(self):
        name = self.export()
        self.route().ownership.import_file(name)  # Caller loses ACK; fence must remain.
        ack = self.route().ownership.import_file(name)
        self.assertEqual(ack["status"], "duplicate")
        asyncio.run(self.route().drain())
        asyncio.run(self.route().drain())
        self.assertEqual(len(self.post.raw), 1)

    def test_multiple_router_instances_and_importers_have_one_http_claim(self):
        self.export()
        first, second = self.route(), self.route()
        with ThreadPoolExecutor(2) as pool:
            acks = list(pool.map(lambda r: r.ownership.scan(10)[0]["status"], (first, second)))
        self.assertEqual(sorted(acks), ["accepted", "duplicate"])
        async def concurrent():
            return await asyncio.gather(first.deliver(self.event["id"]), second.deliver(self.event["id"]))
        asyncio.run(concurrent())
        self.assertEqual(len(self.post.raw), 1)

    def test_uncertain_http_ack_loss_never_retries_after_restart(self):
        self.export()
        self.post.replies = [TimeoutError("synthetic lost ACK")]
        asyncio.run(self.route().drain())
        r = self.route()
        self.clock.now += 1000
        asyncio.run(r.drain())
        self.assertEqual(r.ledger.get(self.event["id"], self.cfg.destination)["state"], "uncertain")
        self.assertEqual(len(self.post.raw), 1)

    def test_crash_after_http_claim_expires_uncertain_not_new_attempt(self):
        name = self.export()
        r = self.route()
        r.ownership.import_file(name)
        r.ledger.claim(self.event["id"], self.cfg.destination, self.clock(), self.cfg)
        self.clock.now += 10
        asyncio.run(self.route().drain())
        row = r.ledger.get(self.event["id"], self.cfg.destination)
        self.assertEqual((row["state"], row["attempts"]), ("uncertain", 1))
        self.assertEqual(self.post.raw, [])

    def test_known_preaccept_retry_preserves_exact_request_bytes(self):
        self.export()
        self.post.replies = [family.Receipt(429, preaccept=True), family.Receipt(200, "synthetic")]
        r = self.route()
        asyncio.run(r.drain())
        self.clock.now += 2
        asyncio.run(self.route().drain())
        self.assertEqual(len(self.post.raw), 2)
        self.assertEqual(self.post.raw[0], self.post.raw[1])

    def test_missing_barrier_and_missing_grant_are_recoverable_no_http(self):
        (self.control / "barrier.json").unlink()
        r = self.route()
        self.assertEqual(self.handle(r), "family-pending")
        asyncio.run(r.drain())
        self.assertEqual(self.post.raw, [])
        save_json(self.control / "barrier.json", self.barrier)
        self.export()
        asyncio.run(self.route().drain())
        self.assertEqual(len(self.post.raw), 1)

    def test_corrupt_grant_and_unavailable_after_import_do_not_post(self):
        name = self.export()
        r = self.route()
        r.ownership.import_file(name)
        path = self.control / "grants" / name
        path.unlink()
        self.assertEqual(asyncio.run(r.deliver(self.event["id"])), "family-pending")
        path.write_text("{bad")
        path.chmod(0o600)
        self.assertEqual(asyncio.run(r.deliver(self.event["id"])), "family-pending")
        self.assertEqual(self.post.raw, [])

    def test_stale_or_unfenced_grant_is_rejected_with_durable_ack(self):
        for field in ("barrier_id", "admission_barrier_id"):
            h = json.loads(json.dumps(self.handoff))
            h["grant"][field] = str(uuid.uuid4())
            h["grant"]["grant_id"] = owner.grant_id(h["grant"])
            ack = self.route().ownership.import_file(self.export(h))
            self.assertEqual(ack["status"], "rejected")
        h = json.loads(json.dumps(self.handoff))
        h["grant"]["fence"]["never_attempted"] = False
        self.assertEqual(self.route().ownership.import_file(self.export(h))["status"], "rejected")
        self.assertEqual(self.post.raw, [])

    def test_historical_delivered_episode_cannot_be_imported_or_renotified(self):
        e = event_for(LEGACY)
        h = handoff_for(e)
        r = self.route()
        self.assertEqual(self.handle(r, e), "family-suppressed")
        ack = r.ownership.import_file(self.export(h))
        self.assertEqual(ack["status"], "rejected")
        asyncio.run(self.route().drain())
        self.assertEqual(self.post.raw, [])

    def test_old_suppressed_row_is_never_promoted_by_later_valid_grant(self):
        r = self.route()
        request = family.format_message(self.event, self.cfg)
        r.ledger.offer(self.event, request, self.cfg.destination, "lowwater-owner-episode-gate-unavailable")
        ack = r.ownership.import_file(self.export())
        self.assertEqual(ack["status"], "rejected")
        self.assertEqual(self.handle(self.route()), "family-suppressed")
        asyncio.run(self.route().drain())
        self.assertEqual(self.post.raw, [])

    def test_preexisting_pending_is_legacy_suppressed_even_with_gates_closed(self):
        r = self.route()
        r.ledger.offer(self.event, family.format_message(self.event, self.cfg), self.cfg.destination)
        asyncio.run(self.route(replace(self.cfg, enabled=False)).drain())
        self.assertEqual(r.ledger.get(self.event["id"], self.cfg.destination)["state"], "suppressed")
        ack = r.ownership.import_file(self.export())
        self.assertEqual(ack["status"], "rejected")

    def test_changed_event_bytes_same_semantic_id_is_persistent_conflict(self):
        r = self.route()
        self.handle(r)
        self.export()
        r.ownership.scan(10)
        e = json.loads(json.dumps(self.event))
        e["data"]["eligible_count"] = 1
        h = handoff_for(e)
        ack = r.ownership.import_file(self.export(h))
        self.assertEqual(ack["status"], "conflict")
        asyncio.run(self.route().drain())
        self.assertEqual(r.ledger.get(e["id"], self.cfg.destination)["state"], "conflict")
        self.assertEqual(self.post.raw, [])

    def test_changed_grant_or_publication_under_same_id_conflicts_after_restart(self):
        name = self.export()
        r = self.route()
        r.ownership.import_file(name)
        self.handoff["publication"]["sequence"] = 2
        self.export()
        ack = self.route().ownership.import_file(name)
        self.assertEqual(ack["status"], "conflict")
        self.export(handoff_for(self.event))
        asyncio.run(self.route().drain())
        self.assertEqual(self.post.raw, [])

    def test_changed_barrier_is_permanent_conflict_not_rotation_or_retarget(self):
        self.route().ownership.import_file(self.export())
        original = dict(self.barrier)
        self.barrier["barrier_id"] = str(uuid.uuid4())
        save_json(self.control / "barrier.json", self.barrier)
        asyncio.run(self.route().drain())
        save_json(self.control / "barrier.json", original)
        asyncio.run(self.route().drain())
        self.assertEqual(self.post.raw, [])
        self.assertEqual(self.route().ledger.get(self.event["id"], self.cfg.destination)["state"], "conflict")

    def test_event_destination_is_pinned_across_configuration_change(self):
        self.handle(self.route())
        r = self.route(replace(self.cfg, topic="other-tolt"))
        self.assertEqual(self.handle(r), "family-conflict")
        asyncio.run(self.route().drain())
        self.assertEqual(self.post.raw, [])

    def test_retargeted_trusted_grant_conflict_is_durable_even_when_binding_invalid(self):
        r = self.route()
        r.ownership.import_file(self.export())
        h = handoff_for(self.event, "https://ntfy.delo.sh/other-tolt")
        self.assertEqual(r.ownership.import_file(self.export(h))["status"], "conflict")
        asyncio.run(self.route().drain())
        self.assertEqual(self.post.raw, [])

    def test_atomic_http_claim_rechecks_registered_authority(self):
        r = self.route()
        self.handle(r)  # Waiting is not an HTTP permission.
        self.assertIsNone(r.ledger.claim(self.event["id"], self.cfg.destination, self.clock(), self.cfg))
        r.ownership.import_file(self.export())
        self.assertTrue(r.ownership.permits(self.event["id"]))
        with r.ledger.connection(True) as db:
            db.execute("UPDATE lowwater_grant SET status='conflict' WHERE event_id=?", (self.event["id"],))
        self.assertIsNone(r.ledger.claim(self.event["id"], self.cfg.destination, self.clock(), self.cfg))
        self.assertEqual(self.post.raw, [])

    def test_protocol_known_vector_derivations_are_stable(self):
        g = self.handoff["grant"]
        self.assertEqual(str(owner.NAMESPACE), "b23a872e-915f-5e6a-b5e8-8659dd54aa67")
        self.assertEqual(self.event["id"], "811cf31c-af98-5062-ab60-5d466964e7d3")
        self.assertEqual(g["fence_id"], "08afa085-e0b8-5c0b-a019-d19cc16f8db1")
        self.assertEqual(g["grant_id"], "7000c6ed-d18a-5f0c-bb0b-dbcd074cbe65")
        self.assertEqual(owner.digest(self.handoff), "fa7f9ac331b9d5450d0ed6b08d99759645d9d34b6cc8370fff8a15116edf3fe9")
        self.assertEqual(owner.identity(["ack", g["grant_id"], owner.digest(self.handoff),
                                        "accepted", "durable-import-only"]),
                         "ee85944b-bcf1-59b0-a6a4-8aaed1f0fe4b")

    def test_gate_disabled_import_remains_suppressed_when_enabled_later(self):
        for field in ("enabled", "cutover_accepted"):
            cfg = replace(self.cfg, **{field: False}, state_path=self.root / (field + ".db"))
            r = self.route(cfg)
            self.assertEqual(r.ownership.import_file(self.export())["status"], "rejected")
            enabled = self.route(replace(cfg, enabled=True, cutover_accepted=True))
            asyncio.run(enabled.drain())
            self.assertEqual(self.handle(enabled), "family-suppressed")
        self.assertEqual(self.post.raw, [])

    def test_untrusted_event_grant_field_does_not_authorize(self):
        self.event["data"]["grant"] = self.handoff["grant"]
        self.assertEqual(self.handle(self.route()), "family-invalid")
        self.assertEqual(self.post.raw, [])

    def test_source_identity_must_be_actual_forge_not_fixture_or_job(self):
        self.event["producer"] = "ltf-job"
        self.assertEqual(self.handle(self.route()), "family-suppressed")
        self.assertEqual(self.post.raw, [])

    def test_wrong_uid_group_writable_symlink_and_duplicate_json_fail_closed(self):
        name = self.export()
        bad = self.route(replace(self.cfg, ownership_uid=os.getuid() + 1))
        self.assertEqual(bad.ownership.import_file(name)["status"], "rejected")
        path = self.control / "grants" / name
        path.chmod(0o660)
        self.assertEqual(self.route().ownership.import_file(name)["status"], "rejected")
        path.unlink()
        target = self.root / "untrusted.json"
        save_json(target, self.handoff)
        path.symlink_to(target)
        self.assertEqual(self.route().ownership.import_file(name)["status"], "rejected")
        path.unlink()
        path.write_text('{"schema":"x","schema":"y"}')
        path.chmod(0o600)
        self.assertEqual(self.route().ownership.import_file(name)["status"], "rejected")

    def test_publication_proof_is_required_not_import_ack_invented_puback(self):
        h = json.loads(json.dumps(self.handoff))
        h.pop("publication")
        self.assertEqual(self.route().ownership.import_file(self.export(h))["status"], "rejected")
        self.assertEqual(self.post.raw, [])

    def test_envelope_hash_count_time_and_admission_bindings_cannot_drift(self):
        for field, value in (("eligible_count", 1), ("observed_at", "2026-11-08T00:00:00Z"),
                             ("envelope_sha256", "0" * 64), ("fence_id", str(uuid.uuid5(uuid.NAMESPACE_URL, "bad")))):
            with self.subTest(field=field):
                h = json.loads(json.dumps(self.handoff))
                h["grant"][field] = value
                h["grant"]["grant_id"] = owner.grant_id(h["grant"])
                self.assertEqual(self.route().ownership.import_file(self.export(h))["status"], "rejected")
        self.assertEqual(self.post.raw, [])

    def test_readback_proof_works_but_wrong_stream_or_message_id_cannot(self):
        name = self.export()
        self.handoff["publication"]["kind"] = "jetstream_readback"
        self.export()
        self.assertEqual(self.route().ownership.import_file(name)["status"], "accepted")
        self.handoff["publication"]["stream"] = "NOT_BLOODBANK"
        self.export()
        self.assertEqual(self.route().ownership.import_file(name)["status"], "conflict")
        self.assertEqual(self.post.raw, [])

    def test_validator_unavailable_cannot_import_a_trusted_file(self):
        name = self.export()
        r = self.route()
        r.ownership.validator = None
        self.assertEqual(r.ownership.import_file(name)["status"], "rejected")
        self.assertEqual(self.post.raw, [])

    def test_local_closed_schema_and_ack_readback(self):
        name = self.export()
        r = self.route()
        ack = r.ownership.import_file(name)
        import jsonschema
        schema = json.loads(Path(owner.__file__).with_name("lowwater_control.schema.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(ack, {**schema, "$ref": "#/$defs/ack"})
        self.assertEqual(r.ownership.status(self.handoff["grant"]["grant_id"])["ownership_status"], "accepted")
        h = json.loads(json.dumps(self.handoff))
        h["extra"] = "not-allowed"
        self.assertEqual(r.ownership.import_file(self.export(h))["status"], "conflict")

    def test_router_uses_exactly_one_family_disposition_without_mirrors(self):
        self.export()
        r = self.route()
        r.ownership.scan(10)
        async def generic(*args):
            self.fail("no generic route or mirror")
        router = main.NtfyRouter(main.Policy(mute=(), digest=()), generic, main.TokenBucket(0, 1), family=r)
        self.assertEqual(asyncio.run(router.handle(self.event, self.event["subject"])), "family-accepted")
        self.assertEqual(len(self.post.raw), 1)
        self.assertEqual(self.post.messages[0]["topic"], "tolt")
        self.assertEqual(self.post.messages[0]["priority"], 4)

    def test_bounded_import_cursor_does_not_starve_later_grants(self):
        for _ in range(3):
            self.export(handoff_for(event_for(str(uuid.uuid4()))))
        r = self.route(replace(self.cfg, drain_limit=1))
        for _ in range(3):
            asyncio.run(r.drain())
        self.assertEqual(len(self.post.raw), 3)

    def test_fair_delivery_after_saturated_holds_both_orders_and_restart(self):
        for limit in (1, self.cfg.drain_limit):
            for blocked in ("awaiting", "imported-missing", "imported-corrupt"):
                for order in ("event-first", "grant-first"):
                    with self.subTest(limit=limit, blocked=blocked, order=order):
                        f = OwnershipTests()
                        f.setUp()
                        self.addCleanup(f.doCleanups)
                        cfg = replace(f.cfg, drain_limit=limit)
                        r = f.route(cfg)
                        holds = []
                        for _ in range(limit):
                            e = event_for(str(uuid.uuid4()))
                            if blocked == "awaiting":
                                self.assertEqual(f.handle(r, e), "family-pending")
                            else:
                                name = f.export(handoff_for(e))
                                self.assertEqual(r.ownership.import_file(name)["status"], "accepted")
                                path = f.control / "grants" / name
                                if blocked == "imported-missing":
                                    path.unlink()
                                else:
                                    path.write_text("{invalid")
                            holds.append(r.ledger.get(e["id"], cfg.destination))
                        later = event_for(str(uuid.uuid4()))
                        if order == "event-first":
                            self.assertEqual(f.handle(r, later), "family-pending")
                        self.assertEqual(r.ownership.import_file(
                            f.export(handoff_for(later)))["status"], "accepted")
                        # No core callback for the grant-first case: recovery must drain it.
                        other = envelope_for("item")
                        r.ledger.offer(other, family.format_message(other, cfg), cfg.destination)
                        self.assertEqual(f.post.raw, [])
                        for _ in range((limit + 2 + limit - 1) // limit + 1):
                            before = len(f.post.raw)
                            asyncio.run(f.route(cfg).drain())  # Restart every invocation.
                            self.assertLessEqual(len(f.post.raw) - before, limit)
                        self.assertEqual(len(f.post.raw), 2)
                        for event in (later, other):
                            self.assertEqual(r.ledger.get(event["id"], cfg.destination)["state"], "accepted")
                        self.assertEqual(f.handle(f.route(cfg), later), "family-duplicate")
                        self.assertEqual(len(f.post.raw), 2)
                        for original in holds:
                            current = r.ledger.get(original["event_id"], cfg.destination)
                            self.assertEqual((current["state"], current["attempts"]), ("pending", 0))
                            for key in ("envelope_json", "request_json", "digest"):
                                self.assertEqual(current[key], original[key])

    def test_delivery_cursor_finishes_finite_round_despite_new_tail_arrivals(self):
        r = self.route()
        initial = [event_for(str(uuid.uuid4())) for _ in range(3)]
        for e in initial:
            self.handle(r, e)
        for expected in initial:
            selected = self.route().ledger.due(self.cfg.destination, self.clock(), 1)
            self.assertEqual([row["event_id"] for row in selected], [expected["id"]])
            self.handle(r, event_for(str(uuid.uuid4())))
        selected = self.route().ledger.due(self.cfg.destination, self.clock(), 1)
        self.assertEqual(selected[0]["event_id"], initial[0]["id"])
        self.assertEqual(self.post.raw, [])

    def test_barrier_destination_change_restore_restart_is_permanent_conflict(self):
        r = self.route()
        r.ownership.import_file(self.export())
        changed = dict(self.barrier, destination="https://ntfy.delo.sh/other-tolt")
        save_json(self.control / "barrier.json", changed)
        self.assertFalse(r.ownership.permits(self.event["id"]))
        save_json(self.control / "barrier.json", self.barrier)
        restarted = self.route()
        self.assertEqual(asyncio.run(restarted.deliver(self.event["id"])), "family-conflict")
        self.assertEqual(self.post.raw, [])
        self.assertEqual(restarted.ownership.status(self.handoff["grant"]["grant_id"])[
            "ownership_status"], "conflict")
        with restarted.ledger.connection() as db:
            barrier = db.execute("SELECT * FROM lowwater_barrier").fetchone()
            self.assertEqual(barrier["state"], "conflict")
            self.assertEqual(barrier["digest"], owner.digest(self.barrier))
            self.assertEqual(db.execute("SELECT count(*) FROM lowwater_conflict "
                                        "WHERE scope='barrier' AND rejected_digest=?",
                                        (owner.digest(changed),)).fetchone()[0], 1)

    def test_untrusted_unparseable_and_shape_invalid_barrier_do_not_latch_bytes(self):
        r = self.route()
        r.ownership.import_file(self.export())
        path = self.control / "barrier.json"
        path.write_text("{invalid")
        self.assertFalse(r.ownership.permits(self.event["id"]))
        save_json(path, dict(self.barrier, destination=3))
        self.assertFalse(r.ownership.permits(self.event["id"]))
        save_json(path, dict(self.barrier, destination="https://ntfy.delo.sh/other-tolt"))
        path.chmod(0o660)
        self.assertFalse(r.ownership.permits(self.event["id"]))
        save_json(path, self.barrier)
        self.assertTrue(self.route().ownership.permits(self.event["id"]))
        with r.ledger.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lowwater_conflict").fetchone()[0], 0)
        self.assertEqual(self.post.raw, [])

    def test_first_mismatching_destination_barrier_is_not_pinned(self):
        r = self.route(replace(self.cfg, topic="other-tolt"))
        with self.assertRaises(owner.ControlUnavailable):
            r.ownership.barrier()
        with r.ledger.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lowwater_barrier").fetchone()[0], 0)
        self.assertEqual(self.route().ownership.barrier(), self.barrier)

    def test_repeat_ack_exactly_matches_stored_body_before_after_delivery_restart(self):
        r = self.route()
        name = self.export()
        accepted = r.ownership.import_file(name)
        before = r.ownership.import_file(name)
        self.assertEqual(before["delivery_state"], "pending")
        asyncio.run(r.deliver(self.event["id"]))
        restarted = self.route()
        after = restarted.ownership.import_file(name)
        self.assertEqual(after, before)
        for ack in (accepted, before, after):
            with restarted.ledger.connection() as db:
                stored = json.loads(db.execute("SELECT body FROM lowwater_ack WHERE ack_id=?",
                                               (ack["ack_id"],)).fetchone()["body"])
            self.assertEqual(ack, stored)
        status = restarted.ownership.status(self.handoff["grant"]["grant_id"])
        self.assertEqual(status["import_ack"], before)
        self.assertEqual(status["delivery"]["state"], "accepted")
        self.assertEqual(len(self.post.raw), 1)

    def _offline_ownership_cli(self, fixture, args, limit=None):
        env = {**os.environ, "NTFY_TOLT_ENABLED": "true", "NTFY_TOLT_CUTOVER_ACCEPTED": "true",
               "NTFY_TOLT_OWNERSHIP_DIR": str(fixture.control), "NTFY_TOLT_OWNERSHIP_UID": str(os.getuid()),
               "NTFY_TOLT_STATE": str(fixture.cfg.state_path), "NTFY_FAMILY_VALIDATOR_ROOT": str(ROOT),
               "BLOODBANK_SCHEMAS_DIR": str(ROOT / "schemas")}
        entry = ("import runpy,socket,sys; "
                 "socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('offline only')); ")
        if limit is not None:
            # Exercise the real CLI in a child process with a smaller test-only Config.
            # The default10 case below does not override the production CLI configuration.
            entry += ("import tolt_family as f; from dataclasses import replace; "
                      "load=f.Config.from_env; "
                      f"f.Config.from_env=classmethod(lambda cls: replace(load(),drain_limit={limit})); ")
        entry += (f"sys.argv=[{owner.__file__!r},*sys.argv[1:]]; "
                  f"runpy.run_path({owner.__file__!r},run_name='__main__')")
        result = subprocess.run([sys.executable, "-B", "-c", entry, *args], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_actual_fresh_cli_advances_past_retained_page1_and_default10(self):
        for limit in (1, self.cfg.drain_limit):
            with self.subTest(limit=limit):
                f = OwnershipTests()
                f.setUp()
                self.addCleanup(f.doCleanups)
                handoffs = sorted([handoff_for(event_for(str(uuid.uuid4()))) for _ in range(limit + 1)],
                                  key=lambda h: h["grant"]["grant_id"])
                for h in handoffs[:limit]:
                    f.export(h)
                override = 1 if limit == 1 else None
                initial = self._offline_ownership_cli(f, ["--import-pending"], override)
                self.assertEqual(len(initial), limit)
                self.assertTrue(all(ack["status"] == "accepted" for ack in initial))
                f.export(handoffs[-1])
                original_files = {p.name: p.read_bytes() for p in (f.control / "grants").iterdir()}
                for _ in range(4):
                    page = self._offline_ownership_cli(f, ["--import-pending"], override)
                    self.assertLessEqual(len(page), limit)
                status = self._offline_ownership_cli(
                    f, ["--status", handoffs[-1]["grant"]["grant_id"]], override)
                self.assertEqual(status["ownership_status"], "accepted")
                self.assertEqual(status["delivery"]["attempts"], 0)
                self.assertEqual(f.post.raw, [])
                self.assertEqual(original_files, {
                    p.name: p.read_bytes() for p in (f.control / "grants").iterdir()})

    def test_import_restart_every_drain_passes_delivered_retained_page(self):
        for limit in (1, self.cfg.drain_limit):
            with self.subTest(limit=limit):
                f = OwnershipTests()
                f.setUp()
                self.addCleanup(f.doCleanups)
                cfg = replace(f.cfg, drain_limit=limit)
                handoffs = sorted([handoff_for(event_for(str(uuid.uuid4()))) for _ in range(limit + 1)],
                                  key=lambda h: h["grant"]["grant_id"])
                for h in handoffs[:limit]:
                    f.export(h)
                asyncio.run(f.route(cfg).drain())
                self.assertEqual(len(f.post.raw), limit)
                f.export(handoffs[-1])  # Missed core callback: only retained import can recover.
                for _ in range(4):
                    asyncio.run(f.route(cfg).drain())
                self.assertEqual(len(f.post.raw), limit + 1)
                self.assertEqual(f.route(cfg).ownership.status(
                    handoffs[-1]["grant"]["grant_id"])["ownership_status"], "accepted")
                self.assertEqual(len(list((f.control / "grants").iterdir())), limit + 1)

    def test_import_round_survives_midscan_crash_before_import_without_losing_files(self):
        handoffs = sorted([handoff_for(event_for(str(uuid.uuid4()))) for _ in range(2)],
                          key=lambda h: h["grant"]["grant_id"])
        for h in handoffs:
            self.export(h)
        r = self.route()
        with patch.object(r.ownership, "import_file", side_effect=RuntimeError("synthetic crash after reservation")):
            with self.assertRaises(RuntimeError):
                r.ownership.scan(1)
        next_page = self.route().ownership.scan(1)
        self.assertEqual(next_page[0]["grant_id"], handoffs[1]["grant"]["grant_id"])
        recovered = self.route().ownership.scan(1)
        self.assertEqual(recovered[0]["grant_id"], handoffs[0]["grant"]["grant_id"])
        self.assertTrue(all(self.route().ownership.status(h["grant"]["grant_id"])[
            "ownership_status"] == "accepted" for h in handoffs))
        self.assertEqual(len(list((self.control / "grants").iterdir())), 2)
        self.assertEqual(self.post.raw, [])

    def test_concurrent_fresh_cli_readers_reserve_different_import_pages(self):
        handoffs = [handoff_for(event_for(str(uuid.uuid4()))) for _ in range(3)]
        for h in handoffs:
            self.export(h)
        self.route()  # Existing router volume/schema; exercise concurrent readers, not DDL bootstrap.
        with ThreadPoolExecutor(3) as pool:
            pages = list(pool.map(lambda _: self._offline_ownership_cli(
                self, ["--import-pending"], 1), range(3)))
        ids = [page[0]["grant_id"] for page in pages]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(set(ids), {h["grant"]["grant_id"] for h in handoffs})
        self.assertTrue(all(page[0]["status"] == "accepted" for page in pages))
        self.assertEqual(len(list((self.control / "grants").iterdir())), 3)
        self.assertEqual(self.post.raw, [])

    def test_import_malformed_first_file_cannot_starve_and_retained_files_revalidate(self):
        bad = self.control / "grants/!.json"
        bad.write_text("{invalid")
        bad.chmod(0o600)
        handoffs = sorted([handoff_for(event_for(str(uuid.uuid4()))) for _ in range(2)],
                          key=lambda h: h["grant"]["grant_id"])
        for h in handoffs:
            self.export(h)
        statuses = [self.route().ownership.scan(1)[0]["status"] for _ in range(3)]
        self.assertEqual(statuses, ["rejected", "accepted", "accepted"])
        handoffs[0]["publication"]["sequence"] = 2
        self.export(handoffs[0])
        self.route().ownership.scan(1)  # The retained malformed prefix is still rejected.
        changed = self.route().ownership.scan(1)[0]
        self.assertEqual(changed["status"], "conflict")
        self.assertEqual(self.route().ownership.status(
            handoffs[0]["grant"]["grant_id"])["ownership_status"], "conflict")
        self.assertEqual(len(list((self.control / "grants").iterdir())), 3)
        self.assertEqual(self.post.raw, [])

    def test_import_finite_snapshot_defers_new_lexically_first_files(self):
        handoffs = sorted([handoff_for(event_for(str(uuid.uuid4()))) for _ in range(2)],
                          key=lambda h: h["grant"]["grant_id"])
        for h in handoffs:
            self.export(h)
        first = self.route().ownership.scan(1)[0]
        self.assertEqual(first["grant_id"], handoffs[0]["grant"]["grant_id"])
        new = self.control / "grants/!.json"
        new.write_text("{invalid")
        new.chmod(0o600)
        second = self.route().ownership.scan(1)[0]
        self.assertEqual(second["grant_id"], handoffs[1]["grant"]["grant_id"])
        self.assertEqual(self.route().ownership.scan(1)[0]["status"], "rejected")
        self.assertEqual(len(list((self.control / "grants").iterdir())), 3)
        self.assertEqual(self.post.raw, [])

    def test_concurrent_barrier_conflict_defeats_new_and_existing_grant_acceptance(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                f = OwnershipTests()
                f.setUp()
                self.addCleanup(f.doCleanups)
                observer = f.route()
                original_ack = observer.ownership.import_file(f.export())
                event = f.event if existing else event_for(str(uuid.uuid4()))
                handoff = handoff_for(event)
                name = f.export(handoff)
                entered, release = threading.Event(), threading.Event()
                def pause(value, config):
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError("scratch race synchronization timed out")
                    return family.format_message(value, config)
                importer = f.route()
                importer.ownership.formatter = pause
                with ThreadPoolExecutor(1) as pool:
                    pending = pool.submit(importer.ownership.import_file, name)
                    try:
                        self.assertTrue(entered.wait(10))
                        save_json(f.control / "barrier.json",
                                  dict(f.barrier, destination="https://ntfy.delo.sh/other-tolt"))
                        self.assertFalse(observer.ownership.permits(f.event["id"]))
                        with observer.ledger.connection() as db:
                            self.assertEqual(db.execute("SELECT state FROM lowwater_barrier").fetchone()[0],
                                             "conflict")
                        save_json(f.control / "barrier.json", f.barrier)
                    finally:
                        release.set()
                    ack = pending.result(timeout=10)
                self.assertEqual(ack["status"], "conflict")
                self.assertFalse(ack["durable_import"])
                for _ in range(3):
                    retried = f.route().ownership.import_file(name)
                    self.assertFalse(retried["durable_import"])
                restarted = f.route()
                status = restarted.ownership.status(handoff["grant"]["grant_id"])
                self.assertEqual(status["ownership_status"], "conflict")
                self.assertEqual(status["import_ack"], ack)
                self.assertEqual(status["delivery"]["attempts"], 0)
                self.assertEqual(asyncio.run(restarted.deliver(event["id"])), "family-conflict")
                self.assertIsNone(restarted.ledger.claim(
                    event["id"], f.cfg.destination, f.clock(), f.cfg))
                with restarted.ledger.connection() as db:
                    stored_ack = json.loads(db.execute("SELECT body FROM lowwater_ack WHERE ack_id=?",
                                                      (ack["ack_id"],)).fetchone()["body"])
                    stored_original = json.loads(db.execute("SELECT body FROM lowwater_ack WHERE ack_id=?",
                                                           (original_ack["ack_id"],)).fetchone()["body"])
                    stored_grant = db.execute("SELECT * FROM lowwater_grant WHERE event_id=?",
                                              (event["id"],)).fetchone()
                self.assertEqual(stored_ack, ack)
                self.assertEqual(stored_original, original_ack)
                self.assertEqual(stored_grant["status"], "conflict")
                self.assertEqual(stored_grant["digest"], owner.digest(handoff))
                self.assertEqual(json.loads(stored_grant["body"]), handoff)
                self.assertEqual(f.post.raw, [])

    def test_grant_acceptance_rechecks_pinned_digest_inside_its_transaction(self):
        r = self.route()
        name = self.export()
        def change_digest(value, config):
            with r.ledger.connection(True) as db:
                db.execute("UPDATE lowwater_barrier SET digest=?", ("0" * 64,))
            return family.format_message(value, config)
        r.ownership.formatter = change_digest
        ack = r.ownership.import_file(name)
        self.assertEqual(ack["status"], "conflict")
        self.assertFalse(ack["durable_import"])
        self.assertEqual(self.route().ownership.status(
            self.handoff["grant"]["grant_id"])["ownership_status"], "conflict")
        self.assertEqual(self.post.raw, [])

    def test_status_exposes_barrier_conflict_for_legacy_late_accepted_row(self):
        r = self.route()
        original_ack = r.ownership.import_file(self.export())
        # Model a pre-fix late insert: barrier conflict exists, grant still says accepted.
        with r.ledger.connection(True) as db:
            db.execute("UPDATE lowwater_barrier SET state='conflict'")
        restarted = self.route()
        status = restarted.ownership.status(self.handoff["grant"]["grant_id"])
        self.assertEqual(status["ownership_status"], "conflict")
        self.assertEqual(status["import_ack"], original_ack)  # Historical immutable evidence, not authority.
        self.assertEqual(status["delivery"]["state"], "pending")
        self.assertIsNone(restarted.ledger.claim(self.event["id"], self.cfg.destination, self.clock(), self.cfg))
        self.assertEqual(self.post.raw, [])

    def test_acceptance_committed_before_conflict_keeps_historical_ack_not_current_authority(self):
        r = self.route()
        ack = r.ownership.import_file(self.export())
        self.assertEqual(ack["status"], "accepted")
        save_json(self.control / "barrier.json", dict(self.barrier, destination="https://ntfy.delo.sh/other-tolt"))
        self.assertFalse(self.route().ownership.permits(self.event["id"]))
        save_json(self.control / "barrier.json", self.barrier)
        current = self.route().ownership.status(self.handoff["grant"]["grant_id"])
        self.assertEqual(current["ownership_status"], "conflict")
        self.assertEqual(current["import_ack"], ack)
        with r.ledger.connection() as db:
            self.assertEqual(json.loads(db.execute("SELECT body FROM lowwater_ack WHERE ack_id=?",
                                                  (ack["ack_id"],)).fetchone()["body"]), ack)
        self.assertEqual(self.post.raw, [])

    def test_local_cli_import_and_status_never_calls_http(self):
        self.export()
        env = {**os.environ, "NTFY_TOLT_ENABLED": "true", "NTFY_TOLT_CUTOVER_ACCEPTED": "true",
               "NTFY_TOLT_OWNERSHIP_DIR": str(self.control), "NTFY_TOLT_OWNERSHIP_UID": str(os.getuid()),
               "NTFY_TOLT_STATE": str(self.cfg.state_path), "NTFY_FAMILY_VALIDATOR_ROOT": str(ROOT),
               "BLOODBANK_SCHEMAS_DIR": str(ROOT / "schemas")}
        result = subprocess.run([sys.executable, owner.__file__, "--import-pending"], env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]["status"], "accepted")
        result = subprocess.run([sys.executable, owner.__file__, "--status", self.handoff["grant"]["grant_id"]],
                                env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["delivery"]["attempts"], 0)

    def test_direct_claim_versus_transfer_race_only_one_owner(self):
        model = SourceRaceModel(self.root)
        model.barrier()
        for _ in range(12):
            eid = str(uuid.uuid4())
            model.birth(eid)
            start = threading.Barrier(2)
            def contender(op):
                start.wait()
                return op(eid)
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(contender, (model.direct, model.transfer)))
            self.assertEqual(sum(results), 1)

    def test_fence_crash_before_export_and_ack_loss_never_restore_direct(self):
        model = SourceRaceModel(self.root)
        model.barrier()
        model.birth(self.event["data"]["episode_id"])
        self.assertTrue(model.transfer(self.event["data"]["episode_id"]))
        self.assertEqual(self.handle(self.route()), "family-pending")
        self.assertEqual(self.post.raw, [])  # Fence alone or unacknowledged publication is not authority.
        recovered = SourceRaceModel(self.root)
        self.assertFalse(recovered.direct(self.event["data"]["episode_id"]))
        self.assertTrue(recovered.transfer(self.event["data"]["episode_id"]))
        self.export()  # Recovery export from fenced source, fake publication receipt.
        self.route().ownership.scan(10)
        self.assertFalse(recovered.direct(self.event["data"]["episode_id"]))
        asyncio.run(self.route().drain())
        self.assertEqual(len(self.post.raw), 1)

    def test_fence_control_is_sibling_and_does_not_rewrite_saved_intent(self):
        body = self.handoff["envelope_body"]
        saved = {"schema": "ltf.lifecycle-intent/1", "body": body,
                 "sha256": hashlib.sha256(body.encode()).hexdigest(),
                 "source": {"notification_owner": "direct",
                            "publication_hold": "bb34-per-episode-ownership-unaccepted"}}
        original = owner.canonical(saved)
        episode = {"bloodbank_intent": saved}
        episode["bloodbank_ownership"] = {
            "schema": "bloodbank.tolt-lowwater-source-control/1", "admitted_barrier_id": BARRIER,
            "episode_id": self.event["data"]["episode_id"], "direct_claims": 0,
            "owner": "bloodbank", "grant": self.handoff["grant"]}
        import jsonschema
        schema = json.loads(Path(owner.__file__).with_name("lowwater_control.schema.json").read_text())
        jsonschema.validate(episode["bloodbank_ownership"], {**schema, "$ref": "#/$defs/source_control"})
        self.assertEqual(owner.canonical(episode["bloodbank_intent"]), original)

    def test_model_all_preexisting_and_attempted_states_never_transfer(self):
        model = SourceRaceModel(self.root)
        for state in ("pending", "sending", "uncertain", "delivered"):
            model.birth(state, state)
        model.barrier()
        for state in ("pending", "sending", "uncertain", "delivered"):
            self.assertFalse(model.transfer(state))
        model.birth("attempted-pending", attempted=True)
        self.assertFalse(model.transfer("attempted-pending"))
        model.birth("fresh")
        self.assertTrue(model.direct("fresh"))
        self.assertFalse(model.transfer("fresh"))


if __name__ == "__main__":
    unittest.main()
