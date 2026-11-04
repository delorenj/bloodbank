import json

import pytest

from conftest import JANE_PHONE, call_ended, email, inbound, rcs, telegram, transcription
from target_watch import envelope as env
from target_watch.matcher import detection_envelope, match, provider_for
from target_watch.xsender import XSenderError, XSenderIndex, parse_block, scan

INBOUND_TELEPHONY = {
    "bloodbank.telephony.call.ended",
    "bloodbank.telephony.call.recorded",
    "bloodbank.telephony.transcription.completed",
}


def test_every_inbound_schema_carries_a_valid_x_sender(schema_tree):
    """The guarantee 'add a provider and every watch picks it up' rests on this.

    Before the schemas track lands the tree is a fixture overlay and this checks
    only that the fixtures are well formed; afterwards it checks the real schemas.
    """
    index, errors = scan(schema_tree)
    assert errors == []
    received = {
        json.loads(p.read_text())["properties"]["type"]["const"]
        for p in (schema_tree / "bloodbank" / "messaging").glob("*.received.json")
    }
    assert received, "no messaging *.received schemas found"
    missing = (received | INBOUND_TELEPHONY) - set(index)
    assert not missing, f"inbound schemas without x-sender: {sorted(missing)}"


def test_parse_block_refuses_bad_shapes():
    good = {"handle_paths": ["data.from"], "name_paths": [], "thread_path": None, "text_paths": [],
            "time_path": "data.received_at", "inbound_when": None, "handle_kinds": ["e164"]}
    assert parse_block("bloodbank.messaging.sms.received", good).handle_kinds == ("e164",)
    for broken in (
        {k: v for k, v in good.items() if k != "handle_kinds"},
        {**good, "handle_kinds": ["phone"]},
        {**good, "handle_paths": []},
        {**good, "handle_paths": ["from"]},
        {**good, "inbound_when": {"path": "data.direction"}},
    ):
        with pytest.raises(XSenderError):
            parse_block("bloodbank.messaging.sms.received", broken)


def test_a_new_provider_is_matched_without_code_changes(schema_tree, tmp_path, registry):
    """Copy the tree, add a provider schema with x-sender, and the index (and every watch) sees it."""
    import shutil

    tree = tmp_path / "tree"
    shutil.copytree(schema_tree, tree)
    index = XSenderIndex(tree, recheck_s=0)
    new_type = "bloodbank.messaging.signal.received"
    event = inbound(new_type, {"sender_number": JANE_PHONE, "body": "new provider hello", "at": "2026-11-04T16:00:00Z"})
    assert index.get(new_type) is None
    (tree / "bloodbank" / "messaging" / "signal.received.json").write_text(json.dumps({
        "$id": "https://33god.dev/schemas/bloodbank/messaging/signal.received.json",
        "x-sender": {"handle_paths": ["data.sender_number"], "name_paths": [], "thread_path": None,
                     "text_paths": ["data.body"], "time_path": "data.at", "inbound_when": None,
                     "handle_kinds": ["e164"]},
        "properties": {"type": {"const": new_type}},
    }))
    xs = index.get(new_type)
    assert xs is not None
    [det] = match(event, xs, registry.current())
    assert det.provider == "signal" and det.matched_kind == "e164" and det.extracted.text == "new provider hello"


def test_rcs_by_number(index, registry):
    [det] = match(rcs(), index.get("bloodbank.messaging.rcs.received"), registry.current())
    assert (det.target.id, det.provider, det.matched_kind, det.matched_value) == ("jane-doe", "rcs", "e164", JANE_PHONE)
    assert det.extracted.thread_id == "thread-9" and det.group_label is None


def test_rcs_by_contact_label_and_group_title(index, registry):
    xs = index.get("bloodbank.messaging.rcs.received")
    [det] = match(rcs(sender="Jane Doe"), xs, registry.current())
    assert (det.matched_kind, det.matched_value, det.group_label) == ("label", "jane doe", None)
    [det] = match(rcs(sender="Crew: Jane Doe", thread_id=None), xs, registry.current())
    assert (det.matched_kind, det.group_label) == ("label", "Crew")
    assert det.extracted.thread_id is None


def test_rcs_from_name_counts_as_a_label(index, registry):
    [det] = match(rcs(sender="+12155550111", from_name="Janie D"), index.get("bloodbank.messaging.rcs.received"), registry.current())
    assert det.matched_kind == "label"


def test_email_matches_the_address_never_the_display_name(index, registry):
    xs = index.get("bloodbank.messaging.email.received")
    [det] = match(email(sender="JANE.DOE@EXAMPLE.COM"), xs, registry.current())
    assert (det.matched_kind, det.matched_value) == ("email", "jane.doe@example.com")
    assert match(email(sender="impostor@example.net", display_name="Jane Doe"), xs, registry.current()) == []
    assert match(email(direction="outbound"), xs, registry.current()) == []


