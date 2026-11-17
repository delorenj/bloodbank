import json

import pytest

from target_watch import envelope as env
from target_watch.bus import Publisher
from target_watch.gmail import Gog, build_query, email_envelope, plain_text, poll
from target_watch.matcher import match


def gog_message(message_id="18c0ffee", thread_id="18c0ffee", sender="Jane Doe <Jane.Doe@Example.com>",
                subject="Gutter estimate", body="Hi,\n\nCan you send the estimate?\n", to="Operator <operator@example.net>"):
    return {
        "body": body,
        "headers": {"from": sender, "to": to, "subject": subject, "date": "Wed, 04 Nov 2026 09:43:10 -0800", "cc": "", "bcc": ""},
        "message": {"id": message_id, "threadId": thread_id, "internalDate": "1793814190000", "labelIds": ["INBOX"]},
    }


class FakeGog:
    def __init__(self, messages, fail_search=False):
        self.messages = {m["message"]["id"]: m for m in messages}
        self.fail_search = fail_search
        self.calls = []

    def __call__(self, argv, cwd=None, timeout=None):
        self.calls.append(list(argv))
        if argv[1:3] == ["gmail", "messages"]:
            if self.fail_search:
                return 1, "", "token expired"
            hits = [{"id": i, "threadId": m["message"].get("threadId")} for i, m in self.messages.items()]
            return 0, json.dumps({"messages": hits, "nextPageToken": ""}), ""
        if argv[1:3] == ["gmail", "get"]:
            return 0, json.dumps(self.messages[argv[3]]), ""
        raise AssertionError(argv)


def test_query_is_the_union_of_target_addresses():
    assert build_query(["b@example.com", "a@example.com", "a@example.com"]) == (
        "from:(a@example.com OR b@example.com) newer_than:2d in:anywhere -in:sent -in:drafts"
    )
    assert build_query([]) is None


def test_envelope_is_exactly_email_received(index, registry):
    envelope = email_envelope(gog_message(), "operator@example.net")
    env.validate(envelope)
    data = envelope["data"]
    assert envelope["id"] == env.gmail_event_id("18c0ffee")
    assert envelope["ordering_key"] == "messaging:email:18c0ffee"
    assert data["direction"] == "inbound" and data["sender"] == {"handle": "jane.doe@example.com", "display_name": "Jane Doe"}
    assert data["recipient"] == {"handle": "operator@example.net"}
    assert data["content"] == {"text": "Gutter estimate\n\nHi,\n\nCan you send the estimate?", "format": "plain"}
    assert data["received_at"] == "2026-11-04T17:43:10.000Z"
    assert (data["message_id"], data["thread_id"]) == ("18c0ffee", "18c0ffee")
    # ...and the matcher reads it through x-sender like any other provider.
    [det] = match(envelope, index.get(envelope["type"]), registry.current())
    assert det.matched_kind == "email" and det.provider == "email"


def test_html_bodies_become_plain_text():
    text = plain_text("<html><head><style>p{}</style></head><body><p>Hello</p><div>World &amp; co</div></body></html>")
    assert "Hello" in text and "World & co" in text and "<" not in text and "p{}" not in text
    assert len(plain_text("x" * 20000)) == 8000


async def test_poll_publishes_new_target_mail_once(registry, state, fake_js):
    fake = FakeGog([gog_message(), gog_message(message_id="18c0ffef", sender="stranger@example.net")])
    gog = Gog(fake, account="operator@example.net")
    summary = await poll(registry.current(), gog, state, Publisher(fake_js))
    assert summary == {"query_matches": 2, "new": 2, "published": 1, "skipped": 1, "failed": 0}
    query = fake.calls[0][4]
    assert query.startswith("from:(jane.doe@example.com OR jd@example.org)")
    assert fake.calls[0][5:] == ["-a", "operator@example.net", "-j", "--all"]
    [(subject, event, headers)] = fake_js.calls
    assert subject == "bloodbank.evt.messaging.email.received" and headers["Nats-Msg-Id"] == event["id"]

    again = await poll(registry.current(), gog, state, Publisher(fake_js))
    assert again["new"] == 0 and len(fake_js.calls) == 1


async def test_poll_failures(registry, state, fake_js):
    with pytest.raises(RuntimeError):
        await poll(registry.current(), Gog(FakeGog([], fail_search=True)), state, Publisher(fake_js))
    broken = gog_message()
    del broken["message"]["threadId"]
    summary = await poll(registry.current(), Gog(FakeGog([broken])), state, Publisher(fake_js))
    assert summary["failed"] == 1 and not state.gmail_seen("18c0ffee")


async def test_poll_without_email_handles_is_a_no_op(state, fake_js):
    summary = await poll([], Gog(FakeGog([])), state, Publisher(fake_js))
    assert summary["published"] == 0 and "note" in summary
