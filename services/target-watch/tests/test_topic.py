"""Topic targets: watch a KIND of email (a vendor's payment failure) from any sender, not a person."""
from __future__ import annotations

import pytest

from target_watch import envelope as env
from target_watch.bus import Publisher
from target_watch.gmail import Gog, build_topic_query, email_envelope, poll
from target_watch.matcher import detection_data, detection_envelope, match
from target_watch.sink import headline, notify_line
from target_watch.targets import TargetError, TargetRegistry, fold_words, parse_target, subject_phrase

from conftest import write_target
from test_gmail import FakeGog, gog_message

TOPIC = """
[target]
id = "vendor-billing"
display_name = "Vendor billing"

[target.topic]
subject_any = ["Past due", "payment failed", "Payment FAILED", "not received payment"]

[sink]
kind = "notify_only"
notify = ["telegram:1000"]
"""


@pytest.fixture
def topic_registry(tmp_path, jane_file):
    write_target(jane_file.parent, "vendor-billing", TOPIC)
    return TargetRegistry([str(jane_file.parent / "*.toml")])


def test_a_topic_needs_no_handles_and_folds_its_phrases(tmp_path):
    import tomllib

    target = parse_target(tomllib.loads(TOPIC), tmp_path / "t.toml")
    assert target.handle_count() == 0
    assert target.subject_any == ("past due", "payment failed", "not received payment")  # folded, deduped, file order


@pytest.mark.parametrize("body, error", [
    ('[target]\nid = "x"\ndisplay_name = "X"\n[sink]\nkind = "notify_only"\n', "at least one handle or"),
    ('[target]\nid = "x"\ndisplay_name = "X"\n[target.topic]\nsubject_any = ["say \\"hi\\""]\n[sink]\nkind = "notify_only"\n',
     "double quotes"),
    ('[target]\nid = "x"\ndisplay_name = "X"\n[target.topic]\nsubject_any = ["  "]\n[sink]\nkind = "notify_only"\n',
     "non-empty"),
    ('[target]\nid = "x"\ndisplay_name = "X"\n[target.topic]\nsender_any = ["a"]\n[sink]\nkind = "notify_only"\n',
     "unknown topic keys"),
])
def test_bad_topics_are_refused(tmp_path, body, error):
    import tomllib

    with pytest.raises(TargetError, match=error):
        parse_target(tomllib.loads(body), tmp_path / "t.toml")


def test_phrases_match_whole_words_only():
    assert fold_words("Action required – Your AWS account is PAST-DUE!") == "action required your aws account is past due"
    assert subject_phrase("Your AWS account is past due", ("past due",)) == "past due"
    assert subject_phrase("Payment failed for invoice #IN-1", ("past due", "payment failed")) == "payment failed"
    assert subject_phrase("Unpast duet tonight", ("past due",)) is None  # no match inside other words
    assert subject_phrase(None, ("past due",)) is None


def test_gmail_topic_query_keeps_marketing_spam_and_trash_out():
    query = build_topic_query(["payment failed", "past due", "past due"])
    assert query == ('subject:("past due" OR "payment failed") newer_than:2d '
                     "-in:sent -in:drafts -in:spam -in:trash -category:promotions")
    assert build_topic_query([]) is None


def test_any_sender_with_a_topic_subject_is_detected(index, topic_registry):
    envelope = email_envelope(gog_message(sender="Amazon Web Services <payment@vendor.example>",
                                          subject="Action required – Your account is past due",
                                          body="Invoice 1 for $1.00 is unpaid."), "operator@example.net")
    [det] = match(envelope, index.get(envelope["type"]), topic_registry.current())
    assert (det.target.id, det.matched_kind, det.matched_value) == ("vendor-billing", "subject", "past due")
    assert det.extracted.subject == "Action required – Your account is past due"
    built = detection_envelope(det)
    env.validate(built)  # 'subject' is part of the detected contract
    data = detection_data(det)
    assert headline(data, 120) == "Amazon Web Services: Action required – Your account is past due"
    assert notify_line(data, "notified", None).startswith("Vendor billing via email: Amazon Web Services: Action")


