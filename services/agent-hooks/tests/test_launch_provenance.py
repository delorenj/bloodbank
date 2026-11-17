"""TOLT-99 provenance characterization, not a production admission implementation.

Synthetic source-policy/release records exercise the documented trust boundary.
No function here authenticates a client, reads a device, sends HTTP or publishes.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/agent-hooks"))
from core.validate import (
    APP_LAUNCH_TYPE, ContractViolation, EnvelopeInvalid, _build_registry,
    app_launch_event_id, assert_contract, validate_envelope,
)
from tolt_contract_fixtures import CASES, envelope_for

FLAGS = ("installation_verified", "native_boot_verified",
         "bundle_verified", "final_native_identity_verified")
# SHA256 of compact sorted JSON with descriptions removed from accepted base
# 34a18a1's launch schema. Tests do not require Git history or a live registry.
ACCEPTED_CONSTRAINT_SHA256 = "e2106a549c85de7cf983cbcec532dbc9eb4284d5310a7215f55cad9edf427d8e"
SOURCE_CHECKS = ("persisted_uuid_readback", "actual_ready_capture", "nondebug_ios_bridge")
WIRE_OBSERVATION_KEYS = {
    "format", "installation_id", "observed_at", "native_build",
    "bundle_manifest_sha256", "native_identity_source", "channel_hint",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def constraints(value):
    if isinstance(value, dict):
        return {k: constraints(v) for k, v in value.items() if k != "description"}
    if isinstance(value, list):
        return [constraints(v) for v in value]
    return value


def observation():
    e = envelope_for("launch")
    return {"format": 1, "installation_id": e["data"]["installation_id"],
            "observed_at": e["data"]["observed_at"], "native_build": copy.deepcopy(e["data"]["native_build"]),
            "bundle_manifest_sha256": e["data"]["bundle_manifest_sha256"],
            "native_identity_source": "ios_bundle", "channel_hint": "sandbox_receipt_only"}


def source_policy():
    # Reviewed implementation evidence, not per-observation booleans accepted from POST.
    return {"record_ref": "repo://contract-fixture/reviewed-native-source",
            "checks": {key: "source-positive-check" for key in SOURCE_CHECKS}}


def release_for(o):
    return {"record_ref": "urn:contract-fixture:release:final",
            "authority": "release-owner", "stage": "final-artifact",
            "native_build": copy.deepcopy(o["native_build"]),
            "bundle_manifest_sha256": o["bundle_manifest_sha256"]}


def logical_ref(value):
    return isinstance(value, str) and value.startswith(("urn:", "repo://")) and not any(c.isspace() for c in value)


def characterize_admission(o, policy, release):
    """Test-only decision table. A positive result is conditional source trust.

    Assumes the actual intake shape validator has accepted the observation.
    Matching records cannot distinguish a forged POST from an honest identical
    observation; this model deliberately does not claim otherwise.
    """
    if set(o) != WIRE_OBSERVATION_KEYS:
        return "invalid-observation"
    if not policy or not logical_ref(policy.get("record_ref")) or any(
            policy.get("checks", {}).get(key) != "source-positive-check" for key in SOURCE_CHECKS):
        return "observed-only/pending-evidence"
    if (not release or release.get("authority") != "release-owner"
            or release.get("stage") != "final-artifact" or not logical_ref(release.get("record_ref"))):
        return "observed-only/pending-evidence"
    if (release["native_build"] != o["native_build"]
            or release["bundle_manifest_sha256"] != o["bundle_manifest_sha256"]):
        return "observed-only/held-evidence-mismatch"
    return "client-observed/release-matched"


def characterize_channel(o, trusted_runtime_evidence=None):
    """Synthetic already-checked owner evidence; not a runtime-proof verifier."""
    scope = {key: o[key] for key in ("installation_id", "native_build", "bundle_manifest_sha256")}
    evidence = trusted_runtime_evidence or {}
    if (evidence.get("authority") == "runtime-distribution-owner"
            and evidence.get("kind") == "testflight-runtime"
            and evidence.get("scope") == scope and evidence.get("observed_at") == o["observed_at"]
            and logical_ref(evidence.get("underlying_runtime_ref")) and logical_ref(evidence.get("record_ref"))):
        return {"channel": "testflight", "eligible": True, "basis": "verified_testflight_runtime",
                "ref": evidence["record_ref"]}
    return {"channel": "unknown", "eligible": False,
            "basis": "sandbox_receipt_only" if o["channel_hint"] == "sandbox_receipt_only" else "unverified",
            "ref": None}


class LaunchProvenanceTests(unittest.TestCase):
    def test_annotation_only_schema_delta_preserves_all_wire_constraints(self):
        path = "schemas/bloodbank/project/app_launch.recorded.json"
        current = json.loads((ROOT / path).read_text())
        self.assertEqual(hashlib.sha256(canonical(constraints(current)).encode()).hexdigest(),
                         ACCEPTED_CONSTRAINT_SHA256)
        self.assertEqual(current["properties"]["schemaref"]["const"], APP_LAUNCH_TYPE + ".v1")
        self.assertEqual(current["properties"]["data"]["properties"]["schema_version"]["const"], 1)

    def test_existing_five_envelopes_and_ids_stay_valid(self):
        expected = {"implementation": "47eb32b3-f0d2-516d-a7f9-b77d32a7c681",
                    "ready": "aee4ab32-7326-578d-8b2b-b73e77d2bd2f",
                    "low_water": "14b91329-a325-5640-bcb7-6dc398f12e8c",
                    "item": "47ee22cb-0503-5f7a-adde-0f629c8bb307",
                    "launch": "38ef5256-6902-5bfb-baa1-6762ec34656b"}
        for case in CASES:
            e = envelope_for(case)
            before = canonical(e)
            assert_contract(e)
            validate_envelope(e)
            self.assertEqual(e["id"], expected[case])
            self.assertEqual(canonical(e), before)

    def test_pinned_original_constraints_accept_unknown_channel_example(self):
        from jsonschema import Draft202012Validator
        e = envelope_for("launch")
        e["data"].update(channel="unknown", testflight_eligible=False,
                         channel_evidence={"basis": "sandbox_receipt_only", "evidence_ref": None})
        assert_contract(e)
        validate_envelope(e)
        original_constraints = constraints(json.loads(
            (ROOT / "schemas/bloodbank/project/app_launch.recorded.json").read_text()))
        self.assertEqual(hashlib.sha256(canonical(original_constraints).encode()).hexdigest(),
                         ACCEPTED_CONSTRAINT_SHA256)
        Draft202012Validator(original_constraints, registry=_build_registry()).validate(e)
        self.assertEqual(e["id"], "38ef5256-6902-5bfb-baa1-6762ec34656b")

    def test_schema_valid_assertions_are_not_runtime_authentication(self):
        e = envelope_for("launch")
        e["data"]["provenance"]["record_ref"] = "urn:contract-fixture:unsubstantiated-claim"
        assert_contract(e)
        validate_envelope(e)  # Deliberate counterexample: validators cannot inspect remote reality.
        o = observation()
        self.assertEqual(characterize_admission(o, None, None), "observed-only/pending-evidence")
        forged_identical_post = json.loads(canonical(o))
        self.assertEqual(characterize_admission(o, source_policy(), release_for(o)),
                         characterize_admission(forged_identical_post, source_policy(), release_for(o)))
        self.assertEqual(canonical(o), canonical(forged_identical_post))

    def test_new_observations_are_not_manually_digest_allowlisted(self):
        o = observation()
        original_digest = hashlib.sha256(canonical(o).encode()).hexdigest()
        policy, release = source_policy(), release_for(o)
        self.assertEqual(characterize_admission(o, policy, release), "client-observed/release-matched")
        later = copy.deepcopy(o)
        later.update(installation_id="31ff09c7-dee7-4139-a797-228c06b0ff67",
                     observed_at="2026-11-08T01:00:00Z")
        self.assertNotEqual(hashlib.sha256(canonical(later).encode()).hexdigest(), original_digest)
        self.assertEqual(characterize_admission(later, policy, release), "client-observed/release-matched")
        self.assertNotIn("observations", policy)

    def test_missing_source_checks_or_final_release_evidence_never_qualifies(self):
        o = observation()
        for missing in SOURCE_CHECKS:
            policy = source_policy()
            del policy["checks"][missing]
            self.assertEqual(characterize_admission(o, policy, release_for(o)),
                             "observed-only/pending-evidence")
        for release in (None, {**release_for(o), "authority": "client"},
                        {**release_for(o), "stage": "archive-only"},
                        {**release_for(o), "record_ref": None}):
            self.assertEqual(characterize_admission(o, source_policy(), release),
                             "observed-only/pending-evidence")

    def test_tuple_and_exact_raw_manifest_mismatches_are_held_not_normalized(self):
        o = observation()
        candidates = []
        for key in o["native_build"]:
            release = release_for(o)
            release["native_build"][key] += "1"
            candidates.append(release)
        candidates.append({**release_for(o), "bundle_manifest_sha256": "0" * 64})
        before = canonical(o)
        for release in candidates:
            self.assertEqual(characterize_admission(o, source_policy(), release),
                             "observed-only/held-evidence-mismatch")
        self.assertEqual(canonical(o), before)

    def test_observed_only_is_not_a_canonical_false_flag_branch(self):
        for flag in FLAGS:
            e = envelope_for("launch")
            e["data"][flag] = False
            # Stdlib identity checks are not a replacement for full schema validation.
            assert_contract(e)
            with self.assertRaises(EnvelopeInvalid):
                validate_envelope(e)

    def test_client_cannot_select_flags_policy_or_producer_through_observation(self):
        for key in (*FLAGS, "producer", "actor", "provenance", "admission", "policy", "channel"):
            o = observation()
            o[key] = True
            self.assertEqual(characterize_admission(o, source_policy(), release_for(o)),
                             "invalid-observation")

    def test_release_match_store_upload_and_sandbox_do_not_establish_testflight(self):
        o = observation()
        for kind in ("release-match", "sandbox-receipt", "store-bundled-only", "asc-upload", "beta-available"):
            evidence = {"authority": "release-owner", "kind": kind,
                        "scope": {key: o[key] for key in ("installation_id", "native_build",
                                                        "bundle_manifest_sha256")},
                        "record_ref": "urn:contract-fixture:not-runtime-distribution"}
            result = characterize_channel(o, evidence)
            self.assertEqual((result["channel"], result["eligible"]), ("unknown", False))
            self.assertIsNone(result["ref"])

    def test_testflight_requires_independent_runtime_evidence_bound_to_original_observation(self):
        o = observation()
        evidence = {"authority": "runtime-distribution-owner", "kind": "testflight-runtime",
                    "scope": {key: o[key] for key in ("installation_id", "native_build",
                                                    "bundle_manifest_sha256")},
                    "observed_at": o["observed_at"], "record_ref": "urn:contract-fixture:channel-record",
                    "underlying_runtime_ref": "urn:contract-fixture:underlying-runtime-evidence"}
        self.assertTrue(characterize_channel(o, evidence)["eligible"])
        for key in ("scope", "observed_at", "underlying_runtime_ref", "record_ref"):
            bad = copy.deepcopy(evidence)
            bad[key] = None
            self.assertFalse(characterize_channel(o, bad)["eligible"])

    def test_bad_provenance_and_channel_conditions_remain_rejected(self):
        for field, value in (("provenance", {"basis": "client_assertion", "record_ref": "urn:fixture:x"}),
                             ("native_identity_source", "android_diagnostic"),
                             ("provenance", {"basis": "tolt_native_first_startup",
                                             "record_ref": "https://fixture.invalid/no-logical-record"})):
            e = envelope_for("launch")
            e["data"][field] = value
            with self.assertRaises((EnvelopeInvalid, ContractViolation)):
                validate_envelope(e)
        e = envelope_for("launch")
        e["data"]["channel"] = "unknown"  # Positive TF evidence/eligibility cannot survive this.
        with self.assertRaises(ContractViolation):
            assert_contract(e)
        with self.assertRaises(ContractViolation):
            validate_envelope(e)

    def test_evidence_arrival_and_retries_preserve_original_observation_identity(self):
        o = observation()
        before = canonical(o)
        self.assertEqual(characterize_admission(o, source_policy(), None), "observed-only/pending-evidence")
        e = envelope_for("launch")
        self.assertEqual(characterize_admission(o, source_policy(), release_for(o)),
                         "client-observed/release-matched")
        frozen = canonical(e)
        id_before = app_launch_event_id(e["data"])
        # Channel/time/evidence are not ID inputs, but changing them WOULD change frozen bytes.
        later = copy.deepcopy(e)
        later["data"]["observed_at"] = "2026-11-08T01:00:00Z"
        later["data"]["provenance"]["record_ref"] = "urn:contract-fixture:newer-evidence"
        self.assertEqual(app_launch_event_id(later["data"]), id_before)
        self.assertNotEqual(canonical(later), frozen)
        self.assertEqual(canonical(e), frozen)  # Return/retry saved first bytes, not later.
        self.assertEqual(canonical(o), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
