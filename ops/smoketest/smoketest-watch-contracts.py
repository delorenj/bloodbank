#!/usr/bin/env python3
"""Watch domain contract smoketest.

Proves, against the real schema tree and validator:

1. x-sender guarantee (docs/event-naming.md section 10.1). Every
   messaging.*.received schema and every listed telephony inbound schema carries
   a valid top-level `x-sender` block, and every path in it resolves to a
   property of that schema's `data`. This is what makes "add a provider and
   every watch picks it up" true: a provider schema without x-sender fails here.
2. bloodbank.watch.target.detected and .routed validate as full envelopes with
   deterministic ids, and refuse unknown fields, a mismatched ordering bucket,
   an oversized text and a routed ticket action without a ticket reference.

Handles in this file are fake. Never put a real person's handle in this public repo.

Run: python3 ops/smoketest/smoketest-watch-contracts.py
"""
from __future__ import annotations

import copy
import json
import re
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "services" / "agent-hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from core.validate import (  # noqa: E402
    ContractViolation,
    EnvelopeInvalid,
    assert_contract,
    subject_for,
    validate_envelope,
)

SCHEMAS = ROOT / "schemas" / "bloodbank"

# Telephony types that carry an inbound party. A new inbound telephony type is added here
# when it is added to the schema tree; messaging is discovered by glob and needs no edit.
TELEPHONY_INBOUND = ("call.ended", "call.recorded", "transcription.completed")

HANDLE_KINDS = frozenset({"email", "e164", "label", "platform_user"})
X_SENDER_KEYS = frozenset({
    "handle_paths", "name_paths", "thread_path", "text_paths",
    "time_path", "inbound_when", "handle_kinds",
})
NAMESPACE = uuid.NAMESPACE_URL


def inbound_schema_paths() -> list[Path]:
    paths = sorted((SCHEMAS / "messaging").glob("*.received.json"))
    paths += [SCHEMAS / "telephony" / f"{name}.json" for name in TELEPHONY_INBOUND]
    return paths


def resolve(schema: dict, dotted: str) -> dict | None:
    """Walk 'data.a.b' through schema.properties; return the leaf schema or None."""
    node = schema.get("properties", {})
    leaf = None
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        leaf = node[part]
        node = leaf.get("properties", {}) if isinstance(leaf, dict) else {}
    return leaf


def is_stringish(leaf: dict) -> bool:
    t = leaf.get("type")
    types = t if isinstance(t, list) else [t]
    return "string" in types


def check_x_sender(schema: dict) -> list[str]:
    """Return a list of defects; empty means the block is valid for this schema."""
    block = schema.get("x-sender")
    if not isinstance(block, dict):
        return ["no x-sender block"]
    problems: list[str] = []
    extra = set(block) - X_SENDER_KEYS
    missing = X_SENDER_KEYS - set(block)
    if extra:
        problems.append(f"unknown keys {sorted(extra)}")
    if missing:
        problems.append(f"missing keys {sorted(missing)}")
    if problems:
        return problems

    def path_ok(path, what: str) -> None:
        if not isinstance(path, str) or not path.startswith("data."):
            problems.append(f"{what} {path!r} must be a dotted path starting with 'data.'")
            return
        leaf = resolve({"properties": schema["properties"]}, path)
        if leaf is None:
            problems.append(f"{what} {path!r} does not resolve to a property of the schema")
        elif not is_stringish(leaf):
            problems.append(f"{what} {path!r} is not a string property")

    handles = block["handle_paths"]
    if not isinstance(handles, list) or not handles:
        problems.append("handle_paths must be a non-empty list")
    else:
        for p in handles:
            path_ok(p, "handle_paths")
    for key in ("name_paths", "text_paths"):
        if not isinstance(block[key], list):
            problems.append(f"{key} must be a list (empty when the event has none)")
        else:
            for p in block[key]:
                path_ok(p, key)
    path_ok(block["thread_path"], "thread_path")
    path_ok(block["time_path"], "time_path")

    kinds = block["handle_kinds"]
    if (not isinstance(kinds, list) or not kinds or len(set(kinds)) != len(kinds)
            or not set(kinds) <= HANDLE_KINDS):
        problems.append(f"handle_kinds must be a non-empty list of unique values from {sorted(HANDLE_KINDS)}")

    data_props = schema["properties"]["data"].get("properties", {})
    inbound = block["inbound_when"]
    if inbound is None:
        if "direction" in data_props:
            problems.append("data has a 'direction' property, so inbound_when must be set (outbound events must not alert)")
    elif not (isinstance(inbound, dict) and set(inbound) == {"path", "equals"}):
        problems.append("inbound_when must be null or {path, equals}")
    else:
        path_ok(inbound["path"], "inbound_when.path")
        eq = inbound["equals"]
        if not isinstance(eq, list) or not eq or not all(isinstance(v, str) for v in eq):
            problems.append("inbound_when.equals must be a non-empty list of strings")
        else:
            leaf = resolve({"properties": schema["properties"]}, inbound["path"]) or {}
            enum = leaf.get("enum")
            if enum and not set(eq) <= set(enum):
                problems.append(f"inbound_when.equals {eq} not within enum {enum}")
    return problems


