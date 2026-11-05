"""ring:sip: targets and the ring outcome follower. Fake values only (public repo); Telnyx is faked at the
HTTP layer, the clock and the sleeps are shrunk to nothing."""
import json
from datetime import UTC, datetime

import pytest

from conftest import write_target
from target_watch import sink as sink_module
from target_watch.sink import SinkService, classify_ring, mask_channel
from target_watch.state import State
from target_watch.targets import TargetRegistry, load_targets
from test_ring import (CONNECTION_ID, OP, RING_FROM, TARGET, Clock, FakeTelnyx, _Publisher, burst, clock, detect,  # noqa: F401
                       parse, ring_env)  # the fixtures are re-exported so pytest finds them here

SIP = "sip:example-user@sip.telnyx.com"
CHANNEL = f"ring:{SIP}"
LEG = "11111111-2222-3333-4444-555555555555"


def hangup(cause, sip, source="callee"):
    return {"name": "call.hangup", "leg_id": LEG,
            "payload": {"event_type": "call.hangup", "payload": {"hangup_cause": cause, "sip_hangup_cause": sip,
                                                                  "hangup_source": source}}}


INITIATED = {"name": "call.initiated", "leg_id": LEG, "payload": {"payload": {}}}
ANSWERED = {"name": "call.answered", "leg_id": LEG, "payload": {"payload": {}}}
BUSY = [INITIATED, hangup("user_busy", "603")]
TIMEOUT = [INITIATED, hangup("timeout", "487", "unknown")]
PICKED_UP = [INITIATED, ANSWERED, hangup("normal_clearing", "200")]


class FakeEvents:
    """Telnyx GET /v2/call_events: one scripted answer per call (the last repeats)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def __call__(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": dict(headers)})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return answer, b'{"errors": [{"title": "Internal"}]}'
        return 200, json.dumps({"data": answer}).encode()


@pytest.fixture
def fast(monkeypatch):
    for name in ("RING_OUTCOME_DELAY_S", "RING_OUTCOME_POLL_S", "RING_BUSY_RETRY_S"):
        monkeypatch.setattr(sink_module, name, 0)


class LegTelnyx(FakeTelnyx):
    """The dial answer carries call_leg_id, as the live API's does."""

    def __init__(self):
        super().__init__()
        self.body = json.dumps({"data": {"call_control_id": "v3:fake", "call_leg_id": LEG,
                                         "call_session_id": "sess-1", "is_alive": True}}).encode()


def make(tmp_path, runner, clock, events, *, extra="", notify=None, name="w"):
    telnyx = LegTelnyx()
    notify = notify or ["telegram:1000", CHANNEL]
    write_target(tmp_path / name, "jane", TARGET.format(notify=json.dumps(notify), extra=extra))
    registry = TargetRegistry([str(tmp_path / name / "*.toml")])
    sink = SinkService(registry, State(tmp_path / "state" / "state.db"), _Publisher(), runner=runner, clock=clock,
                       http=telnyx, op=OP, http_get=events)
    return sink, registry, telnyx


def events_of(capsys, name):
    return [json.loads(line) for line in capsys.readouterr().err.splitlines() if f'"event": "{name}"' in line]


def failure_lines(runner):
    return [argv[-1] for argv in runner.of("send") if "did not get through" in argv[-1]]


# --- target validation and masking ---------------------------------------------------


def test_sip_ring_channel_loads_and_lowercases_the_host(tmp_path):
    targets, errors = parse(tmp_path, ["telegram:1000", "ring:sip:Example.User_1+x@SIP.Telnyx.COM"])
    assert errors == []
    assert targets[0].sink.notify == ("telegram:1000", "ring:sip:Example.User_1+x@sip.telnyx.com")


@pytest.mark.parametrize("channel", [
    "ring:sip:", "ring:sip:@sip.telnyx.com", "ring:sip:zqsecret7@", "ring:sip:zqsecret7", "ring:sip:zqsecret7@localhost",
    "ring:sip:zqsecret7@sip.telnyx.com:5060", "ring:sip:zqsecret7@sip.telnyx.com;transport=tcp",
    "ring:sip:zqsecret7@sip.telnyx.com?X-Evil=1", "ring:sip:zqsecret7@sip.telnyx.com\n", "ring:sip:zqsecret7 @sip.telnyx.com",
    "ring:sip:zqsecret7@sip.telnyx.com sip:other@evil.example", "ring:sip:zq;secret@sip.telnyx.com",
    "ring:sip:zq<secret>@sip.telnyx.com", "ring:sip:zqsecret7@b@sip.telnyx.com", "ring:sip:zqsecret7@-bad.example.com",
    "ring:sip:zqsecret7@sip..telnyx.com", "ring:sip:" + "zqsecret7" * 8 + "@sip.telnyx.com", "ring:SIP:zqsecret7@sip.telnyx.com",
    "ring:sips:zqsecret7@sip.telnyx.com", "ring:sip:zqsecret7@sip.telnyx.com\r\nVia: x", "ring:sip:%7asecret7@sip.telnyx.com",
    "ring:sip:zqsecret7@192.168.1.5"
])
def test_malicious_or_malformed_sip_channels_are_refused_without_echoing(tmp_path, channel):
    targets, errors = parse(tmp_path, [channel])
    assert targets == [] and len(errors) == 1
    assert "zq" not in errors[0] and "secret" not in errors[0]


