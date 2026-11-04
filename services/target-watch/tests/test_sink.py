import json
from datetime import UTC, datetime, timedelta

import pytest

from conftest import FakeJS, call_ended, email, rcs, transcription, write_target
from target_watch import envelope as env
from target_watch.bus import Permanent, Publisher
from target_watch.matcher import detection_envelope, match
from target_watch.sink import FOOTER, SinkService, first_match, CREATED
from target_watch.state import State
from target_watch.targets import TargetRegistry


def detect(index, registry, event):
    [det] = match(event, index.get(event["type"]), registry.current())
    return detection_envelope(det)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 4, 15, 30, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sink(registry, state, publisher, runner, clock):
    return SinkService(registry, state, publisher, runner=runner, clock=clock)


async def test_creates_a_ticket_then_notifies_then_publishes_routed(sink, index, registry, runner, fake_js, plane_repo):
    detection = detect(index, registry, rcs())
    routed = await sink.handle(detection)

    [create] = runner.of("task")
    _, cwd = runner.calls[0]
    assert cwd == str(plane_repo)
    assert create[:3] == ["px", "task", "create"]
    title = create[3]
    assert title == "[watch] Jane Doe via rcs: Can you come by Tuesday?"
    description = create[create.index("-d") + 1]
    assert "From: +12155550100" in description and "Can you come by Tuesday?" in description
    assert detection["data"]["source_event_id"] in description
    assert description.endswith(FOOTER.format(provider="rcs"))
    assert create[create.index("--state") + 1] == "Todo"
    assert create[create.index("-l") + 1] == "submitted-by-client"
    assert create[create.index("--priority") + 1] == "medium"

    [send] = runner.of("send")
    assert send[:6] == ["hermes", "-p", "33god-pm", "send", "--to", "telegram:1000"] and "--json" in send
    assert send[-1] == "Jane Doe via rcs: Can you come by Tuesday? -> ticket_created JIMB-450"

    assert routed["data"]["action"] == "ticket_created" and routed["data"]["ticket_ref"] == "JIMB-450"
    assert routed["data"]["notify_channels"] == ["telegram:1000"]
    assert routed["id"] == env.routed_id(detection["id"], "ticket_created")
    assert routed["causationid"] == detection["id"] and routed["ordering_key"] == "watch:jane-doe"
    env.validate(routed)
    assert fake_js.calls[-1][2] == {"Nats-Msg-Id": routed["id"]}


async def test_same_conversation_is_coalesced_into_comments(sink, index, registry, runner, clock):
    await sink.handle(detect(index, registry, rcs()))
    clock.now += timedelta(hours=2)
    follow_up = rcs(message_id="rcs-2", text="Also the porch light", received_at="2026-11-04T17:00:00Z",
                    event_id="0b9c6a62-1111-4d0e-8f5f-0000000000b2")
    routed = await sink.handle(detect(index, registry, follow_up))
    [comment] = runner.of("comment")
    assert comment[2] == "JIMB-450" and comment[3] == "-m" and "Also the porch light" in comment[4]
    assert routed["data"]["action"] == "ticket_commented" and routed["data"]["ticket_ref"] == "JIMB-450"
    assert runner.of("send")[-1][-1].endswith("-> ticket_commented JIMB-450")

    other_thread = rcs(message_id="rcs-3", text="new topic", thread_id="thread-10",
                       received_at="2026-11-04T17:05:00Z", event_id="0b9c6a62-1111-4d0e-8f5f-0000000000b3")
    routed = await sink.handle(detect(index, registry, other_thread))
    assert routed["data"]["action"] == "ticket_created" and routed["data"]["ticket_ref"] == "JIMB-451"


async def test_coalescing_window_expires(sink, index, registry, runner, clock):
    await sink.handle(detect(index, registry, rcs()))
    clock.now += timedelta(hours=25)
    later = rcs(message_id="rcs-9", text="next day", received_at="2026-11-05T16:00:00Z",
                event_id="0b9c6a62-1111-4d0e-8f5f-0000000000b9")
    routed = await sink.handle(detect(index, registry, later))
    assert routed["data"]["action"] == "ticket_created" and len(runner.of("task")) == 2


async def test_voicemail_lands_on_one_ticket(sink, index, registry, runner):
    first = await sink.handle(detect(index, registry, call_ended()))
    second = await sink.handle(detect(index, registry, transcription()))
    assert first["data"]["action"] == "ticket_created"
    assert runner.of("task")[0][3] == "[watch] Jane Doe via voicemail: no text (call.ended)"
    assert second["data"]["action"] == "ticket_commented" and second["data"]["ticket_ref"] == first["data"]["ticket_ref"]