class XSenderContract(unittest.TestCase):
    def test_inbound_schema_set_is_not_empty(self) -> None:
        paths = inbound_schema_paths()
        self.assertGreaterEqual(len(paths), 13)
        for p in paths:
            self.assertTrue(p.exists(), f"listed inbound schema missing: {p}")

    def test_every_inbound_schema_has_valid_x_sender(self) -> None:
        failures = {}
        for path in inbound_schema_paths():
            problems = check_x_sender(json.loads(path.read_text()))
            if problems:
                failures[path.relative_to(ROOT).as_posix()] = problems
        self.assertEqual(
            failures, {},
            "inbound schemas need a valid x-sender block (docs/event-naming.md 10.1); "
            "a provider without one is invisible to every watch",
        )

    def test_checker_rejects_defects(self) -> None:
        good = json.loads((SCHEMAS / "messaging" / "slack.received.json").read_text())
        self.assertEqual(check_x_sender(good), [])
        cases = {
            "absent": lambda s: s.pop("x-sender"),
            "unresolvable handle": lambda s: s["x-sender"].update(handle_paths=["data.sender.nope"]),
            "empty handles": lambda s: s["x-sender"].update(handle_paths=[]),
            "bad kind": lambda s: s["x-sender"].update(handle_kinds=["phone"]),
            "direction without inbound_when": lambda s: s["x-sender"].update(inbound_when=None),
            "inbound value outside enum": lambda s: s["x-sender"]["inbound_when"].update(equals=["sideways"]),
            "non-data path": lambda s: s["x-sender"].update(thread_path="thread_id"),
            "unknown key": lambda s: s["x-sender"].update(extra=1),
        }
        for name, mutate in cases.items():
            bad = copy.deepcopy(good)
            mutate(bad)
            self.assertTrue(check_x_sender(bad), name)

    def test_x_sender_leaves_type_and_enum_untouched(self) -> None:
        # x-sender is an annotation: the schema still validates as Draft 2020-12.
        from jsonschema import Draft202012Validator
        for path in inbound_schema_paths():
            Draft202012Validator.check_schema(json.loads(path.read_text()))


FAKE_SOURCE_ID = "6f1c2b0e-0000-5000-8000-000000000001"