def test_platform_user(index, registry):
    xs = index.get("bloodbank.messaging.telegram.received")
    [det] = match(telegram(), xs, registry.current())
    assert (det.provider, det.matched_kind, det.matched_value) == ("telegram", "platform_user", "telegram:4242")
    assert match(telegram(handle="999"), xs, registry.current()) == []


def test_voicemail_events_share_provider_and_thread(index, registry):
    ended = match(call_ended(), index.get("bloodbank.telephony.call.ended"), registry.current())[0]
    spoken = match(transcription(), index.get("bloodbank.telephony.transcription.completed"), registry.current())[0]
    assert ended.provider == spoken.provider == "voicemail"
    assert ended.extracted.thread_id == spoken.extracted.thread_id == "call-77"
    assert ended.extracted.text == "" and ended.extracted.description == "call ended (voicemail)"
    assert spoken.extracted.text.startswith("Hi, it's Jane")
    assert provider_for("bloodbank.telephony.call.ended", {"disposition": "answered"}) == "call"
    # Outbound calls are not inbound messages.
    assert match(call_ended(direction="outbound"), index.get("bloodbank.telephony.call.ended"), registry.current()) == []


def test_no_match_and_unknown_shapes(index, registry):
    xs = index.get("bloodbank.messaging.rcs.received")
    assert match(rcs(sender="+12155550999"), xs, registry.current()) == []
    assert match({"type": "bloodbank.messaging.rcs.received", "id": "x", "data": "nope"}, xs, registry.current()) == []
    assert match({"type": "bloodbank.messaging.rcs.received", "id": "x", "data": {}}, xs, registry.current()) == []


def test_detection_envelope_is_contract_valid_and_deterministic(index, registry):
    event = rcs(text="x" * 5000)
    [det] = match(event, index.get("bloodbank.messaging.rcs.received"), registry.current())
    first = detection_envelope(det)
    env.validate(first)
    assert first["id"] == env.detection_id("jane-doe", "bloodbank.messaging.rcs.received:rcs-1")
    assert first["ordering_key"] == "watch:jane-doe"
    assert first["causationid"] == event["id"] and first["correlationid"] == event["correlationid"]
    assert first["subject"] == "bloodbank.evt.watch.target.detected"
    data = first["data"]
    assert len(data["content"]["text"]) == 4000
    assert data["sender"] == {"handle": JANE_PHONE, "display_name": None}
    assert data["matched_on"] == {"kind": "e164", "value": JANE_PHONE}
    assert data["conversation"] == {"thread_id": "thread-9", "group_label": None}
    [again] = match(event, index.get("bloodbank.messaging.rcs.received"), registry.current())
    assert detection_envelope(again)["id"] == first["id"]


def test_every_live_family_builds_a_valid_detection(index, registry):
    for event in (rcs(), email(), call_ended(), transcription(), telegram()):
        [det] = match(event, index.get(event["type"]), registry.current())
        env.validate(detection_envelope(det))


async def test_non_uuid_source_id_is_a_permanent_refusal(index, registry, state, publisher, fake_js):
    from target_watch.bus import Permanent
    from target_watch.match_service import MatchService

    service = MatchService(index, registry, state, publisher)
    with pytest.raises(Permanent, match="not a UUID"):
        await service.handle(rcs(event_id="n8n-not-a-uuid"))
    assert fake_js.events == []


def test_ids_use_the_published_namespace():
    import uuid

    key = "jim:bloodbank.messaging.rcs.received:msg-1"
    assert env.detection_id("jim", "bloodbank.messaging.rcs.received:msg-1") == str(uuid.uuid5(uuid.NAMESPACE_URL, key))
    assert env.routed_id("abc", "ticket_created") == str(uuid.uuid5(uuid.NAMESPACE_URL, "abc:ticket_created"))


def test_telephony_twins_share_one_dedupe_key_per_type(index, registry):
    """Live n8n sends call.ended twice per call with different event ids; the call id is the key."""
    ended = index.get("bloodbank.telephony.call.ended")
    a = match(call_ended(event_id="0b9c6a62-1111-4d0e-8f5f-0000000000a1"), ended, registry.current())[0]
    b = match(call_ended(event_id="0b9c6a62-1111-4d0e-8f5f-0000000000a2"), ended, registry.current())[0]
    other_call = match(call_ended(call_id="call-78"), ended, registry.current())[0]
    spoken = match(transcription(), index.get("bloodbank.telephony.transcription.completed"), registry.current())[0]
    assert a.extracted.dedupe_key == b.extracted.dedupe_key == "bloodbank.telephony.call.ended:call-77"
    assert a.event_id == b.event_id != other_call.event_id
    assert spoken.extracted.dedupe_key == "bloodbank.telephony.transcription.completed:call-77"
    # a messaging type without message_id never keys on its thread
    msg = rcs()
    msg["data"]["message_id"] = None
    [det] = match(msg, index.get(msg["type"]), registry.current())
    assert det.extracted.dedupe_key == f"{msg['type']}:{msg['id']}"
