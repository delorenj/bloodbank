"""Synthetic BB-34 contract vectors. Never publish these as production facts."""
import copy

from core.validate import game_event_id, game_ordering_key, subject_for, app_launch_event_id

CORRELATION = "8dc2014d-f387-4d34-b478-dc599d755750"
IMPLEMENTATION = "b416b035-2219-57db-91ef-f84a4928606e"
EPISODE = "44c9d238-0347-48b4-8820-1b4565e73065"
INSTALLATION = "bf3aa422-355e-4e52-a306-d0fd911c2852"
NATIVE = {"bundle_id": "dev.example.tolt", "app_version": "0.1.0", "build_number": "42"}

COMMON = {
    "schema_version": 1,
    "project_slug": "contract-fixture",
    "observed_at": "2026-11-07T20:00:00Z",
    "correlation_id": CORRELATION,
    "cause_event_id": None,
    "summary": "Synthetic BB-34 contract fixture; not a production fact",
    "provenance": {
        "basis": "",
        "record_ref": "urn:bb34:fixture:source",
        "source_commit": None,
    },
}

CASES = {
    "implementation": (
        "bloodbank.game.implementation.started", "forge_build_phase_running",
        {"implementation_id": IMPLEMENTATION, "job_id": "job-fixture",
         "concept_id": "993cc768-6141-47a1-a3d6-4142c7ff9440",
         "milestone": "build_started", "game_kind": "new_game"},
    ),
    "low_water": (
        "bloodbank.game.concept_pool.breached", "forge_low_water_episode",
        {"pool_id": "curated-plane-novel", "episode_id": EPISODE,
         "episode_schema": "ltf.concept-low-water/1", "eligible_count": 0,
         "threshold": 5, "complete": True},
    ),
    "item": (
        "bloodbank.game.item.created", "forge_item_landed",
        {"item_id": "fixture-thing", "item_name": "Fixture Thing",
         "content_status": "ready", "registry_status": "not_observed",
         "item_sha256": "a" * 64, "art_sha256": "b" * 64, "request_id": None},
    ),
    "ready": (
        "bloodbank.game.pack.released", "tolt_pack_readback",
        {"pack": {"id": "fixture-pack", "version": 2, "sha256": "c" * 64},
         "title": "Fixture Pack", "source_ticket": None, "source_job": "job-fixture",
         "changelog": "An exact fixture update", "release_kind": "update",
         "playable_url": "https://tolt.delo.sh/play/fixture-pack?v=2",
         "catalog_revision": "fixture-catalog-2", "game_commit": "f" * 40,
         "verification": {"catalog_verified": True, "exact_version_verified": True,
                          "artifact_verified": True, "player_assets_verified": True, "load_verified": True},
         "channels": [{"channel": "web", "verified": True,
                       "evidence_ref": "urn:bb34:fixture:readback", "native_build": None}]},
    ),
    "launch": (
        "bloodbank.project.app_launch.recorded", "tolt_native_first_startup",
        {"schema_version": 1, "project": {"slug": "contract-fixture"},
         "observed_at": "2026-11-07T20:00:00Z", "summary": "Synthetic app launch fixture",
         "installation_id": INSTALLATION, "installation_id_origin": "persisted_csprng_uuid4",
         "native_build": NATIVE, "native_identity_source": "ios_bundle",
         "final_native_identity_verified": True, "channel": "testflight", "testflight_eligible": True,
         "channel_evidence": {"basis": "verified_testflight_runtime", "evidence_ref": "urn:bb34:fixture:channel"},
         "bundle_manifest_sha256": "d" * 64, "installation_verified": True,
         "bundle_verified": True, "native_boot_verified": True,
         "provenance": {"basis": "tolt_native_first_startup", "record_ref": "urn:bb34:fixture:app-start"}},
    ),
}


def envelope_for(case):
    ce_type, basis, fields = CASES[case]
    data = copy.deepcopy(fields if case == "launch" else {**COMMON, **fields})
    data["provenance"]["basis"] = basis
    if case in ("item", "ready"):
        data["provenance"]["source_commit"] = "e" * 40
    return {
        "specversion": "1.0", "id": app_launch_event_id(data) if case == "launch" else game_event_id(ce_type, data),
        "source": "urn:bb34:fixture:producer", "type": ce_type,
        "subject": subject_for(ce_type, "event"), "time": "2026-11-07T20:00:01Z",
        "datacontenttype": "application/json",
        "dataschema": f"apicurio://holyfields/{ce_type}/versions/1",
        "schemaref": f"{ce_type}.v1", "kind": "event", "domain": ce_type.split(".")[1],
        "correlationid": data.get("correlation_id", CORRELATION), "causationid": data.get("cause_event_id"),
        "producer": "bb34-contract-fixture", "service": "bb34-contract-fixture",
        "actor": {"type": "service", "agent_id": "bloodbank.test.bb34"},
        "ordering_key": (f"project:{data['project']['slug']}:installation:{data['installation_id']}" if case == "launch" else game_ordering_key(ce_type, data)), "data": data,
    }