async def test_redelivered_detection_acts_once(sink, index, registry, runner, fake_js):
    detection = detect(index, registry, rcs())
    first = await sink.handle(detection)
    again = await sink.handle(detection)
    assert again == first
    assert len(runner.of("task")) == 1 and len(runner.of("send")) == 1 and len(fake_js.calls) == 1


async def test_publish_failure_republishes_without_acting_again(registry, state, runner, clock, index):
    js = FakeJS(fail=1)
    sink = SinkService(registry, state, Publisher(js), runner=runner, clock=clock)
    detection = detect(index, registry, rcs())
    with pytest.raises(ConnectionError):
        await sink.handle(detection)
    routed = await sink.handle(detection)
    assert len(runner.of("task")) == 1 and len(runner.of("send")) == 1
    assert [e["id"] for e in js.events] == [routed["id"]]


async def test_unconfirmed_create_fails_loudly_and_is_never_retried(sink, index, registry, runner, fake_js):
    runner.create_output, runner.create_code = "Error: fetch failed\n", 1
    detection = detect(index, registry, rcs())
    routed = await sink.handle(detection)
    assert routed["data"]["action"] == "failed" and routed["data"]["ticket_ref"] is None
    assert "not retried" in routed["data"]["reason"]
    assert runner.of("send")[0][-1].endswith("-> failed")
    await sink.handle(detection)
    assert len(runner.of("task")) == 1


async def test_crash_while_px_ran_is_failed_not_recreated(sink, index, registry, runner, state):
    detection = detect(index, registry, rcs())
    data = detection["data"]
    state.open_route(detection["id"], "jane-doe", "rcs:thread:thread-9", None, 0)
    state.update_route(detection["id"], px_kind="ticket_created", px_state="pending")
    routed = await sink.handle(detection)
    assert runner.of("task") == []
    assert routed["data"]["action"] == "failed" and "outcome unknown" in routed["data"]["reason"]
    assert len(runner.of("send")) == 1 and data["watch_id"] == "jane-doe"


async def test_px_success_is_read_from_the_first_line_not_the_url():
    out = "created JIMB-450 [Todo] [watch] Jane\n  https://plane.example/x/issues/JIMB-999\n"
    assert first_match(CREATED, out) == "JIMB-450"
    assert first_match(CREATED, "  https://plane.example/created JIMB-1\n") is None


async def test_same_text_on_another_provider_is_skipped(sink, index, registry, runner):
    await sink.handle(detect(index, registry, rcs(text="On my way")))
    sms = rcs(message_id="sms-1", text="on my  way", event_id="0b9c6a62-1111-4d0e-8f5f-0000000000c1")
    sms["type"] = "bloodbank.messaging.sms.received"
    routed = await sink.handle(detect(index, registry, sms))
    assert routed["data"]["action"] == "skipped_duplicate" and "same text" in routed["data"]["reason"]
    assert routed["data"]["notify_channels"] == []
    assert len(runner.of("task")) == 1 and len(runner.of("send")) == 1


async def test_failed_first_detection_does_not_swallow_its_twin(sink, index, registry, runner):
    runner.create_output, runner.create_code = "boom\n", 1
    first = await sink.handle(detect(index, registry, rcs(text="Need you Tuesday")))
    assert first["data"]["action"] == "failed"
    runner.create_output = None
    sms = rcs(message_id="sms-1", text="Need you Tuesday", event_id="0b9c6a62-1111-4d0e-8f5f-0000000000c2")
    sms["type"] = "bloodbank.messaging.sms.received"
    routed = await sink.handle(detect(index, registry, sms))
    assert routed["data"]["action"] == "ticket_created"


async def test_same_text_outside_carrier_twins_is_not_skipped(sink, index, registry, runner):
    await sink.handle(detect(index, registry, rcs(text="On my way")))
    other = rcs(message_id="rcs-2", text="On my way", thread_id="thread-other", event_id="0b9c6a62-1111-4d0e-8f5f-0000000000c3")
    routed = await sink.handle(detect(index, registry, other))
    assert routed["data"]["action"] != "skipped_duplicate"
    assert len(runner.of("task")) + len(runner.of("comment")) == 2


async def test_twin_call_ended_events_make_one_ticket_and_no_comment(sink, index, registry, runner):
    first = await sink.handle(detect(index, registry, call_ended()))
    again = call_ended(event_id="0b9c6a62-1111-4d0e-8f5f-0000000000c4")
    detection = detect(index, registry, again)
    assert detection["id"] == first["causationid"]
    await sink.handle(detection)
    assert len(runner.of("task")) == 1 and runner.of("comment") == []
    assert len(runner.of("send")) == 1