def test_a_person_match_wins_and_off_topic_mail_is_ignored(index, topic_registry):
    from_jane = email_envelope(gog_message(subject="Your invoice is past due"), "operator@example.net")
    dets = {d.target.id: d for d in match(from_jane, index.get(from_jane["type"]), topic_registry.current())}
    assert dets["jane-doe"].matched_kind == "email" and dets["vendor-billing"].matched_kind == "subject"

    receipt = email_envelope(gog_message(sender="billing@vendor.example", subject="Your receipt"), "operator@example.net")
    assert match(receipt, index.get(receipt["type"]), topic_registry.current()) == []


async def test_poll_reads_topic_mail_from_strangers_and_nothing_else(topic_registry, state, fake_js):
    fake = FakeGog([
        gog_message(message_id="a1", sender="payment@vendor.example", subject="We have not received payment for October"),
        gog_message(message_id="a2", sender="news@vendor.example", subject="Star suspended for a week"),
    ])
    summary = await poll(topic_registry.current(), Gog(fake, account="operator@example.net"), state, Publisher(fake_js))
    assert summary == {"query_matches": 2, "new": 2, "published": 1, "skipped": 1, "failed": 0}
    queries = [call[4] for call in fake.calls if call[1:3] == ["gmail", "messages"]]
    assert queries[0].startswith("from:(jane.doe@example.com") and queries[1].startswith('subject:("not received payment"')
    [(_, event, _)] = fake_js.calls
    assert event["data"]["sender"]["handle"] == "payment@vendor.example"


async def test_topic_only_targets_still_poll(tmp_path, state, fake_js):
    path = write_target(tmp_path / "only", "vendor-billing", TOPIC)
    registry = TargetRegistry([str(path)])
    fake = FakeGog([gog_message(message_id="b1", sender="x@vendor.example", subject="Payment failed")])
    summary = await poll(registry.current(), Gog(fake), state, Publisher(fake_js))
    assert summary["published"] == 1
    assert [c[4] for c in fake.calls if c[1:3] == ["gmail", "messages"]][0].startswith("subject:(")


def test_replies_forwards_and_our_own_alerts_are_not_news():
    phrases = ("past due",)
    assert subject_phrase("Re: Your account is past due", phrases) is None
    assert subject_phrase("FWD: Your account is past due", phrases) is None
    assert subject_phrase("RE[2]: Your account is past due", phrases) is None
    assert subject_phrase("\U0001f6a8 Payment problem: AWS: Your account is past due", phrases) is None
    assert subject_phrase("Refund: your account is past due", phrases) == "past due"  # 'Re' must be a prefix word


def test_email_channel_mails_the_whole_message(tmp_path, index, state, fake_js):
    import tomllib

    from target_watch.sink import SinkService, email_subject, mask_channel

    body = TOPIC.replace('notify = ["telegram:1000"]', 'notify = ["telegram:1000", "email:Partner@Example.com"]')
    path = write_target(tmp_path / "w", "vendor-billing", body)
    target = parse_target(tomllib.loads(body), path)
    assert target.sink.notify == ("telegram:1000", "email:partner@example.com")
    assert mask_channel("email:partner@example.com") == "email:p...@example.com"

    calls = []

    def runner(argv, cwd=None, timeout=None):
        calls.append(argv)
        if argv[1:3] == ["gmail", "send"]:
            return 0, "id\t19abc\nthread_id\t19abc\n", ""
        return 0, '{"success": true}', ""

    envelope = email_envelope(gog_message(sender="Cloudflare <noreply@notify.vendor.example>",
                                          subject="[Action required] Payment failed for invoice #IN-1",
                                          body="Your card was declined. Amount due: $12.00"), "operator@example.net")
    [det] = match(envelope, index.get(envelope["type"]), [target])
    data = detection_data(det)
    assert email_subject(data, "Payment problem").startswith("\U0001f6a8 Payment problem: Cloudflare: [Action required]")

    import asyncio
    sink = SinkService(TargetRegistry([str(path)]), state, Publisher(fake_js), runner=runner)
    sent, failed = asyncio.run(sink._notify(list(target.sink.notify), "line", target, None,
                                            detection_id=det.event_id, data=data))
    assert failed == [] and set(sent) == {"telegram:1000", "email:partner@example.com"}
    [mail] = [c for c in calls if c[1:3] == ["gmail", "send"]]
    assert mail[mail.index("--to") + 1] == "partner@example.com"
    assert "Your card was declined. Amount due: $12.00" in mail[mail.index("--body") + 1]
    assert "From: Cloudflare <noreply@notify.vendor.example>" in mail[mail.index("--body") + 1]
