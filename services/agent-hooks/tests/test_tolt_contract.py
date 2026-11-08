"""BB-34 five-fact contract tests. No production broker, Redis, HTTP or device calls."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/agent-hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["BLOODBANK_SCHEMAS_DIR"] = str(ROOT / "schemas")
from core.validate import (ContractViolation, EnvelopeInvalid, GAME_EVENT_NAMESPACE,
    assert_contract, assert_type_shape, game_event_id, game_ordering_key,
    app_launch_event_id, validate_envelope)
from tolt_contract_fixtures import CASES, NATIVE, envelope_for
FAILURES = (ContractViolation, EnvelopeInvalid)


def reidentify(env):
    if env["domain"] == "game":
        env["id"] = game_event_id(env["type"], env["data"])
        env["ordering_key"] = game_ordering_key(env["type"], env["data"])
    else:
        env["id"] = app_launch_event_id(env["data"])
    return env


class ToltContractTests(unittest.TestCase):
    def test_five_independent_facts_validate(self):
        self.assertEqual(len({v[0] for v in CASES.values()}), 5)
        for case in CASES:
            with self.subTest(case=case):
                env=envelope_for(case); validate_envelope(env)
                self.assertEqual(len(env["type"].split(".")),4)
                self.assertEqual(len(env["subject"].split(".")),5)

    def test_abandoned_union_is_not_registered(self):
        self.assertFalse((ROOT/"schemas/bloodbank/game/delivery.observed.json").exists())
        with self.assertRaises(ContractViolation): assert_type_shape("bloodbank.game.delivery.observed")

    def test_negative_domain_entity_action_pairs(self):
        for bad in ("bloodbank.game.task.started", "bloodbank.game.item.started",
                    "bloodbank.repo.item.created", "bloodbank.project.pack.released",
                    "bloodbank.game.app_launch.recorded", "bloodbank.project.app_launch.started",
                    "bloodbank.game.implementation.start", "bloodbank.tolt.item.created",
                    "bloodbank.v1.game.item.created"):
            with self.subTest(type=bad),self.assertRaises(ContractViolation): assert_type_shape(bad)

    def test_final_literal_known_vectors(self):
        expected = {'implementation': '47eb32b3-f0d2-516d-a7f9-b77d32a7c681', 'low_water': '14b91329-a325-5640-bcb7-6dc398f12e8c', 'item': '47ee22cb-0503-5f7a-adde-0f629c8bb307', 'ready': 'aee4ab32-7326-578d-8b2b-b73e77d2bd2f', 'launch': '38ef5256-6902-5bfb-baa1-6762ec34656b'}
        self.assertEqual(str(GAME_EVENT_NAMESPACE),"d8d05b87-993a-5550-9d73-a647951c7fac")
        for case,value in expected.items(): self.assertEqual(envelope_for(case)["id"],value)

    def test_repeated_readback_and_channels_do_not_remint_same_pack(self):
        env=envelope_for("ready"); changed=copy.deepcopy(env)
        changed["data"]["catalog_revision"]="later-readback"
        changed["data"]["channels"].append({"channel":"private_app","verified":True,"evidence_ref":"urn:bb34:private","native_build":NATIVE})
        self.assertEqual(reidentify(changed)["id"],env["id"])
        validate_envelope(changed)
        for field,value in (("id","other-pack"),("version",3),("sha256","f"*64)):
            changed=copy.deepcopy(env); changed["data"]["pack"][field]=value
            self.assertNotEqual(reidentify(changed)["id"],env["id"])

    def test_exact_playable_link_not_fallback_page(self):
        for url in ("https://tolt.delo.sh/play", "https://tolt.delo.sh/play/fixture-pack?v=1",
                    "https://tolt.delo.sh/play/other?v=2", "https://user:pass@tolt.delo.sh/play/fixture-pack?v=2"):
            env=envelope_for("ready");env["data"]["playable_url"]=url
            with self.subTest(url=url),self.assertRaises(FAILURES):validate_envelope(env)

    def test_release_payload_required_fields_and_verification(self):
        for field in ("title","source_ticket","source_job","changelog","release_kind","channels","verification"):
            env=envelope_for("ready");del env["data"][field]
            with self.subTest(field=field),self.assertRaises(FAILURES):validate_envelope(env)
        for field in envelope_for("ready")["data"]["verification"]:
            env=envelope_for("ready");env["data"]["verification"][field]=False
            with self.assertRaises(FAILURES):validate_envelope(env)

    def test_native_pack_channel_requires_verified_exact_native_build(self):
        env=envelope_for("ready")
        env["data"]["channels"].append({"channel":"testflight","verified":True,"evidence_ref":"urn:bb34:verified-native","native_build":None})
        with self.assertRaises(FAILURES):validate_envelope(env)
        env["data"]["channels"][-1]["native_build"]=NATIVE;validate_envelope(env)
        env["data"]["channels"][-1]["channel"]="unknown"
        with self.assertRaises(FAILURES):validate_envelope(env)

    def test_low_water_existing_episode_zero_five_unknown(self):
        for count in (0,5):
            env=envelope_for("low_water");env["data"]["eligible_count"]=count;validate_envelope(env)
        for count in (None,6,-1,True,3.5,"0"):
            env=envelope_for("low_water");env["data"]["eligible_count"]=count
            with self.subTest(count=count),self.assertRaises(FAILURES):validate_envelope(env)
        env=envelope_for("low_water");env["data"]["complete"]=False
        with self.assertRaises(FAILURES):validate_envelope(env)

    def test_episode_not_count_is_identity(self):
        env=envelope_for("low_water"); original=env["id"]
        env["data"]["eligible_count"]=5; self.assertEqual(reidentify(env)["id"],original)
        env["data"]["episode_id"]=str(uuid.uuid4()); self.assertNotEqual(reidentify(env)["id"],original)
        env["data"]["episode_id"]=str(uuid.uuid5(uuid.NAMESPACE_URL,"second-detector"))
        with self.assertRaises(FAILURES):validate_envelope(reidentify(env))

    def test_implementation_claim_not_attempt_or_job_spelling(self):
        env=envelope_for("implementation"); original=env["id"]
        env["data"]["job_id"]="same-claim-retry";self.assertEqual(reidentify(env)["id"],original)
        env["data"]["implementation_id"]=str(uuid.uuid4());self.assertNotEqual(reidentify(env)["id"],original)

    def test_item_mint_not_registry_approval_or_art_edit(self):
        env=envelope_for("item");original=env["id"]
        for status in ("not_observed","candidate","approved","rejected","live"):
            env["data"]["registry_status"]=status;env["data"]["art_sha256"]="f"*64
            validate_envelope(env);self.assertEqual(reidentify(env)["id"],original)
        env["data"]["content_status"]="draft"
        with self.assertRaises(FAILURES):validate_envelope(env)

    def test_app_identity_includes_native_version_final_build_bundle_and_installation(self):
        env=envelope_for("launch")
        for field,value in (("bundle_id","dev.other.app"),("app_version","0.2.0"),("build_number","43")):
            changed=copy.deepcopy(env);changed["data"]["native_build"][field]=value
            self.assertNotEqual(reidentify(changed)["id"],env["id"])
        changed=copy.deepcopy(env);changed["data"]["installation_id"]=str(uuid.uuid4())
        self.assertNotEqual(reidentify(changed)["id"],env["id"])
        self.assertNotIn("project_slug",env["data"])
        self.assertNotIn("pack",env["data"])

    def test_unknown_channel_never_testflight_and_sandbox_alone_insufficient(self):
        env=envelope_for("launch");env["data"].update(channel="unknown",testflight_eligible=False)
        env["data"]["channel_evidence"]={"basis":"sandbox_receipt_only","evidence_ref":None}
        validate_envelope(env)
        env["data"]["testflight_eligible"]=True
        with self.assertRaises(FAILURES):validate_envelope(env)
        env["data"]["channel"]="testflight"
        with self.assertRaises(FAILURES):validate_envelope(env)

    def test_launch_not_install_diag_or_unverified_boot(self):
        for field,value in (("native_identity_source","linux_android_diag"),("native_boot_verified",False),
                            ("final_native_identity_verified",False),("bundle_verified",False),
                            ("installation_verified",False)):
            env=envelope_for("launch");env["data"][field]=value
            with self.subTest(field=field),self.assertRaises(FAILURES):validate_envelope(env)

    def test_lineage_identity_revision_ordering_and_self_cause(self):
        for case in CASES:
            for field,value in (("id",str(uuid.uuid4())),("ordering_key","wrong:bucket"),("schemaref",CASES[case][0]+".v2")):
                env=envelope_for(case);env[field]=value
                with self.subTest(case=case,field=field),self.assertRaises(FAILURES):validate_envelope(env)
            env=envelope_for(case);env["causationid"]=env["id"]
            if case!="launch":env["data"]["cause_event_id"]=env["id"]
            with self.assertRaises(FAILURES):validate_envelope(env)

    def test_closed_payloads_dates_and_observation_not_receipt(self):
        for case in CASES:
            env=envelope_for(case);env["data"]["received_at"]=env["time"]
            with self.assertRaises(FAILURES):validate_envelope(env)
            env=envelope_for(case);env["data"]["observed_at"]="2026-02-30T00:00:00Z"
            with self.assertRaises(FAILURES):validate_envelope(env)
            env=envelope_for(case);env["data"]["provenance"]["unexpected"]=1
            with self.assertRaises(FAILURES):validate_envelope(env)

    def test_spec_channel_evidence_matrix(self):
        allowed = {"testflight": {"verified_testflight_runtime"},
                   "app_store": {"verified_other_distribution"},
                   "private_app": {"verified_other_distribution"},
                   "unknown": {"unverified", "sandbox_receipt_only"}}
        bases = {"verified_testflight_runtime", "verified_other_distribution",
                 "unverified", "sandbox_receipt_only"}
        for channel, accepted in allowed.items():
            for basis in bases:
                env = envelope_for("launch")
                env["data"].update(channel=channel, testflight_eligible=channel == "testflight")
                env["data"]["channel_evidence"]["basis"] = basis
                for check in (assert_contract, validate_envelope):
                    with self.subTest(channel=channel, basis=basis, checker=check.__name__):
                        if basis in accepted:
                            check(env)
                        else:
                            with self.assertRaises(FAILURES):
                                check(env)

    def test_spec_shared_native_app_version_shape(self):
        for value in ("android-diagnostic", "1.2-beta", "1..2", "1.2.3.4", ""):
            for case in ("launch", "ready"):
                env = envelope_for(case)
                if case == "launch":
                    env["data"]["native_build"]["app_version"] = value
                else:
                    native = copy.deepcopy(NATIVE)
                    native["app_version"] = value
                    env["data"]["channels"].append({"channel": "testflight", "verified": True,
                        "evidence_ref": "urn:bb34:native-version", "native_build": native})
                with self.subTest(case=case, value=value), self.assertRaises(FAILURES):
                    validate_envelope(reidentify(env))

    def test_spec_exact_query_rejects_blank_extra_duplicate_and_malformed(self):
        for query in ("v=2&force_fallback=", "v=2&force_fallback", "v=2&", "v=2&&",
                      "v=2&v=", "v=&v=2", "v=2&v=2", "v=", "v", "v=2&%ZZ=",
                      "%76=2", "v=%32", "v=2#"):
            env = envelope_for("ready")
            env["data"]["playable_url"] = "https://tolt.delo.sh/play/fixture-pack?" + query
            for check in (assert_contract, validate_envelope):
                with self.subTest(query=query, checker=check.__name__), self.assertRaises(FAILURES):
                    check(env)

    def test_malformed_playable_authority_rejected_by_both_paths(self):
        bad_urls = ("https://tolt.delo.sh:bad/play/fixture-pack?v=2",
                    "https://tolt.delo.sh:99999/play/fixture-pack?v=2",
                    "https://tolt.delo.sh:/play/fixture-pack?v=2",
                    "https://tolt.delo.sh\n/play/fixture-pack?v=2",
                    "https://tolt.delo.sh\t/play/fixture-pack?v=2",
                    " https://tolt.delo.sh/play/fixture-pack?v=2",
                    "https://bad_host/play/fixture-pack?v=2")
        for url in bad_urls:
            env = envelope_for("ready")
            env["data"]["playable_url"] = url
            for check in (assert_contract, validate_envelope):
                with self.subTest(url=url, checker=check.__name__), self.assertRaises(ContractViolation):
                    check(env)
        for host in ("example.org", "example.org:443", "[::1]:443"):
            env = envelope_for("ready")
            env["data"]["playable_url"] = f"https://{host}/play/fixture-pack?v=2"
            assert_contract(env)

    def test_malformed_identity_inputs_refused_before_hash_and_by_cli(self):
        cases = (("implementation", ("project_slug",)),
                 ("implementation", ("implementation_id",)),
                 ("low_water", ("pool_id",)),
                 ("low_water", ("episode_id",)),
                 ("item", ("item_id",)),
                 ("ready", ("pack", "id")),
                 ("ready", ("pack", "version")),
                 ("ready", ("pack", "sha256")),
                 ("launch", ("project", "slug")),
                 ("launch", ("native_build", "bundle_id")),
                 ("launch", ("native_build", "app_version")),
                 ("launch", ("native_build", "build_number")))
        for case, path in cases:
            for value in (None, True, -1 if path == ("pack", "version") else 123, [], {}, ""):
                env = envelope_for(case)
                node = env["data"]
                for segment in path[:-1]:
                    node = node[segment]
                node[path[-1]] = value
                with self.subTest(case=case, path=path, value=value):
                    for check in ((lambda: game_event_id(env["type"], env["data"]))
                                  if case != "launch" else (lambda: app_launch_event_id(env["data"])),
                                  lambda: assert_contract(env), lambda: validate_envelope(env)):
                        with self.assertRaises(ContractViolation):
                            check()
        for case, path, value in (("implementation", ("project_slug",), ["contract-fixture"]),
                                   ("launch", ("native_build", "app_version"), 123)):
            env = envelope_for(case)
            node = env["data"]
            for segment in path[:-1]:
                node = node[segment]
            node[path[-1]] = value
            if case == "implementation":
                parts = [env["type"], value, env["data"]["implementation_id"]]
                env["ordering_key"] = f"game:{value}:implementation:{env['data']['implementation_id']}"
            else:
                data = env["data"]
                parts = [env["type"], data["project"]["slug"], data["installation_id"],
                         data["native_build"]["bundle_id"], value, data["native_build"]["build_number"]]
            env["id"] = str(uuid.uuid5(GAME_EVENT_NAMESPACE,
                                     json.dumps(parts, ensure_ascii=True, separators=(",", ":"))))
            proc = subprocess.run([sys.executable, str(ROOT / "cli/bb.py"), "verify-envelope", "--file", "-"],
                                  input=json.dumps(env), capture_output=True, text=True, cwd=ROOT)
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("FAIL", proc.stdout + proc.stderr)

    def test_malformed_identity_containers_are_contract_violations(self):
        for case, field in (("ready", "pack"), ("launch", "project"),
                            ("launch", "native_build")):
            for value in (None, [], True, 12, "wrong"):
                env = envelope_for(case)
                env["data"][field] = value
                with self.subTest(case=case, field=field, value=value):
                    with self.assertRaises(ContractViolation):
                        assert_contract(env)
                    with self.assertRaises(ContractViolation):
                        validate_envelope(env)
        for case in CASES:
            env = envelope_for(case)
            env["data"] = []
            with self.subTest(case=case), self.assertRaises(ContractViolation):
                assert_contract(env)

    def test_saved_full_envelope_verify_command_is_green_without_publication(self):
        for case in CASES:
            env=envelope_for(case)
            proc=subprocess.run([sys.executable,str(ROOT/"cli/bb.py"),"verify-envelope","--file","-"],
                input=json.dumps(env),capture_output=True,text=True,cwd=ROOT)
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
            self.assertIn("PASS",proc.stdout)

    def test_existing_outbox_precedent_retains_exact_bytes_across_bounded_failure_and_replay(self):
        sys.path.insert(0, str(ROOT / "services/artifact-sidecar"))
        from sidecar import OUTBOX_MAX_ATTEMPTS, Sidecar, SidecarState

        # Fake callbacks exercise the existing state/drain only. This does not
        # install a TOLT producer or prove actual JetStream acceptance.
        for case in CASES:
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix="bb34-outbox-") as tmp:
                path = Path(tmp) / "state.sqlite3"
                env = envelope_for(case)
                state = SidecarState(path)
                self.assertTrue(state.enqueue(env["id"], env))
                original_json = state.due()[0]["envelope_json"]
                self.assertFalse(state.enqueue(env["id"], env))
                seen = []

                async def unavailable(envelope):
                    seen.append(envelope)
                    raise TimeoutError("synthetic lost acknowledgement")

                result = asyncio.run(Sidecar.drain_outbox_async(SimpleNamespace(state=state), unavailable))
                self.assertEqual(result[0].kind, "error")
                self.assertEqual(len(state.pending()), 1)
                self.assertEqual(state.due(), [])
                state.close()
                state = SidecarState(path)
                row_id = state.pending()[0]["outbox_id"]
                for _ in range(OUTBOX_MAX_ATTEMPTS - 1):
                    state.record_attempt(row_id, "synthetic outage")
                self.assertEqual(state.due(), [])
                self.assertEqual(len(state.exhausted()), 1)
                self.assertEqual(state.retry_exhausted(), 1)
                self.assertEqual(state.due()[0]["envelope_json"], original_json)

                async def fake_ack(envelope):
                    seen.append(envelope)

                result = asyncio.run(Sidecar.drain_outbox_async(SimpleNamespace(state=state), fake_ack))
                self.assertEqual(result[0].kind, "published")
                self.assertEqual(state.pending(), [])
                self.assertEqual(seen, [env, env])
                self.assertFalse(state.enqueue(env["id"], env))
                state.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