def detection_id(watch_id: str, dedupe_key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{watch_id}:{dedupe_key}"))


def detected_envelope(**data_overrides) -> dict:
    data = {
        "schema_version": 1,
        "watch_id": "example-person",
        "target_display_name": "Example Person",
        "provider": "rcs",
        "source_type": "bloodbank.messaging.rcs.received",
        "source_event_id": FAKE_SOURCE_ID,
        "matched_on": {"kind": "e164", "value": "+12155550100"},
        "sender": {"handle": "+1 (215) 555-0100", "display_name": "Example Person"},
        "conversation": {"thread_id": "thread-1", "group_label": None},
        "content": {"text": "Can you call me back?", "subject": None},
        "received_at": "2026-11-04T15:00:00Z",
        "dedupe_key": "bloodbank.messaging.rcs.received:msg-1",
    }
    data.update(data_overrides)
    t = "bloodbank.watch.target.detected"
    return {
        "specversion": "1.0",
        "id": detection_id(data["watch_id"], data["dedupe_key"]),
        "source": "urn:33god:service:target-watch",
        "type": t,
        "subject": subject_for(t, "event"),
        "time": "2026-11-04T15:00:01Z",
        "datacontenttype": "application/json",
        "dataschema": f"apicurio://holyfields/{t}/versions/1",
        "correlationid": FAKE_SOURCE_ID,
        "causationid": FAKE_SOURCE_ID,
        "producer": "target-watch",
        "service": "target-watch",
        "domain": "watch",
        "kind": "event",
        "schemaref": f"{t}.v1",
        "actor": {"type": "service", "agent_id": "bloodbank.service.target-watch"},
        "ordering_key": f"watch:{data['watch_id']}",
        "data": data,
    }


def routed_envelope(**data_overrides) -> dict:
    det = detected_envelope()
    data = {
        "schema_version": 1,
        "watch_id": "example-person",
        "detection_event_id": det["id"],
        "action": "ticket_created",
        "ticket_ref": "EXM-12",
        "notify_channels": ["telegram:000000"],
        "reason": None,
        "routed_at": "2026-11-04T15:00:09Z",
    }
    data.update(data_overrides)
    t = "bloodbank.watch.target.routed"
    return {
        "specversion": "1.0",
        "id": str(uuid.uuid5(NAMESPACE, f"{data['detection_event_id']}:{data['action']}")),
        "source": "urn:33god:service:target-watch",
        "type": t,
        "subject": subject_for(t, "event"),
        "time": "2026-11-04T15:00:09Z",
        "datacontenttype": "application/json",
        "dataschema": f"apicurio://holyfields/{t}/versions/1",
        "correlationid": FAKE_SOURCE_ID,
        "causationid": data["detection_event_id"],
        "producer": "target-watch",
        "service": "target-watch",
        "domain": "watch",
        "kind": "event",
        "schemaref": f"{t}.v1",
        "actor": {"type": "service", "agent_id": "bloodbank.service.target-watch"},
        "ordering_key": f"watch:{data['watch_id']}",
        "data": data,
    }


FAILS = (ContractViolation, EnvelopeInvalid)


class WatchEvents(unittest.TestCase):
    def test_types_pass_the_naming_contract(self) -> None:
        for env in (detected_envelope(), routed_envelope()):
            assert_contract(env)

    def test_detected_validates(self) -> None:
        validate_envelope(detected_envelope())

    def test_detected_group_and_email_and_call_shapes_validate(self) -> None:
        validate_envelope(detected_envelope(
            provider="telegram", source_type="bloodbank.messaging.telegram.received",
            matched_on={"kind": "platform_user", "value": "telegram:123"},
            conversation={"thread_id": "-100", "group_label": "Example Group"},
            dedupe_key="bloodbank.messaging.telegram.received:msg-2"))
        validate_envelope(detected_envelope(
            provider="email", source_type="bloodbank.messaging.email.received",
            matched_on={"kind": "email", "value": "person@example.test"},
            content={"text": "Subject\n\nBody", "subject": "Subject"}, dedupe_key="bloodbank.messaging.email.received:gmail-abc"))
        validate_envelope(detected_envelope(
            provider="voicemail", source_type="bloodbank.telephony.transcription.completed",
            matched_on={"kind": "e164", "value": "+12155550100"},
            sender={"handle": "+12155550100", "display_name": None},
            conversation={"thread_id": "call-1", "group_label": None},
            content={"text": "", "subject": None}, dedupe_key="bloodbank.telephony.transcription.completed:call-1"))

    def test_detected_is_closed(self) -> None:
        env = detected_envelope()
        env["data"]["extra"] = 1
        with self.assertRaises(FAILS):
            validate_envelope(env)
        env = detected_envelope()
        env["data"]["matched_on"]["kind"] = "carrier_pigeon"
        with self.assertRaises(FAILS):
            validate_envelope(env)

    def test_detected_text_is_capped(self) -> None:
        validate_envelope(detected_envelope(content={"text": "x" * 4000, "subject": None}))
        with self.assertRaises(FAILS):
            validate_envelope(detected_envelope(content={"text": "x" * 4001, "subject": None}))

    def test_ordering_bucket_names_the_target(self) -> None:
        for env in (detected_envelope(), routed_envelope()):
            env["ordering_key"] = "messaging:email:thread-1"
            with self.assertRaises(FAILS):
                validate_envelope(env)

    def test_ordering_bucket_must_be_this_targets_own(self) -> None:
        for env in (detected_envelope(), routed_envelope()):
            env["ordering_key"] = "watch:someone-else"
            with self.assertRaises(FAILS):
                validate_envelope(env)

    def test_causation_is_the_event_answered(self) -> None:
        env = detected_envelope()
        env["causationid"] = "6f1c2b0e-0000-5000-8000-0000000000ff"
        with self.assertRaises(FAILS):
            validate_envelope(env)
        env = routed_envelope()
        env["causationid"] = FAKE_SOURCE_ID
        with self.assertRaises(FAILS):
            validate_envelope(env)

    def test_telephony_detections_of_one_call_do_not_collide(self) -> None:
        ended = detection_id("example-person", "bloodbank.telephony.call.ended:call-1")
        transcript = detection_id(
            "example-person", "bloodbank.telephony.transcription.completed:call-1")
        self.assertNotEqual(ended, transcript)

    def test_source_type_admits_every_type_the_naming_contract_admits(self) -> None:
        # A provider entity with a digit passes the naming contract (TYPE_REGEX),
        # so its detections must pass the schema too.
        t = "bloodbank.messaging.line2.received"
        validate_envelope(detected_envelope(provider="line2", source_type=t))

    def test_matched_on_kinds_are_the_x_sender_handle_kinds(self) -> None:
        schema = json.loads((SCHEMAS / "watch" / "target.detected.json").read_text())
        enum = schema["properties"]["data"]["properties"]["matched_on"]["properties"]["kind"]["enum"]
        self.assertEqual(frozenset(enum), HANDLE_KINDS)

    def test_detection_id_is_deterministic(self) -> None:
        a = detected_envelope()
        b = detected_envelope()
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(a["id"], detected_envelope(dedupe_key="bloodbank.messaging.rcs.received:msg-9")["id"])
        self.assertTrue(re.fullmatch(r"[0-9a-f-]{36}", a["id"]))

    def test_routed_validates_for_every_action(self) -> None:
        validate_envelope(routed_envelope())
        validate_envelope(routed_envelope(action="ticket_commented", ticket_ref="EXM-12"))
        validate_envelope(routed_envelope(action="notified_only", ticket_ref=None))
        validate_envelope(routed_envelope(action="awaiting_button", ticket_ref=None))
        validate_envelope(routed_envelope(action="skipped_duplicate", ticket_ref=None,
                                          notify_channels=[], reason="already routed"))
        validate_envelope(routed_envelope(action="failed", ticket_ref=None,
                                          reason="px create outcome unknown"))

    def test_routed_ticket_actions_need_a_ticket_ref(self) -> None:
        for action in ("ticket_created", "ticket_commented"):
            with self.assertRaises(FAILS):
                validate_envelope(routed_envelope(action=action, ticket_ref=None))
        with self.assertRaises(FAILS):
            validate_envelope(routed_envelope(ticket_ref="not a ref"))
        with self.assertRaises(FAILS):
            validate_envelope(routed_envelope(action="deleted"))

    def test_routed_id_is_deterministic_per_detection_and_action(self) -> None:
        self.assertEqual(routed_envelope()["id"], routed_envelope()["id"])
        self.assertNotEqual(routed_envelope()["id"], routed_envelope(action="failed", ticket_ref=None)["id"])

    def test_schemas_are_in_the_tree(self) -> None:
        for name in ("target.detected", "target.routed"):
            self.assertTrue((SCHEMAS / "watch" / f"{name}.json").exists(), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