async def test_notify_only_target(tmp_path, index, state, publisher, runner, clock):
    write_target(tmp_path / "w", "self", '[target]\nid="watch-selftest"\ndisplay_name="Watch Selftest"\n'
                 '[target.handles]\nemail=["watch-selftest@delo.sh"]\n[sink]\nkind="notify_only"\nnotify=["telegram:1000","telegram:2000"]')
    registry = TargetRegistry([str(tmp_path / "w" / "*.toml")])
    runner.hermes_code = 0
    sink = SinkService(registry, state, publisher, runner=runner, clock=clock)
    detection = detect(index, registry, email(sender="watch-selftest@delo.sh", text="Selftest\n\nhello"))
    routed = await sink.handle(detection)
    assert runner.of("task") == [] and runner.of("comment") == []
    assert [s[s.index("--to") + 1] for s in runner.of("send")] == ["telegram:1000", "telegram:2000"]
    assert routed["data"]["action"] == "notified_only" and routed["data"]["ticket_ref"] is None


async def test_failed_notification_is_recorded(sink, index, registry, runner):
    runner.hermes_code = 1
    routed = await sink.handle(detect(index, registry, rcs()))
    assert routed["data"]["action"] == "ticket_created" and routed["data"]["notify_channels"] == []
    assert "notify failed: telegram:1000" in routed["data"]["reason"]


async def test_unknown_watch_id_fails_without_side_effects(sink, index, registry, runner, tmp_path):
    detection = detect(index, registry, rcs())
    sink.registry = TargetRegistry([str(tmp_path / "none" / "*.toml")])
    routed = await sink.handle(detection)
    assert routed["data"]["action"] == "failed" and runner.calls == []


async def test_invalid_detection_is_permanent(sink):
    with pytest.raises(Permanent):
        await sink.handle({"type": env.DETECTED, "id": "x", "data": {}})


async def test_dry_run_touches_nothing(registry, index, runner, clock, tmp_path, capsys):
    from target_watch.bus import DryRunPublisher

    state_path = tmp_path / "dry" / "state.db"
    sink = SinkService(registry, State(state_path, read_only=True), DryRunPublisher(), runner=runner, clock=clock, dry_run=True)
    routed = await sink.handle(detect(index, registry, rcs()))
    assert runner.calls == [] and not state_path.exists()
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert printed["action"] == "ticket_created" and printed["px"]["argv"][:3] == ["px", "task", "create"]
    assert printed["notify"][0]["text"].endswith("-> ticket_created <new>")
    assert routed["data"]["action"] == "ticket_created"


async def test_notify_runs_from_home_not_the_service_checkout(sink, index, registry, runner):
    """Notifications run outside the service checkout (2026-11-04: notified=0)."""
    import os

    from target_watch import sink as sink_module

    await sink.handle(detect(index, registry, rcs()))
    [(argv, cwd)] = [(a, c) for a, c in runner.calls if runner.verb(a) == "send"]
    assert cwd == sink_module.NOTIFY_CWD
    assert cwd and not cwd.startswith(os.path.dirname(os.path.dirname(os.path.abspath(sink_module.__file__))))


def test_deploy_artefacts_never_go_through_the_mise_uv_shim_and_pin_binaries():
    """bloodbank's mise PATH shadows hermes with a bloodbank-pm wrapper, so the
    units and the Candybar runner call the venv entry point and absolute binaries."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for unit in ("systemd/target-watch-sink.service", "systemd/target-watch-match.service"):
        text = (root / unit).read_text()
        [exec_line] = [l for l in text.splitlines() if l.startswith("ExecStart=")]
        assert "/.venv/bin/target-watch" in exec_line and "uv run" not in exec_line
        assert "TARGET_WATCH_HERMES=%h/.local/bin/hermes" in text and "TARGET_WATCH_PX=%h/.local/bin/px" in text
    manifest = (root / ".candybars/target-watch-gmail-poll.candybar.toml").read_text()
    [argv] = [l for l in manifest.splitlines() if l.startswith("argv")]
    assert "/.venv/bin/target-watch" in argv and "uv" not in argv.split("[", 1)[1].split(",")[0]


async def test_notify_names_the_hermes_profile_and_trusts_only_a_json_verdict(sink, index, registry, runner):
    """2026-11-04: without -p, hermes found a profile with no Telegram token and
    the sink routed notified=0. An exit 0 without a success verdict is not a send."""
    routed = await sink.handle(detect(index, registry, rcs()))
    [send] = runner.of("send")
    assert send[1:3] == ["-p", "33god-pm"]
    assert routed["data"]["notify_channels"] == ["telegram:1000"]

    from target_watch.sink import confirmed
    assert not confirmed("") and not confirmed('{"error": "x"}') and not confirmed('{"success": true, "skipped": true}')
    assert confirmed('{"success": true, "message_id": "1"}')
