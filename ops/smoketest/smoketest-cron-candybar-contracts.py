#!/usr/bin/env python3
"""Offline, fixture-driven CAND-51 contract proof. Never publishes."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "agent-hooks"))
sys.path.insert(0, str(ROOT / "scripts"))

from core import validate  # noqa: E402
from candybar_codegen_schema import codegen_schema  # noqa: E402

FIXTURES = json.loads((ROOT / "ops/fixtures/cron-candybar-contracts.json").read_text())
CREATED = validate.CRON_CREATED_TYPE
DELETED = validate.CRON_DELETED_TYPE


def fixture(key: str, **changes) -> dict:
    env = copy.deepcopy(FIXTURES[key])
    env["data"].update(changes)
    return env


def raw_validate(env: dict) -> None:
    """Bypass assert_contract to test the actual schema's revision union."""
    from jsonschema import Draft202012Validator

    Draft202012Validator(
        validate.load_event_schema(env["type"]),
        registry=validate._build_registry(),
        format_checker=validate._draft202012_format_checker(Draft202012Validator),
    ).validate(env)


class CandybarContract(unittest.TestCase):
    def accept(self, env: dict) -> None:
        validate.assert_contract(env)
        validate.validate_envelope(env)
        raw_validate(env)

    def reject(self, env: dict) -> None:
        for check in (validate.assert_contract, validate.validate_envelope):
            with self.subTest(check=check.__name__):
                with self.assertRaises(validate.ContractViolation):
                    check(env)

    def test_v1_payload_schema_is_exactly_preserved(self):
        selected = validate.load_event_schema(CREATED, schema_version=1)
        digest = hashlib.sha256(
            json.dumps(selected["properties"]["data"], sort_keys=True).encode()
        ).hexdigest()
        # Semantic hash of the entire original data schema at 38633e90.
        self.assertEqual(digest, "df5b0a7a5b4cc027ea426dde0d533d5ca6efacc005ae6dada12defe1d7cbdc17")

    def test_updated_file_is_byte_identical(self):
        self.assertEqual(
            hashlib.sha256((ROOT / "schemas/bloodbank/cron/candybar.updated.json").read_bytes()).hexdigest(),
            "2fb2432a1fcd8d07ea2774e3cb77b36786d0b6e0d8a5d94f2d2943302aa7c6cd",
        )

    def test_default_lookup_retains_dispatching_schema(self):
        schema = validate.load_event_schema(CREATED)
        self.assertEqual(len(schema["oneOf"]), 2)
        self.assertEqual(schema, validate.load_schema_for(CREATED))
        with self.assertRaisesRegex(ValueError, "select schema_version"):
            validate.load_event_data_schema(CREATED)
        for revision, required, forbidden in ((1, "schedule", "dates"), (2, "dates", "schedule")):
            selected = validate.load_event_schema(CREATED, schema_version=revision)
            self.assertEqual(selected["allOf"][0]["$ref"], "../../_common/cloudevent_base.v1.json")
            data = validate.load_event_data_schema(CREATED, schema_version=revision)
            self.assertIn(required, data["required"])
            self.assertNotIn(forbidden, data["properties"])
            self.assertFalse(data["additionalProperties"])
        for revision in (0, 3, True, "2", 2.0):
            with self.assertRaises(ValueError):
                validate.load_event_schema(CREATED, schema_version=revision)
        # Selecting one branch never mutates the cached dispatching schema.
        self.assertEqual(len(validate.load_event_schema(CREATED)["oneOf"]), 2)

    def test_v1_minimal_and_optional_allowances_unchanged(self):
        self.accept(fixture("created_v1"))
        for zone in ("UTC", "America/New_York", "Legacy/Not-IANA", " "):
            for enabled in (True, False):
                with self.subTest(zone=zone, enabled=enabled):
                    self.accept(fixture("created_v1", timezone=zone, enabled=enabled))
        # No new cron parser or whitespace restrictions, either.
        self.accept(fixture("created_v1", schedule=" "))
        # Preserve the exact original anchored-regex semantics, not just its
        # intended slug vocabulary. New representations have strict slugs.
        self.accept(fixture("created_v1", name="legacy-name\n"))

    def test_v1_rejects_dates_and_missing_schedule(self):
        self.reject(fixture("created_v1", dates=["2028-02-29T12:34"]))
        env = fixture("created_v1")
        del env["data"]["schedule"]
        self.reject(env)

    def test_v2_minute_and_zero_seconds(self):
        for dates in (["2028-02-29T12:34"], ["2028-02-29T12:34:00"],
                      ["2028-02-29T12:34", "2028-03-01T12:34:00"]):
            self.accept(fixture("created_v2", dates=dates))
        self.accept(fixture("created_v2", enabled=False, timezone="UTC"))

    def test_owner_policies_are_not_selected_by_wire_validator(self):
        # Past dates, a DST gap and a fold are representations, not a scheduling
        # disposition. Do not resolve/reject them on the owner's behalf.
        for date in ("2000-01-01T12:00", "2027-03-14T02:30", "2027-11-07T01:30"):
            self.accept(fixture("created_v2", dates=[date], timezone="America/New_York"))

    def test_v2_missing_empty_or_mixed_variant(self):
        for field in ("dates", "timezone"):
            env = fixture("created_v2")
            del env["data"][field]
            self.reject(env)
        for dates in ([], None, {}, "2028-01-01T00:00", [None], [42]):
            self.reject(fixture("created_v2", dates=dates))
        self.reject(fixture("created_v2", schedule="* * * * *"))

    def test_v2_real_calendars_and_exact_local_grammar(self):
        for value in (
            "2027-02-29T12:34", "2028-02-30T12:34", "2028-04-31T12:34",
            "0000-01-01T12:34", "2028-13-01T12:34", "2028-00-01T12:34",
            "2028-01-00T12:34", "2028-01-01T24:00", "2028-01-01T12:60",
            "2028-01-01T12:34:01", "2028-01-01T12:34:60",
            "2028-01-01T12:34:00.0", "2028-01-01T12:34.0",
            "2028-01-01T12:34Z", "2028-01-01T12:34:00Z",
            "2028-01-01T12:34+00:00", "2028-01-01T12:34-05:00",
            "2028-01-01 12:34", "2028-01-01t12:34", "20280101T1234",
            "2028-01-01T1:34", "2028-01-01T12:34\n",
        ):
            with self.subTest(value=value):
                self.reject(fixture("created_v2", dates=[value]))

    def test_v2_actual_iana_lookup(self):
        for zone in ("", "Mars/Olympus", "+00:00", "america/new_york",
                     "/usr/share/zoneinfo/UTC", "../UTC", "localtime", "right/UTC",
                     None, 1):
            with self.subTest(zone=zone):
                self.reject(fixture("created_v2", timezone=zone))

    def test_v2_duplicate_and_equivalent_dates(self):
        for dates in (
            ["2028-01-01T12:34", "2028-01-01T12:34"],
            ["2028-01-01T12:34", "2028-01-01T12:34:00"],
            ["2028-01-01T12:34:00", "2028-01-01T12:34"],
        ):
            self.reject(fixture("created_v2", dates=dates))

    def test_handles_and_payload_revision_must_agree(self):
        from jsonschema import ValidationError

        for key in ("created_v1", "created_v2", "deleted"):
            for field in ("dataschema", "schemaref"):
                for revision in (0, 1, 2, 3):
                    env = fixture(key)
                    if revision == env["data"]["schema_version"]:
                        continue
                    env[field] = (f"apicurio://holyfields/{env['type']}/versions/{revision}"
                                  if field == "dataschema" else f"{env['type']}.v{revision}")
                    self.reject(env)
                    with self.assertRaises(ValidationError):
                        raw_validate(env)
                env = fixture(key)
                del env[field]
                self.reject(env)
            for revision in (None, True, 0, 3, "1", [], {}):
                self.reject(fixture(key, schema_version=revision))

    def test_created_common_metadata_required_and_closed(self):
        for key in ("created_v1", "created_v2"):
            for field in ("schema_version", "candybar_id", "name", "description", "tags", "noise"):
                env = fixture(key)
                del env["data"][field]
                self.reject(env)
            for field, value in (("description", None), ("tags", [""]), ("tags", "tag"),
                                 ("noise", 0), ("enabled", None), ("enabled", 1),
                                 ("name", "Bad_Slug"), ("candybar_id", "not-a-uuid"),
                                 ("extra", True)):
                self.reject(fixture(key, **{field: value}))

    def test_deleted_reasons_and_explicit_null_or_uuid(self):
        for reason in ("manual", "schedule_exhausted"):
            for last in (None, "f9301fae-7cb6-4bec-ae34-4f784e2c61d7"):
                self.accept(fixture("deleted", reason=reason, last_invocation_id=last))
        self.accept(fixture("deleted", deleted_at="2026-11-03T20:00:00.123+00:00"))

    def test_deleted_missing_fields(self):
        for field in FIXTURES["deleted"]["data"]:
            env = fixture("deleted")
            del env["data"][field]
            self.reject(env)

    def test_deleted_bad_values_and_unknown_keys(self):
        for field, values in {
            "candybar_id": ("bad", "", "b0a1d5207e674faead548725725a2503", None),
            "last_invocation_id": ("bad", "", {}, 0),
            "name": ("Bad-name", "bad_name", "-bad", "bad-", "", "bad\n"),
            "deleted_at": ("2026-02-30T00:00:00Z", "2026-11-03T20:00:00-05:00",
                           "2026-11-03T20:00:00+01:00", "2026-11-03T20:00:00-00:00",
                           "2026-11-03T20:00:00", "2026-11-03", "not-time", None),
            "reason": ("archived", "paused", "unknown", None),
            "extra": (True,),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.reject(fixture("deleted", **{field: value}))

    def test_ordering_identity_exact(self):
        for key in FIXTURES:
            for ordering in ("candybar:other", "cron:any", FIXTURES[key]["ordering_key"] + " ",
                             FIXTURES[key]["ordering_key"].upper()):
                env = fixture(key)
                env["ordering_key"] = ordering
                self.reject(env)

    def test_type_subject_kind_and_tense_negatives(self):
        self.assertIn("deleted", validate.EVENT_ACTIONS)
        self.assertNotIn("deleted", validate.COMMAND_ACTIONS)
        for key in FIXTURES:
            for field, value in (("subject", "bloodbank.evt.cron.candybar.updated"),
                                 ("type", "bloodbank.v2.cron.candybar.created"),
                                 ("type", "bloodbank.cron.candybar.create"),
                                 ("type", "bloodbank.cron.candybar.delete"),
                                 ("kind", "command"), ("domain", "agent")):
                env = fixture(key)
                env[field] = value
                self.reject(env)

    def test_allof_refs_and_branches_validate_without_network(self):
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            for key in FIXTURES:
                self.accept(fixture(key))
            # Both branch UUID refs really resolve, not merely the v1 branch.
            from jsonschema import ValidationError
            for key in FIXTURES:
                with self.assertRaises(ValidationError):
                    raw_validate(fixture(key, candybar_id="not-a-uuid"))

    def test_codegen_view_preserves_branch_and_base_constraints(self):
        from jsonschema import Draft202012Validator

        for action, keys in (("created", ("created_v1", "created_v2")), ("deleted", ("deleted",))):
            view = codegen_schema(action)
            Draft202012Validator.check_schema(view)
            self.assertNotIn('"$ref"', json.dumps(view))
            expanded = Draft202012Validator(
                view, format_checker=validate._draft202012_format_checker(Draft202012Validator),
            )
            for key in keys:
                env = fixture(key)
                expanded.validate(env)
                for missing in ("data", "id", "actor", "schemaref", "dataschema"):
                    bad = copy.deepcopy(env)
                    del bad[missing]
                    self.assertFalse(expanded.is_valid(bad))
                for field in ("dataschema", "schemaref"):
                    bad = {**env, field: env[field] + "-wrong"}
                    self.assertFalse(expanded.is_valid(bad))
                for field in ("candybar_id", "schema_version"):
                    bad = copy.deepcopy(env)
                    bad["data"][field] = "bad"
                    self.assertFalse(expanded.is_valid(bad))
                bad = copy.deepcopy(env)
                bad["data"]["extra"] = True
                self.assertFalse(expanded.is_valid(bad))
        created = codegen_schema("created")
        self.assertEqual(len(created["oneOf"]), 2)
        self.assertIn("schedule", created["oneOf"][0]["properties"]["data"]["required"])
        self.assertNotIn("dates", created["oneOf"][0]["properties"]["data"]["properties"])
        self.assertIn("dates", created["oneOf"][1]["properties"]["data"]["required"])
        self.assertNotIn("schedule", created["oneOf"][1]["properties"]["data"]["properties"])
        for version in (1, 2):
            selected = codegen_schema("created", schema_version=version)
            self.assertEqual(selected["properties"]["data"]["properties"]["schema_version"]["const"], version)

    def test_codegen_ref_guard_fails_unknown_local_or_network_refs(self):
        real_load = validate.load_event_schema

        def unsupported_ref(ce_type, **kwargs):
            schema = copy.deepcopy(real_load(ce_type, **kwargs))
            if kwargs.get("schema_version") is not None:
                schema["properties"]["data"]["properties"]["candybar_id"] = {
                    "$ref": "https://example.invalid/not-a-supported-primitive.json",
                }
            return schema

        with patch("candybar_codegen_schema.load_event_schema", side_effect=unsupported_ref):
            with self.assertRaisesRegex(ValueError, "unsupported CAND-51 codegen ref"):
                codegen_schema("created")


if __name__ == "__main__":
    unittest.main(verbosity=2)