def test_mask_hides_the_sip_user():
    assert mask_channel(CHANNEL) == "ring:sip:...er@sip.telnyx.com"
    assert "example-user" not in mask_channel(CHANNEL)
    assert mask_channel("ring:sip:bob@sip.telnyx.com") == "ring:sip:...@sip.telnyx.com"
    assert mask_channel("ring:+12155550142") == "ring:...0142"


async def test_dial_payload_carries_the_sip_uri_unchanged_and_logs_stay_masked(tmp_path, index, runner, clock, fast, capsys):
    sink, registry, telnyx = make(tmp_path, runner, clock, FakeEvents(PICKED_UP))
    routed = await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    [call] = telnyx.calls
    assert call["body"] == {"connection_id": CONNECTION_ID, "to": SIP, "from": RING_FROM,
                            "timeout_secs": 20, "time_limit_secs": 40}
    assert routed["data"]["notify_channels"] == ["telegram:1000", CHANNEL]
    assert "example-user" not in capsys.readouterr().err
    plan = sink.ring_plan(registry.get("jane-doe"), CHANNEL)
    assert plan["to"] == SIP


# --- classification -------------------------------------------------------------------


@pytest.mark.parametrize("events,kind", [
    (PICKED_UP, "answered"), ([INITIATED, ANSWERED], "answered"), (TIMEOUT, "unanswered"), (BUSY, "busy"),
    ([INITIATED, hangup("unspecified", "486")], "busy"), ([INITIATED, hangup("call_rejected", "403")], "rejected"),
    ([INITIATED], "pending"), ([], "pending"),
])
def test_classify_ring(events, kind):
    assert classify_ring(events)[0] == kind


def test_classify_keeps_the_causes():
    assert classify_ring(BUSY)[1] == {"hangup_cause": "user_busy", "sip_hangup_cause": "603", "hangup_source": "callee"}


# --- outcomes -------------------------------------------------------------------------


async def test_answered_ring_logs_and_sends_no_failure_line(tmp_path, index, runner, clock, fast, capsys):
    events = FakeEvents(PICKED_UP)
    sink, registry, _ = make(tmp_path, runner, clock, events)
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    assert len(events_of(capsys, "ring_answered")) == 1
    assert failure_lines(runner) == []
    [call] = events.calls
    assert call["url"].endswith(f"/v2/call_events?filter%5Bleg_id%5D={LEG}")
    assert call["headers"]["Authorization"] == f"Bearer {runner.op_key}" and "urllib" not in call["headers"]["User-Agent"]


async def test_the_outcome_check_does_not_block_the_detection(tmp_path, index, runner, clock, monkeypatch):
    monkeypatch.setattr(sink_module, "RING_OUTCOME_DELAY_S", 3600)
    events = FakeEvents(PICKED_UP)
    sink, registry, _ = make(tmp_path, runner, clock, events)
    routed = await sink.handle(detect(index, registry, burst(1)))  # returns although the follower sleeps an hour
    assert routed["data"]["notify_channels"] == ["telegram:1000", CHANNEL] and events.calls == []
    for task in list(sink._bg):
        task.cancel()
    await sink.settle()


async def test_unanswered_ring_logs_and_tells_telegram_once(tmp_path, index, runner, clock, fast, capsys):
    sink, registry, _ = make(tmp_path, runner, clock, FakeEvents(TIMEOUT))
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    [record] = events_of(capsys, "ring_unanswered")
    assert record["hangup_cause"] == "timeout"
    [line] = failure_lines(runner)
    assert "Jane Doe" in line and "nobody answered" in line and "example-user" not in line and "..." in line
    send = [a for a in runner.of("send") if "did not get through" in a[-1]][0]
    assert send[:6] == [sink.hermes, "-p", "33god-pm", "send", "--to", "telegram:1000"]


async def test_declined_ring_logs_the_causes_and_tells_telegram_once(tmp_path, index, runner, clock, fast, capsys):
    sink, registry, telnyx = make(tmp_path, runner, clock, FakeEvents([INITIATED, hangup("call_rejected", "403")]))
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    [record] = events_of(capsys, "ring_rejected")
    assert record["hangup_cause"] == "call_rejected" and record["sip_hangup_cause"] == "403" and record["busy"] is False
    [line] = failure_lines(runner)
    assert "declined" in line and "403" in line
    assert len(telnyx.calls) == 1  # not busy: no retry


