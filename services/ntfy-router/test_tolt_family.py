"""Synthetic envelopes, temporary SQLite and fake POST only; never production."""
import asyncio
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
import main
import tolt_family as family

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/agent-hooks"))
sys.path.insert(0, str(ROOT / "services/agent-hooks/tests"))
from tolt_contract_fixtures import CASES, envelope_for


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class FakePost:
    def __init__(self, replies=()):
        self.replies, self.raw, self.messages = list(replies), [], []

    async def __call__(self, raw):
        self.raw.append(raw)
        self.messages.append(json.loads(raw))
        reply = self.replies.pop(0) if self.replies else family.Receipt(200, "synthetic-message-id")
        if isinstance(reply, Exception):
            raise reply
        return reply


class FamilyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bb34-family-")
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.config = family.Config(enabled=True, cutover_accepted=True,
                                    state_path=Path(self.tmp.name) / "delivery.sqlite3",
                                    validator_root=ROOT, retry_base=1, lease_seconds=5,
                                    post_timeout=0.5)
        self.validator = family.CanonicalValidator(ROOT)
        self.post = FakePost()

    def route(self, config=None, poster=None, validator=True):
        cfg = config or self.config
        return family.FamilyRoute(cfg, family.DeliveryLedger(cfg.state_path),
                                  self.validator if validator else None,
                                  poster or self.post, clock=self.clock)

    def handle(self, route, case):
        env = envelope_for(case)
        return asyncio.run(route.handle(env, env["subject"]))

    def test_five_formats_have_one_topic_correct_priorities_and_actual_identities(self):
        for case in CASES:
            env = envelope_for(case)
            self.validator(env)
            message = family.format_message(env, self.config)
            self.assertEqual(message["topic"], "tolt")
            self.assertEqual(message["priority"], 4 if case == "low_water" else 3)
            self.assertIn(env["id"], message["message"])
            self.assertIn(env["data"]["observed_at"], message["message"])
            self.assertNotIn("attach", message)
        pack = family.format_message(envelope_for("ready"), self.config)
        self.assertEqual(pack["click"], "https://tolt.delo.sh/play/fixture-pack?v=2")
        self.assertIn("c" * 64, pack["message"])
        launch = family.format_message(envelope_for("launch"), self.config)
        self.assertIn("0.1.0", launch["message"])
        self.assertIn("final iOS build: 42", launch["message"])
        self.assertNotIn("pack v", launch["message"])
        item = family.format_message(envelope_for("item"), self.config)
        self.assertIn("Registry status: not_observed", item["message"])

    def test_four_deliverable_formats_post_once_and_lowwater_is_held(self):
        route = self.route()
        for case in CASES:
            result = self.handle(route, case)
            self.assertEqual(result, "family-suppressed" if case == "low_water" else "family-accepted")
        self.assertEqual(len(self.post.messages), 4)
        self.assertEqual({m["topic"] for m in self.post.messages}, {"tolt"})

    def test_router_intercepts_before_mutes_buckets_and_all_mirrors(self):
        async def generic(*args):
            self.fail("family must not reach generic poster or mirrors")
        router = main.NtfyRouter(main.Policy(mute=("bloodbank.*",), digest=()),
                                 generic, main.TokenBucket(0, 1), family=self.route(),
                                 mirror_domain=True, deploy_topic="deploys")
        for case in CASES:
            env = envelope_for(case)
            result = asyncio.run(router.handle(env, env["subject"]))
            self.assertTrue(result.startswith("family-"))
        self.assertEqual(len(self.post.messages), 4)
        self.assertEqual(router.overflow, {})
        self.assertEqual(router.digested, {})

    def test_default_disabled_and_each_owner_gate_suppresses_persistently(self):
        self.assertFalse(family.Config().authorized)
        for enabled, accepted in ((False, False), (True, False), (False, True)):
            cfg = replace(self.config, enabled=enabled, cutover_accepted=accepted,
                          state_path=Path(self.tmp.name) / f"{enabled}-{accepted}.db")
            route = self.route(cfg)
            self.assertEqual(self.handle(route, "implementation"), "family-suppressed")
            reopened = self.route(replace(cfg, enabled=True, cutover_accepted=True))
            self.assertEqual(self.handle(reopened, "implementation"), "family-suppressed")
            asyncio.run(reopened.drain())
        self.assertEqual(self.post.raw, [])

    def test_lowwater_is_always_non_notifying_including_late_first_observation(self):
        for authorized in (False, True):
            cfg = replace(self.config, enabled=authorized, state_path=Path(self.tmp.name) / f"low-{authorized}.db")
            route = self.route(cfg)
            self.assertEqual(self.handle(route, "low_water"), "family-suppressed")
            row = route.ledger.get(envelope_for("low_water")["id"], cfg.destination)
            self.assertEqual(row["reason"], "lowwater-owner-episode-gate-unavailable")
            self.assertIn("episode_id", row["envelope_json"])
            reopened = self.route(replace(cfg, enabled=True, cutover_accepted=True))
            self.clock.now += 100000
            self.assertEqual(self.handle(reopened, "low_water"), "family-suppressed")
            asyncio.run(reopened.drain())
        self.assertEqual(self.post.raw, [])

    def test_absent_id_wrong_payload_and_transport_subject_fail_closed(self):
        route = self.route()
        for field in ("id", "data", "actor"):
            env = envelope_for("ready")
            del env[field]
            self.assertEqual(asyncio.run(route.handle(env)), "family-invalid")
        env = envelope_for("item")
        env["data"]["content_status"] = "draft"
        self.assertEqual(asyncio.run(route.handle(env)), "family-invalid")
        env = envelope_for("ready")
        self.assertEqual(asyncio.run(route.handle(env, "bloodbank.evt.project.deployment.completed")),
                         "family-invalid")
        self.assertEqual(self.post.raw, [])
        self.assertEqual(route.ledger.counts(), {})

    def test_missing_type_on_family_subject_never_generic_fallback(self):
        env = envelope_for("ready")
        del env["type"]
        self.assertTrue(family.matches(None, env["subject"]))
        self.assertEqual(asyncio.run(self.route().handle(env, env["subject"])), "family-invalid")

    def test_canonical_loader_missing_or_wrong_tree_is_unavailable(self):
        with self.assertRaises(family.ValidatorUnavailable):
            family.CanonicalValidator(Path(self.tmp.name))
        with patch.dict(os.environ, {"BLOODBANK_SCHEMAS_DIR": self.tmp.name}):
            with self.assertRaises(family.ValidatorUnavailable):
                family.CanonicalValidator(ROOT)
        route = self.route(validator=False)
        self.assertEqual(self.handle(route, "ready"), "family-unavailable")
        self.assertEqual(self.post.raw, [])

    def test_jsonschema_unavailable_never_downgrades_to_shape_only(self):
        with patch.object(self.validator.module, "validate_envelope",
                          side_effect=self.validator.module.ValidationUnavailable("not logged")):
            self.assertEqual(self.handle(self.route(), "ready"), "family-unavailable")
        self.assertEqual(self.post.raw, [])

    def test_restart_dedup_immutable_request_and_acceptance_receipt(self):
        route = self.route()
        self.assertEqual(self.handle(route, "ready"), "family-accepted")
        row = route.ledger.get(envelope_for("ready")["id"], self.config.destination)
        self.assertEqual(row["state"], "accepted")
        self.assertEqual(row["http_status"], 200)
        self.assertEqual(row["message_id"], "synthetic-message-id")
        self.assertEqual(row["request_json"], self.post.raw[0])
        self.assertEqual(self.handle(self.route(), "ready"), "family-duplicate")
        asyncio.run(self.route().drain())
        self.assertEqual(len(self.post.raw), 1)

    def test_same_id_different_envelope_is_a_persistent_conflict_not_overwrite(self):
        route = self.route()
        env = envelope_for("item")
        self.assertEqual(asyncio.run(route.handle(env)), "family-accepted")
        original = route.ledger.get(env["id"], self.config.destination)
        env["data"]["item_name"] = "Different but still schema-valid display"
        self.assertEqual(asyncio.run(route.handle(env)), "family-conflict")
        row = route.ledger.get(env["id"], self.config.destination)
        self.assertEqual(row["envelope_json"], original["envelope_json"])
        self.assertEqual(row["request_json"], original["request_json"])
        self.assertTrue(row["conflict_digest"])
        self.assertEqual(self.handle(self.route(), "item"), "family-conflict")
        self.assertEqual(len(self.post.raw), 1)

    def test_atomic_claim_across_two_router_instances(self):
        entered, release = None, None
        calls = []
        async def go():
            nonlocal entered, release
            entered, release = asyncio.Event(), asyncio.Event()
            async def delayed(raw):
                calls.append(raw)
                entered.set()
                await release.wait()
                return family.Receipt(200, "one")
            first, second = self.route(poster=delayed), self.route(poster=delayed)
            env = envelope_for("implementation")
            task = asyncio.create_task(first.handle(env))
            await entered.wait()
            self.assertEqual(await second.handle(env), "family-sending")
            release.set()
            self.assertEqual(await task, "family-accepted")
        asyncio.run(go())
        self.assertEqual(len(calls), 1)

    def test_429_waits_and_retries_exact_saved_request_on_existing_tick(self):
        self.post = FakePost([family.Receipt(429, retry_after=10), family.Receipt(200, "after-wait")])
        route = self.route()
        self.assertEqual(self.handle(route, "ready"), "family-pending")
        self.assertEqual(asyncio.run(route.drain()), [])
        self.clock.now += 11
        self.assertEqual(asyncio.run(self.route().drain()), ["family-accepted"])
        self.assertEqual(self.post.raw[0], self.post.raw[1])

    def test_explicitly_proven_preaccept_5xx_retries_but_ambiguous_5xx_does_not(self):
        self.post = FakePost([family.Receipt(503, preaccept=True), family.Receipt(200)])
        route = self.route()
        self.assertEqual(self.handle(route, "ready"), "family-pending")
        self.clock.now += 2
        self.assertEqual(asyncio.run(route.drain()), ["family-accepted"])
        self.post = FakePost([family.Receipt(503)])
        self.assertEqual(self.handle(self.route(), "item"), "family-uncertain")
        self.clock.now += 1000
        asyncio.run(self.route().drain())
        self.assertEqual(len(self.post.raw), 1)

    def test_bounded_exhaustion_retains_row_and_never_auto_rearms(self):
        self.post = FakePost([family.Receipt(429), family.Receipt(429)])
        cfg = replace(self.config, max_attempts=2)
        route = self.route(cfg)
        self.assertEqual(self.handle(route, "ready"), "family-pending")
        self.clock.now += 2
        self.assertEqual(asyncio.run(route.drain()), ["family-exhausted"])
        self.clock.now += 10000
        self.assertEqual(asyncio.run(self.route(cfg).drain()), [])
        row = route.ledger.get(envelope_for("ready")["id"], cfg.destination)
        self.assertEqual((row["state"], row["attempts"]), ("exhausted", 2))
        self.assertEqual(len(self.post.raw), 2)

    def test_destination_pushback_holds_other_family_rows_without_losing_them(self):
        self.post = FakePost([family.Receipt(429, retry_after=20)])
        route = self.route()
        self.assertEqual(self.handle(route, "ready"), "family-pending")
        self.assertEqual(self.handle(route, "item"), "family-pending")
        self.assertEqual(len(self.post.raw), 1)
        self.clock.now += 21
        self.assertEqual(asyncio.run(route.drain()), ["family-accepted", "family-accepted"])

    def test_ack_lost_after_post_is_uncertain_never_retried(self):
        self.post = FakePost([TimeoutError("synthetic lost acknowledgement, not logged")])
        route = self.route()
        self.assertEqual(self.handle(route, "ready"), "family-uncertain")
        self.clock.now += 10000
        self.assertEqual(self.handle(self.route(), "ready"), "family-uncertain")
        self.assertEqual(asyncio.run(self.route().drain()), [])
        self.assertEqual(len(self.post.raw), 1)

    def test_crash_after_claim_expires_to_uncertain_not_a_stolen_retry(self):
        route = self.route()
        env = envelope_for("ready")
        route.ledger.offer(env, family.format_message(env, self.config), self.config.destination)
        claim = route.ledger.claim(env["id"], self.config.destination, self.clock(), self.config)
        reopened = self.route()
        self.assertEqual(asyncio.run(reopened.drain()), [])
        self.clock.now += 6
        self.assertEqual(asyncio.run(reopened.drain()), [])
        self.assertEqual(reopened.ledger.get(env["id"], self.config.destination)["state"], "uncertain")
        self.assertEqual(route.ledger.finish(env["id"], self.config.destination, claim["claim_token"],
                                           self.clock(), family.Receipt(200, "late"), self.config), "uncertain")
        self.assertEqual(reopened.ledger.get(env["id"], self.config.destination)["late_message_id"], "late")
        self.assertEqual(self.post.raw, [])

    def test_late_http_callback_retains_uncertainty_and_receipt_for_reconciliation(self):
        async def late(raw):
            self.clock.now += 6
            return family.Receipt(200, "late-ack")
        route = self.route(poster=late)
        self.assertEqual(self.handle(route, "ready"), "family-uncertain")
        row = route.ledger.get(envelope_for("ready")["id"], self.config.destination)
        self.assertEqual((row["late_http_status"], row["late_message_id"]), (200, "late-ack"))

    def test_conflict_while_sending_cannot_be_overwritten_by_late_accept(self):
        async def go():
            entered, release = asyncio.Event(), asyncio.Event()
            async def delayed(raw):
                entered.set()
                await release.wait()
                return family.Receipt(200, "too-late")
            route = self.route(poster=delayed)
            env = envelope_for("item")
            task = asyncio.create_task(route.handle(env))
            await entered.wait()
            changed = copy.deepcopy(env)
            changed["data"]["item_name"] = "changed"
            self.assertEqual(await self.route().handle(changed), "family-conflict")
            release.set()
            self.assertEqual(await task, "family-conflict")
        asyncio.run(go())

    def test_cancelled_inflight_post_leaves_uncertain(self):
        async def go():
            entered = asyncio.Event()
            async def blocked(raw):
                entered.set()
                await asyncio.Event().wait()
            route = self.route(poster=blocked)
            env = envelope_for("item")
            task = asyncio.create_task(route.handle(env))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(route.ledger.get(env["id"], self.config.destination)["state"], "uncertain")
        asyncio.run(go())

    def test_corrupted_saved_request_is_conflict_without_post(self):
        route = self.route()
        env = envelope_for("ready")
        route.ledger.offer(env, family.format_message(env, self.config), self.config.destination)
        with route.ledger.connection(True) as db:
            db.execute("UPDATE delivery SET request_json=?", ('{"topic":"deploys"}',))
        self.assertEqual(asyncio.run(route.drain()), ["family-conflict"])
        self.assertEqual(self.post.raw, [])

    def test_destination_is_part_of_dedup_identity(self):
        self.assertEqual(self.handle(self.route(), "ready"), "family-accepted")
        self.assertEqual(self.handle(self.route(replace(self.config, topic="tolt-test")), "ready"), "family-accepted")
        self.assertEqual(len(self.post.raw), 2)

    def test_unknown_launch_channel_is_never_formatted_as_testflight(self):
        env = envelope_for("launch")
        env["data"].update(channel="unknown", testflight_eligible=False)
        env["data"]["channel_evidence"] = {"basis": "unverified", "evidence_ref": None}
        self.validator(env)
        message = family.format_message(env, self.config)
        self.assertNotIn("TestFlight", message["title"] + message["message"])
        self.assertIn("unknown", message["title"])

    def non_testflight_launch(self, channel):
        env = envelope_for("launch")
        env["data"].update(channel=channel, testflight_eligible=False)
        env["data"]["channel_evidence"] = {
            "basis": "unverified" if channel == "unknown" else "verified_other_distribution",
            "evidence_ref": "urn:bb34:synthetic:non-testflight",
        }
        self.validator(env)
        return env

    def family_router(self, route):
        async def generic(*args):
            self.fail("suppressed launch reached generic route/mirrors")
        return main.NtfyRouter(main.Policy((), ()), generic, main.TokenBucket(1, 1),
                               family=route, mirror_domain=True, deploy_topic="deploys")

    def test_non_testflight_arrivals_hold_through_both_entrypoints_and_gate_changes(self):
        for channel in ("unknown", "app_store", "private_app"):
            for authorized in (False, True):
                for entry in ("family", "router"):
                    with self.subTest(channel=channel, authorized=authorized, entry=entry):
                        cfg = replace(self.config, enabled=authorized, cutover_accepted=authorized,
                                      state_path=Path(self.tmp.name) / f"{channel}-{authorized}-{entry}.db")
                        route = self.route(cfg)
                        env = self.non_testflight_launch(channel)
                        target = route if entry == "family" else self.family_router(route)
                        self.assertEqual(asyncio.run(target.handle(env, env["subject"])), "family-suppressed")
                        saved = route.ledger.get(env["id"], cfg.destination)
                        self.assertEqual(saved["reason"], "app-launch-not-testflight-eligible")
                        self.assertEqual(saved["envelope_json"], family.canonical(env))
                        self.assertEqual(saved["attempts"], 0)
                        reopened = self.route(replace(cfg, enabled=True, cutover_accepted=True))
                        self.assertEqual(asyncio.run(reopened.handle(env)), "family-suppressed")
                        self.assertEqual(asyncio.run(reopened.drain()), [])
                        after = reopened.ledger.get(env["id"], cfg.destination)
                        for field in ("envelope_json", "request_json", "digest"):
                            self.assertEqual(after[field], saved[field])
                        self.assertEqual(after["state"], "suppressed")
                        self.assertEqual(self.post.raw, [])

    def test_preexisting_non_testflight_pending_is_held_by_direct_deliver_even_disabled(self):
        for channel in ("unknown", "app_store", "private_app"):
            for authorized in (False, True):
                with self.subTest(channel=channel, authorized=authorized):
                    cfg = replace(self.config, enabled=authorized, cutover_accepted=authorized,
                                  state_path=Path(self.tmp.name) / f"direct-{channel}-{authorized}.db")
                    route = self.route(cfg)
                    env = self.non_testflight_launch(channel)
                    request = family.format_message(env, cfg)
                    route.ledger.offer(env, request, cfg.destination)
                    # Simulate an old retry intent that is not yet due.
                    with route.ledger.connection(True) as db:
                        db.execute("UPDATE delivery SET next_attempt=?", (self.clock() + 10000,))
                    reopened = self.route(cfg)
                    self.assertEqual(asyncio.run(reopened.deliver(env["id"])), "family-suppressed")
                    row = reopened.ledger.get(env["id"], cfg.destination)
                    self.assertEqual((row["state"], row["attempts"]), ("suppressed", 0))
                    self.assertEqual(row["envelope_json"], family.canonical(env))
                    self.assertEqual(row["request_json"], family.canonical(request))
        self.assertEqual(self.post.raw, [])

    def test_restart_drain_suppresses_old_pending_even_not_due_or_gates_disabled(self):
        for channel in ("unknown", "app_store", "private_app"):
            for authorized in (False, True):
                for entry in ("family", "router"):
                    with self.subTest(channel=channel, authorized=authorized, entry=entry):
                        cfg = replace(self.config, enabled=authorized, cutover_accepted=authorized,
                                      state_path=Path(self.tmp.name) / f"drain-{channel}-{authorized}-{entry}.db")
                        route = self.route(cfg)
                        env = self.non_testflight_launch(channel)
                        route.ledger.offer(env, family.format_message(env, cfg), cfg.destination)
                        before = route.ledger.get(env["id"], cfg.destination)
                        with route.ledger.connection(True) as db:
                            db.execute("UPDATE delivery SET next_attempt=?", (self.clock() + 10000,))
                        reopened = self.route(cfg)
                        if entry == "family":
                            self.assertEqual(asyncio.run(reopened.drain()), ["family-suppressed"])
                        else:
                            router = self.family_router(reopened)
                            asyncio.run(router.drain_family())
                            self.assertEqual(router.stats["family-state-suppressed"], 1)
                        row = reopened.ledger.get(env["id"], cfg.destination)
                        self.assertEqual((row["state"], row["attempts"]), ("suppressed", 0))
                        for field in ("envelope_json", "request_json", "digest"):
                            self.assertEqual(row[field], before[field])
                        enabled = self.route(replace(cfg, enabled=True, cutover_accepted=True))
                        self.assertEqual(asyncio.run(enabled.handle(env)), "family-suppressed")
        self.assertEqual(self.post.raw, [])

    def test_non_testflight_eligibility_scan_is_bounded_and_advances_past_future_tf(self):
        cfg = replace(self.config, drain_limit=2)
        route = self.route(cfg)
        for number in range(3):
            env = envelope_for("launch")
            # Distinct synthetic installations; still canonical UUID4 inputs.
            import uuid
            env["data"]["installation_id"] = str(uuid.uuid4())
            env["id"] = self.validator.module.app_launch_event_id(env["data"])
            env["ordering_key"] = f"project:contract-fixture:installation:{env['data']['installation_id']}"
            self.validator(env)
            route.ledger.offer(env, family.format_message(env, cfg), cfg.destination)
        held = self.non_testflight_launch("unknown")
        route.ledger.offer(held, family.format_message(held, cfg), cfg.destination)
        with route.ledger.connection(True) as db:
            db.execute("UPDATE delivery SET next_attempt=?", (self.clock() + 10000,))
        self.assertEqual(asyncio.run(route.drain()), [])
        self.assertEqual(asyncio.run(route.drain()), ["family-suppressed"])
        self.assertEqual(route.ledger.get(held["id"], cfg.destination)["state"], "suppressed")
        self.assertEqual(self.post.raw, [])

    def test_only_real_eligible_tf_posts_and_old_tf_pending_still_obeys_gates(self):
        for entry in ("family", "router", "restart"):
            cfg = replace(self.config, state_path=Path(self.tmp.name) / f"positive-{entry}.db")
            env = envelope_for("launch")
            route = self.route(cfg)
            before = len(self.post.raw)
            if entry == "restart":
                route.ledger.offer(env, family.format_message(env, cfg), cfg.destination)
                disabled = self.route(replace(cfg, enabled=False))
                asyncio.run(disabled.drain())
                self.assertEqual(disabled.ledger.get(env["id"], cfg.destination)["state"], "pending")
                self.assertEqual(len(self.post.raw), before)
                self.assertEqual(asyncio.run(self.route(cfg).drain()), ["family-accepted"])
            else:
                target = route if entry == "family" else self.family_router(route)
                self.assertEqual(asyncio.run(target.handle(env, env["subject"])), "family-accepted")
            self.assertEqual(len(self.post.raw), before + 1)
            self.assertEqual(asyncio.run(self.route(cfg).handle(env)), "family-duplicate")
        invalid = envelope_for("launch")
        invalid["data"]["testflight_eligible"] = False
        self.assertEqual(asyncio.run(self.route().handle(invalid)), "family-invalid")

    def test_suppressed_launch_cannot_promote_on_new_topic_or_changed_channel(self):
        route = self.route()
        env = self.non_testflight_launch("unknown")
        self.assertEqual(asyncio.run(route.handle(env)), "family-suppressed")
        original = route.ledger.get(env["id"], self.config.destination)
        other = self.route(replace(self.config, topic="tolt-new"))
        self.assertEqual(asyncio.run(other.handle(env)), "family-suppressed")
        promoted = envelope_for("launch")
        self.assertEqual(promoted["id"], env["id"])
        self.assertEqual(asyncio.run(route.handle(promoted)), "family-conflict")
        row = route.ledger.get(env["id"], self.config.destination)
        self.assertEqual(row["envelope_json"], original["envelope_json"])
        self.assertEqual(row["request_json"], original["request_json"])
        self.assertEqual(self.post.raw, [])

    def test_enabled_preview_does_not_advertise_non_testflight_delivery(self):
        with patch.dict(os.environ, {"NTFY_FAMILY_VALIDATOR_ROOT": str(ROOT),
                                     "BLOODBANK_SCHEMAS_DIR": str(ROOT / "schemas"),
                                     "NTFY_TOLT_ENABLED": "true", "NTFY_TOLT_CUTOVER_ACCEPTED": "true"}):
            for channel in ("unknown", "app_store", "private_app"):
                self.assertFalse(main.preview(self.non_testflight_launch(channel))["delivery_enabled"])
            self.assertTrue(main.preview(envelope_for("launch"))["delivery_enabled"])

    def test_family_preview_is_explicitly_closed_and_does_not_create_a_ledger(self):
        state = Path(self.tmp.name) / "preview-must-not-exist.sqlite3"
        with patch.dict(os.environ, {"NTFY_FAMILY_VALIDATOR_ROOT": str(ROOT),
                                     "BLOODBANK_SCHEMAS_DIR": str(ROOT / "schemas"),
                                     "NTFY_TOLT_STATE": str(state),
                                     "NTFY_TOLT_ENABLED": "false",
                                     "NTFY_TOLT_CUTOVER_ACCEPTED": "false"}):
            for case in CASES:
                result = main.preview(envelope_for(case))
                self.assertEqual(result["disposition"], "preview-only")
                self.assertEqual(result["body"]["topic"], "tolt")
                self.assertFalse(result["delivery_enabled"])
                self.assertFalse(result["lowwater_owner_gate_available"])
        self.assertFalse(state.exists())

    def test_untrusted_text_and_nonapproved_urls_do_not_become_headers_or_actions(self):
        env = envelope_for("item")
        env["data"]["item_name"] = "Thing\\nPriority:5\r\nInjected"
        self.validator(env)
        message = family.format_message(env, self.config)
        self.assertNotIn("\\n", message["title"])
        self.assertNotIn("\n", message["title"])
        self.assertNotIn("headers", message)
        env = envelope_for("ready")
        env["data"]["playable_url"] = "https://evil.example/play/fixture-pack?v=2"
        self.validator(env)
        message = family.format_message(env, self.config)
        self.assertNotIn("actions", message)
        self.assertNotIn("click", message)
        self.assertNotIn("evil.example", family.canonical(message))

    def test_existing_stats_tick_drains_without_new_scheduler(self):
        self.post = FakePost([family.Receipt(429), family.Receipt(200)])
        route = self.route()
        router = main.NtfyRouter(main.Policy((), ()), None, main.TokenBucket(1, 1), family=route)
        env = envelope_for("ready")
        self.assertEqual(asyncio.run(router.handle(env, env["subject"])), "family-pending")
        asyncio.run(router.drain_family())
        self.assertEqual(router.stats["family-state-pending"], 1)
        self.clock.now += 2
        asyncio.run(router.drain_family())
        self.assertEqual(router.stats["family-state-pending"], 0)
        self.assertEqual(router.stats["family-state-accepted"], 1)

    def test_http_wire_is_one_json_post_and_token_is_not_in_saved_intent(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"id": "ntfy-fixture"})
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                route = self.route(poster=family.make_http_poster(client, self.config, "test-only-token"))
                env = envelope_for("ready")
                result = await route.handle(env)
                row = route.ledger.get(env["id"], self.config.destination)
                return result, row
        result, row = asyncio.run(go())
        self.assertEqual(result, "family-accepted")
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "https://ntfy.delo.sh/")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-only-token")
        self.assertEqual(json.loads(requests[0].content)["topic"], "tolt")
        self.assertEqual(row["message_id"], "ntfy-fixture")
        self.assertNotIn("test-only-token", row["request_json"] + row["envelope_json"])

    def test_wire_5xx_is_conservatively_ambiguous_not_assumed_preaccept(self):
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
                return await family.make_http_poster(client, self.config, "test-only-token")("{}")
        receipt = asyncio.run(go())
        self.assertEqual(receipt.status, 503)
        self.assertFalse(receipt.preaccept)

    def test_wire_read_timeout_never_retries_or_logs_exception_credentials(self):
        requests = []
        def handler(request):
            requests.append(request)
            raise httpx.ReadTimeout("synthetic token-must-not-be-logged")
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                route = self.route(poster=family.make_http_poster(client, self.config, "test-only-token"))
                router = main.NtfyRouter(main.Policy((), ()), None, main.TokenBucket(1, 1), family=route)
                env = envelope_for("item")
                with self.assertLogs("ntfy-router", level="INFO") as logged:
                    result = await router.handle(env, env["subject"])
                self.assertNotIn("token-must-not-be-logged", " ".join(logged.output))
                self.assertEqual(result, "family-uncertain")
                self.clock.now += 1000
                await route.drain()
        asyncio.run(go())
        self.assertEqual(len(requests), 1)

    def test_wire_connection_failure_is_known_preaccept_not_http_acceptance(self):
        def handler(request):
            raise httpx.ConnectError("synthetic failure before connection")
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                route = self.route(poster=family.make_http_poster(client, self.config, "test-only-token"))
                env = envelope_for("item")
                self.assertEqual(await route.handle(env), "family-pending")
                row = route.ledger.get(env["id"], self.config.destination)
                self.assertEqual(row["http_status"], 0)
        asyncio.run(go())

    def test_family_config_and_packaging_keep_default_disabled_canonical_volume(self):
        self.assertFalse(family.Config.from_env({}).authorized)
        for topic in ("deploys", "game", "project", "bloodbank", "x\nz", ""):
            with self.assertRaises(ValueError):
                replace(self.config, topic=topic)
        import yaml
        cfg = yaml.safe_load((Path(__file__).parent / "compose.yml").read_text())
        service = cfg["services"]["ntfy-router"]
        self.assertEqual(service["build"]["context"], "../..")
        self.assertIn("tolt-delivery-state:/var/lib/ntfy-router", service["volumes"])
        dockerfile = (Path(__file__).parent / "Dockerfile").read_text()
        self.assertIn("10001", dockerfile)
        self.assertIn("COPY schemas/", dockerfile)
        self.assertIn("COPY services/agent-hooks/core/", dockerfile)
        self.assertIn("jsonschema==4.25.1", dockerfile)


if __name__ == "__main__":
    unittest.main(verbosity=2)
