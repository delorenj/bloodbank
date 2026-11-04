import json
from types import SimpleNamespace

import pytest

from conftest import FakeJS, rcs
from target_watch.bus import Permanent, Publisher, handle_message
from target_watch.match_service import MatchService


def service(index, registry, state, publisher, **kwargs):
    return MatchService(index, registry, state, publisher, **kwargs)


async def test_publishes_one_detection_with_msg_id(index, registry, state, publisher, fake_js):
    published = await service(index, registry, state, publisher).handle(rcs())
    assert len(published) == 1
    subject, event, headers = fake_js.calls[0]
    assert subject == "bloodbank.evt.watch.target.detected"
    assert headers == {"Nats-Msg-Id": event["id"]} and event["id"] == published[0]["id"]


async def test_ignores_types_without_x_sender_and_its_own_events(index, registry, state, publisher, fake_js):
    svc = service(index, registry, state, publisher)
    assert await svc.handle({"type": "bloodbank.messaging.sms.sent", "id": "x", "data": {}}) == []
    detection = (await svc.handle(rcs()))[0]
    assert await svc.handle(detection) == []
    assert len(fake_js.calls) == 1


async def test_near_duplicates_within_two_seconds_publish_once(index, registry, state, publisher, fake_js):
    svc = service(index, registry, state, publisher)
    first = rcs(message_id="rcs-a", received_at="2026-11-04T15:00:00.000Z")
    twin = rcs(message_id="rcs-b", received_at="2026-11-04T15:00:00.010Z", event_id="0b9c6a62-1111-4d0e-8f5f-0000000000aa")
    later = rcs(message_id="rcs-c", received_at="2026-11-04T15:00:05Z", event_id="0b9c6a62-1111-4d0e-8f5f-0000000000ab")
    other_text = rcs(message_id="rcs-d", text="different", received_at="2026-11-04T15:00:00.020Z",
                     event_id="0b9c6a62-1111-4d0e-8f5f-0000000000ac")
    for event in (first, twin, later, other_text):
        await svc.handle(event)
    texts = [(e["data"]["dedupe_key"]) for e in fake_js.events]
    assert texts == [
        "bloodbank.messaging.rcs.received:rcs-a",
        "bloodbank.messaging.rcs.received:rcs-c",
        "bloodbank.messaging.rcs.received:rcs-d",
    ]


async def test_redelivery_after_failed_publish_publishes_the_same_id(index, registry, state):
    js = FakeJS(fail=1)
    svc = service(index, registry, state, Publisher(js))
    with pytest.raises(ConnectionError):
        await svc.handle(rcs())
    await svc.handle(rcs())  # the redelivery is not its own near-duplicate
    assert len(js.calls) == 1


async def test_contract_refusal_is_permanent(index, registry, state, publisher):
    def refuse(_):
        raise ValueError("domain 'watch' not in allowlist")
    with pytest.raises(Permanent):
        await service(index, registry, state, publisher, validate=refuse).handle(rcs())


class Message:
    def __init__(self, payload, delivered=1):
        self.data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.metadata = SimpleNamespace(num_delivered=delivered)
        self.calls = []

    async def ack(self):
        self.calls.append("ack")

    async def nak(self, delay=None):
        self.calls.append(("nak", delay))

    async def term(self):
        self.calls.append("term")


async def test_handle_message_dispositions():
    async def ok(event):
        return None

    async def flaky(event):
        raise ConnectionError("bus down")

    async def hopeless(event):
        raise Permanent("bad")

    message = Message({"type": "bloodbank.messaging.rcs.received"})
    assert await handle_message(message, ok, name="t") == "ack" and message.calls == ["ack"]

    message = Message({"type": "x"}, delivered=2)
    assert await handle_message(message, flaky, name="t", nak_delay=10) == "nak"
    assert message.calls == [("nak", 20)]

    message = Message({"type": "x"}, delivered=5)
    assert await handle_message(message, flaky, name="t", max_attempts=5) == "term"

    message = Message({"type": "x"})
    assert await handle_message(message, hopeless, name="t") == "term"

    for junk in (b"not json", b"[1,2]", b'{"no":"type"}'):
        message = Message(junk)
        assert await handle_message(message, ok, name="t") == "term"