async def test_busy_phone_is_retried_once_then_reported_once(tmp_path, index, runner, clock, fast, capsys):
    events = FakeEvents(BUSY)  # busy every time
    sink, registry, telnyx = make(tmp_path, runner, clock, events)
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    assert len(telnyx.calls) == 2 and telnyx.calls[0]["body"] == telnyx.calls[1]["body"]  # one retry, never a third
    err = capsys.readouterr().err
    assert err.count('"ring_busy_retry"') == 1 and err.count('"ring_rejected"') == 2
    [line] = failure_lines(runner)
    assert "busy" in line and "retried once" in line
    assert len(events.calls) == 2


async def test_busy_then_answered_on_the_retry_sends_nothing(tmp_path, index, runner, clock, fast, capsys):
    sink, registry, telnyx = make(tmp_path, runner, clock, FakeEvents(BUSY, PICKED_UP))
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    assert len(telnyx.calls) == 2 and failure_lines(runner) == []
    assert '"ring_answered"' in capsys.readouterr().err


async def test_the_retry_neither_uses_nor_resets_the_cooldown(tmp_path, index, runner, clock, fast):
    sink, registry, telnyx = make(tmp_path, runner, clock, FakeEvents(BUSY))
    await sink.handle(detect(index, registry, burst(1)))
    first = sink.state.last_ring("jane-doe", CHANNEL)
    clock.now = datetime(2026, 11, 4, 15, 32, tzinfo=UTC)
    await sink.settle()
    assert len(telnyx.calls) == 2 and sink.state.last_ring("jane-doe", CHANNEL) == first
    # the burst's next text, inside the original cooldown, still does not ring
    await sink.handle(detect(index, registry, burst(2)))
    await sink.settle()
    assert len(telnyx.calls) == 2


async def test_quiet_hours_suppress_the_retry_and_the_failure_is_still_reported(tmp_path, index, runner, fast, capsys):
    clock = Clock(datetime(2026, 11, 4, 14, 0, tzinfo=UTC))  # 09:00 in New York
    sink, registry, telnyx = make(tmp_path, runner, clock, FakeEvents(BUSY), extra='ring_quiet_hours = "09:01-17:00"')
    sink.clock = lambda: datetime(2026, 11, 4, 14, 2, tzinfo=UTC) if len(telnyx.calls) else clock.now  # quiet begins after the first ring
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    assert len(telnyx.calls) == 1
    err = capsys.readouterr().err
    assert '"ring_quiet"' in err and '"retry": true' in err and "ring_busy_retry" not in err
    [line] = failure_lines(runner)
    assert "busy" in line and "quiet hours" in line


async def test_a_refused_retry_dial_is_reported_once(tmp_path, index, runner, clock, fast):
    sink, registry, telnyx = make(tmp_path, runner, clock, FakeEvents(BUSY))
    sink.http = lambda *a: (422, b'{"errors": [{"title": "Bad"}]}') if telnyx.calls else telnyx(*a)
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    assert len(failure_lines(runner)) == 1


@pytest.mark.parametrize("answer", [RuntimeError("boom"), TimeoutError("t"), 500, 401])
async def test_a_failing_events_api_never_raises_and_is_reported_once(tmp_path, index, runner, clock, fast, capsys, answer):
    events = FakeEvents(answer)
    sink, registry, telnyx = make(tmp_path, runner, clock, events)
    routed = await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    assert routed["data"]["notify_channels"] == ["telegram:1000", CHANNEL]
    assert len(events.calls) == sink_module.RING_OUTCOME_POLLS and len(telnyx.calls) == 1  # no retry on an unknown outcome
    assert len(events_of(capsys, "ring_outcome_unknown")) == 1
    [line] = failure_lines(runner)
    assert "could not be read" in line


async def test_no_hangup_yet_is_polled_again_then_resolved(tmp_path, index, runner, clock, fast, capsys):
    events = FakeEvents([INITIATED], PICKED_UP)
    sink, registry, _ = make(tmp_path, runner, clock, events)
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    assert len(events.calls) == 2 and '"ring_answered"' in capsys.readouterr().err and failure_lines(runner) == []


async def test_a_dial_answer_without_a_leg_id_is_reported_unknown(tmp_path, index, runner, clock, fast, capsys):
    events = FakeEvents(PICKED_UP)
    sink, registry, telnyx = make(tmp_path, runner, clock, events)
    telnyx.body = FakeTelnyx().body  # call_control_id only
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    assert events.calls == [] and len(events_of(capsys, "ring_outcome_unknown")) == 1 and len(failure_lines(runner)) == 1


async def test_a_failed_telegram_failure_line_does_not_raise(tmp_path, index, runner, clock, fast):
    sink, registry, _ = make(tmp_path, runner, clock, FakeEvents(TIMEOUT))
    runner.hermes_code = 1
    await sink.handle(detect(index, registry, burst(1)))  # the detection's own telegram line fails too
    await sink.settle()


async def test_e164_rings_are_followed_too(tmp_path, index, runner, clock, fast, capsys):
    sink, registry, _ = make(tmp_path, runner, clock, FakeEvents(PICKED_UP), notify=["telegram:1000", "ring:+12155550142"])
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    assert len(events_of(capsys, "ring_answered")) == 1
