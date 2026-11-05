"""The "ring:<E.164>" notify channel: a Telnyx Call Control call, faked at the HTTP layer and the op binary.

Fake numbers only (+1215555xxxx): this repo is public. Nothing here touches the network.
"""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import rcs, write_target
from target_watch import sink as sink_module
from target_watch.bus import DryRunPublisher
from target_watch.matcher import detection_envelope, match
from target_watch.sink import SinkService, mask_channel
from target_watch.state import State
from target_watch.targets import TargetError, TargetRegistry, in_quiet_hours, load_targets, parse_quiet_hours

RING_TO = "+12155550142"
CONNECTION_ID = "3086124529433446133"
RING_FROM = "+12155550199"
OP = "/fake/bin/op"
SERVICE_ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self, now=None):
        self.now = now or datetime(2026, 11, 4, 15, 30, tzinfo=UTC)  # 10:30 in New York (EST)

    def __call__(self):
        return self.now


class FakeTelnyx:
    """Stands in for the HTTP layer. Never sees a socket."""

    def __init__(self):
        self.calls: list[dict] = []
        self.status = 200
        self.body = json.dumps({"data": {"call_control_id": "v3:fake-call-1", "is_alive": True}}).encode()
        self.error: Exception | None = None

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": json.loads(body), "timeout": timeout})
        if self.error:
            raise self.error
        return self.status, self.body


TARGET = """
[target]
id = "jane-doe"
display_name = "Jane Doe"
[target.handles]
e164 = ["+12155550100"]
[sink]
kind = "notify_only"
notify = {notify}
{extra}
"""


@pytest.fixture(autouse=True)
def ring_env(monkeypatch):
    monkeypatch.setenv("TARGET_WATCH_TELNYX_CONNECTION_ID", CONNECTION_ID)
    monkeypatch.setenv("TARGET_WATCH_RING_FROM", RING_FROM)
    monkeypatch.delenv("TARGET_WATCH_TELNYX_KEY_REF", raising=False)


@pytest.fixture
def telnyx():
    return FakeTelnyx()


@pytest.fixture
def clock():
    return Clock()


def build(tmp_path, runner, telnyx, clock, *, extra="", notify=None, state=None, name="w"):
    notify = notify or ["telegram:1000", f"ring:{RING_TO}"]
    write_target(tmp_path / name, "jane", TARGET.format(notify=json.dumps(notify), extra=extra))
    registry = TargetRegistry([str(tmp_path / name / "*.toml")])
    state = state or State(tmp_path / "state" / "state.db")
    publisher = _Publisher()
    return SinkService(registry, state, publisher, runner=runner, clock=clock, http=telnyx, op=OP), registry


class _Publisher:
    def __init__(self):
        self.published: list[dict] = []

    async def publish(self, envelope):
        self.published.append(envelope)


def burst(n: int):
    return rcs(text=f"message number {n}", message_id=f"ring-{n}", event_id=f"0b9c6a62-1111-4d0e-8f5f-0000000009{n:02d}")


def detect(index, registry, event):
    [det] = match(event, index.get(event["type"]), registry.current())
    return detection_envelope(det)


async def test_ring_places_one_silent_call_after_the_telegram_line(tmp_path, index, runner, telnyx, clock, capsys):
    sink, registry = build(tmp_path, runner, telnyx, clock, notify=[f"ring:{RING_TO}", "telegram:1000"])  # ring listed first
    routed = await sink.handle(detect(index, registry, burst(1)))

    [call] = telnyx.calls
    assert call["url"] == "https://api.telnyx.com/v2/calls"
    assert call["body"] == {"connection_id": CONNECTION_ID, "to": RING_TO, "from": RING_FROM,
                            "timeout_secs": 20, "time_limit_secs": 40}
    assert call["timeout"] == 15
    assert call["headers"]["Authorization"] == f"Bearer {runner.op_key}"
    assert call["headers"]["User-Agent"] and "urllib" not in call["headers"]["User-Agent"].lower()
    assert call["headers"]["Content-Type"] == "application/json"
    # telegram first, then the key lookup, then the ring
    verbs = [runner.verb(argv) for argv, _ in runner.calls]
    assert verbs == ["send", "read"]
    [read] = runner.of("read")
    assert read == [OP, "read", "--no-newline", "op://DeLoSecrets/74k7nz3xkryqns4lpf65a323ue/credential"]
    assert routed["data"]["notify_channels"] == ["telegram:1000", f"ring:{RING_TO}"]
    assert routed["data"]["reason"] is None
    assert RING_TO not in capsys.readouterr().err  # the number is masked in logs


async def test_ring_covers_ticket_actions_too(tmp_path, index, runner, telnyx, clock, plane_repo):
    write_target(tmp_path / "p", "jane", f"""
[target]
id = "jane-doe"
display_name = "Jane Doe"
[target.handles]
e164 = ["+12155550100"]
[sink]
kind = "plane"
repo = "{plane_repo}"
notify = ["telegram:1000", "ring:{RING_TO}"]
""")
    registry = TargetRegistry([str(tmp_path / "p" / "*.toml")])
    sink = SinkService(registry, State(tmp_path / "s.db"), _Publisher(), runner=runner, clock=clock, http=telnyx, op=OP)
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert routed["data"]["action"] == "ticket_created" and len(telnyx.calls) == 1


async def test_telnyx_4xx_is_a_failed_channel_and_logs_status_and_title_only(tmp_path, index, runner, telnyx, clock, capsys):
    telnyx.status = 422
    telnyx.body = json.dumps({"errors": [{"code": "10015", "title": "Bad Request", "detail": "to is invalid"}]}).encode()
    sink, registry = build(tmp_path, runner, telnyx, clock)
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert routed["data"]["notify_channels"] == ["telegram:1000"]
    assert routed["data"]["reason"] == "notify failed: ring:...0142"
    err = capsys.readouterr().err
    record = json.loads([l for l in err.splitlines() if '"ring_failed"' in l][0])
    assert record["status"] == 422 and record["title"] == "Bad Request"
    assert runner.op_key not in err and "Authorization" not in err and "Bearer" not in err
    # a failed ring does not use up the cooldown: the next text tries again
    telnyx.status, telnyx.body = 200, FakeTelnyx().body
    routed = await sink.handle(detect(index, registry, burst(2)))
    assert f"ring:{RING_TO}" in routed["data"]["notify_channels"] and len(telnyx.calls) == 2


async def test_2xx_without_a_call_control_id_is_not_a_success(tmp_path, index, runner, telnyx, clock):
    telnyx.body = b'{"data": {}}'
    sink, registry = build(tmp_path, runner, telnyx, clock)
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert routed["data"]["notify_channels"] == ["telegram:1000"] and "notify failed" in routed["data"]["reason"]


async def test_timeout_is_a_failed_channel(tmp_path, index, runner, telnyx, clock, capsys):
    telnyx.error = TimeoutError("timed out")
    sink, registry = build(tmp_path, runner, telnyx, clock)
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert routed["data"]["notify_channels"] == ["telegram:1000"]
    assert routed["data"]["reason"] == "notify failed: ring:...0142"
    record = json.loads([l for l in capsys.readouterr().err.splitlines() if '"ring_failed"' in l][0])
    assert record["reason"] == "transport" and record["error"] == "TimeoutError"


async def test_a_401_drops_the_cached_key_so_a_rotated_key_is_read_next_time(tmp_path, index, runner, telnyx, clock):
    telnyx.status, telnyx.body = 401, b'{"errors": [{"title": "Authentication failed"}]}'
    sink, registry = build(tmp_path, runner, telnyx, clock)
    await sink.handle(detect(index, registry, burst(1)))
    runner.op_key = "fake-rotated-key"
    telnyx.status, telnyx.body = 200, FakeTelnyx().body
    await sink.handle(detect(index, registry, burst(2)))
    assert len(runner.of("read")) == 2
    assert telnyx.calls[-1]["headers"]["Authorization"] == "Bearer fake-rotated-key"


async def test_key_is_read_once_per_process(tmp_path, index, runner, telnyx, clock):
    sink, registry = build(tmp_path, runner, telnyx, clock)
    await sink.handle(detect(index, registry, burst(1)))
    clock.now += timedelta(minutes=11)
    await sink.handle(detect(index, registry, burst(2)))
    assert len(telnyx.calls) == 2 and len(runner.of("read")) == 1


async def test_key_ref_and_op_binary_come_from_the_environment(tmp_path, index, runner, telnyx, clock, monkeypatch):
    monkeypatch.setenv("TARGET_WATCH_TELNYX_KEY_REF", "op://Vault/some-item/credential")
    sink, registry = build(tmp_path, runner, telnyx, clock)
    await sink.handle(detect(index, registry, burst(1)))
    assert runner.of("read") == [[OP, "read", "--no-newline", "op://Vault/some-item/credential"]]


async def test_unavailable_key_or_missing_config_fails_the_channel_without_a_call(tmp_path, index, runner, telnyx, clock, monkeypatch):
    runner.op_code = 1
    sink, registry = build(tmp_path, runner, telnyx, clock)
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert telnyx.calls == [] and routed["data"]["notify_channels"] == ["telegram:1000"]

    runner.op_code = 0
    monkeypatch.delenv("TARGET_WATCH_RING_FROM")
    routed = await sink.handle(detect(index, registry, burst(2)))
    assert telnyx.calls == [] and "notify failed" in routed["data"]["reason"]


async def test_burst_rings_once_and_a_throttled_ring_is_neither_sent_nor_failed(tmp_path, index, runner, telnyx, clock, capsys):
    sink, registry = build(tmp_path, runner, telnyx, clock)
    first = await sink.handle(detect(index, registry, burst(1)))
    assert f"ring:{RING_TO}" in first["data"]["notify_channels"]
    for n in (2, 3, 4):
        clock.now += timedelta(seconds=40)
        routed = await sink.handle(detect(index, registry, burst(n)))
        assert routed["data"]["notify_channels"] == ["telegram:1000"]  # telegram still goes out for every text
        assert routed["data"]["reason"] is None  # not a failure
    assert len(telnyx.calls) == 1 and len(runner.of("send")) == 4
    assert capsys.readouterr().err.count('"ring_throttled"') == 3

    clock.now += timedelta(minutes=10)  # 10 minutes after the first ring
    routed = await sink.handle(detect(index, registry, burst(5)))
    assert f"ring:{RING_TO}" in routed["data"]["notify_channels"] and len(telnyx.calls) == 2


async def test_cooldown_is_configurable_per_target(tmp_path, index, runner, telnyx, clock):
    sink, registry = build(tmp_path, runner, telnyx, clock, extra="ring_cooldown_minutes = 3")
    await sink.handle(detect(index, registry, burst(1)))
    clock.now += timedelta(minutes=2, seconds=59)
    await sink.handle(detect(index, registry, burst(2)))
    assert len(telnyx.calls) == 1
    clock.now += timedelta(seconds=2)
    await sink.handle(detect(index, registry, burst(3)))
    assert len(telnyx.calls) == 2


async def test_last_ring_survives_a_restart(tmp_path, index, runner, telnyx, clock):
    sink, registry = build(tmp_path, runner, telnyx, clock)
    await sink.handle(detect(index, registry, burst(1)))
    assert len(telnyx.calls) == 1

    clock.now += timedelta(minutes=2)
    # A new process: new SinkService, new State on the same file, key not cached.
    reborn = SinkService(registry, State(tmp_path / "state" / "state.db"), _Publisher(), runner=runner, clock=clock, http=telnyx, op=OP)
    routed = await reborn.handle(detect(index, registry, burst(2)))
    assert len(telnyx.calls) == 1 and routed["data"]["notify_channels"] == ["telegram:1000"]
    assert len(runner.of("read")) == 1  # throttled before the key is even asked for


async def test_throttle_is_per_watch_and_channel(tmp_path, index, runner, telnyx, clock):
    sink, registry = build(tmp_path, runner, telnyx, clock, notify=[f"ring:{RING_TO}", "ring:+12155550143"])
    await sink.handle(detect(index, registry, burst(1)))
    assert [c["body"]["to"] for c in telnyx.calls] == [RING_TO, "+12155550143"]


@pytest.mark.parametrize("at_utc,rings", [
    ((8, 0), False),    # 03:00 New York, inside 22:00-07:00 (wraps midnight)
    ((3, 30), False),   # 22:30 the evening before
    ((12, 0), True),    # 07:00 New York: the end is exclusive
    ((11, 59), False),  # 06:59
    ((21, 59), True),   # 16:59
    ((3, 0), False),    # 22:00 the evening before: the start is inclusive
])
async def test_quiet_hours_wrap_midnight(tmp_path, index, runner, telnyx, at_utc, rings):
    # 2026-11-04 15:30 UTC is 10:30 EST; pick the instant by UTC hour/minute on 2026-11-04 (EST = UTC-5).
    clock = Clock(datetime(2026, 11, 4, *at_utc, tzinfo=UTC))
    sink, registry = build(tmp_path, runner, telnyx, clock, extra='ring_quiet_hours = "22:00-07:00"')
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert bool(telnyx.calls) is rings
    assert "telegram:1000" in routed["data"]["notify_channels"]  # telegram always goes out
    assert routed["data"]["reason"] is None


async def test_quiet_hours_same_day_window_and_log(tmp_path, index, runner, telnyx, clock, capsys):
    sink, registry = build(tmp_path, runner, telnyx, clock, extra='ring_quiet_hours = "09:00-11:00"')  # clock is 10:30 NY
    await sink.handle(detect(index, registry, burst(1)))
    assert telnyx.calls == [] and runner.of("read") == []
    assert capsys.readouterr().err.count('"ring_quiet"') == 1
    clock.now = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)  # 11:00 NY, end exclusive
    await sink.handle(detect(index, registry, burst(2)))
    assert len(telnyx.calls) == 1


async def test_quiet_hours_follow_new_york_across_dst(tmp_path, index, runner, telnyx):
    # 2026-07-01 02:30 UTC is 22:30 EDT the day before, inside 22:00-07:00; in EST that instant would be 21:30.
    clock = Clock(datetime(2026, 7, 1, 2, 30, tzinfo=UTC))
    sink, registry = build(tmp_path, runner, telnyx, clock, extra='ring_quiet_hours = "22:00-07:00"')
    await sink.handle(detect(index, registry, burst(1)))
    assert telnyx.calls == []


async def test_dry_run_prints_the_planned_call_and_places_nothing(tmp_path, index, runner, telnyx, clock, capsys):
    sink, registry = build(tmp_path, runner, telnyx, clock)
    state_path = tmp_path / "dry" / "state.db"
    dry = SinkService(registry, State(state_path, read_only=True), DryRunPublisher(), runner=runner, clock=clock,
                      dry_run=True, http=telnyx, op=OP)
    await dry.handle(detect(index, registry, burst(1)))
    assert telnyx.calls == [] and runner.calls == [] and not state_path.exists()
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    telegram, ring = printed["notify"]
    assert telegram["to"] == "telegram:1000" and "text" in telegram
    assert ring == {"to": f"ring:{RING_TO}", "ring": {
        "to": RING_TO, "from": RING_FROM, "connection_id": CONNECTION_ID,
        "timeout_secs": 20, "time_limit_secs": 40, "skipped": None}}


async def test_dry_run_reports_a_throttle_it_can_read(tmp_path, index, runner, telnyx, clock, capsys):
    sink, registry = build(tmp_path, runner, telnyx, clock)
    await sink.handle(detect(index, registry, burst(1)))
    dry = SinkService(registry, State(tmp_path / "state" / "state.db", read_only=True), DryRunPublisher(), runner=runner,
                      clock=clock, dry_run=True, http=telnyx, op=OP)
    capsys.readouterr()
    await dry.handle(detect(index, registry, burst(2)))
    ring = json.loads(capsys.readouterr().out.strip().splitlines()[0])["notify"][1]["ring"]
    assert ring["skipped"] == "throttled" and len(telnyx.calls) == 1


async def test_skipped_duplicate_never_rings(tmp_path, index, runner, telnyx, clock):
    sink, registry = build(tmp_path, runner, telnyx, clock)
    await sink.handle(detect(index, registry, rcs(text="On my way")))
    clock.now += timedelta(minutes=30)  # past the cooldown: only the duplicate rule can stop a second ring
    sms = rcs(message_id="sms-1", text="on my  way", event_id="0b9c6a62-1111-4d0e-8f5f-0000000009f1")
    sms["type"] = "bloodbank.messaging.sms.received"
    routed = await sink.handle(detect(index, registry, sms))
    assert routed["data"]["action"] == "skipped_duplicate" and routed["data"]["notify_channels"] == []
    assert len(telnyx.calls) == 1 and len(runner.of("send")) == 1


async def test_a_failed_route_still_rings(tmp_path, index, runner, telnyx, clock, plane_repo):
    """A failed ticket still means the person wrote."""
    write_target(tmp_path / "p", "jane", f"""
[target]
id = "jane-doe"
display_name = "Jane Doe"
[target.handles]
e164 = ["+12155550100"]
[sink]
kind = "plane"
repo = "{plane_repo}"
notify = ["ring:{RING_TO}"]
""")
    runner.create_output, runner.create_code = "boom\n", 1
    registry = TargetRegistry([str(tmp_path / "p" / "*.toml")])
    sink = SinkService(registry, State(tmp_path / "s.db"), _Publisher(), runner=runner, clock=clock, http=telnyx, op=OP)
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert routed["data"]["action"] == "failed" and len(telnyx.calls) == 1


async def test_the_key_never_reaches_logs_argv_state_or_the_routed_event(tmp_path, index, runner, telnyx, clock, capsys):
    sink, registry = build(tmp_path, runner, telnyx, clock)
    telnyx.status, telnyx.body = 403, b'{"errors": [{"title": "Forbidden"}]}'
    routed_failed = await sink.handle(detect(index, registry, burst(1)))
    telnyx.status, telnyx.body = 200, FakeTelnyx().body
    clock.now += timedelta(minutes=1)
    routed_ok = await sink.handle(detect(index, registry, burst(2)))
    out = capsys.readouterr()
    secret = runner.op_key
    assert secret not in out.err and secret not in out.out
    assert secret not in json.dumps([routed_failed, routed_ok])
    assert all(secret not in " ".join(argv) for argv, _ in runner.calls)
    assert secret.encode() not in (tmp_path / "state" / "state.db").read_bytes()
    assert not any(secret in str(v) for rec in sink.publisher.published for v in [json.dumps(rec)])


async def test_redelivery_does_not_ring_twice(tmp_path, index, runner, telnyx, clock):
    sink, registry = build(tmp_path, runner, telnyx, clock)
    detection = detect(index, registry, burst(1))
    await sink.handle(detection)
    clock.now += timedelta(hours=1)
    await sink.handle(detection)
    assert len(telnyx.calls) == 1


# --- target file validation ---------------------------------------------------------


def parse(tmp_path, notify, extra=""):
    path = write_target(tmp_path / "v", "t", TARGET.format(notify=json.dumps(notify), extra=extra))
    targets, errors = load_targets([str(path)])
    return targets, errors


def test_ring_channel_is_normalised_with_the_e164_helper(tmp_path):
    targets, errors = parse(tmp_path, ["telegram:1000", "ring:(215) 555-0142", "ring:+442079460958"])
    assert errors == []
    assert targets[0].sink.notify == ("telegram:1000", "ring:+12155550142", "ring:+442079460958")
    assert targets[0].sink.ring_cooldown_minutes == 10 and targets[0].sink.ring_quiet_hours is None


@pytest.mark.parametrize("channel", ["ring:Jane", "ring:555-0100", "ring:+1", "ring:", "ring:anonymous"])
def test_a_ring_channel_that_is_not_a_number_is_refused_without_echoing_it(tmp_path, channel):
    targets, errors = parse(tmp_path, [channel])
    assert targets == [] and len(errors) == 1
    value = channel.split(":", 1)[1]
    assert "sink.notify" in errors[0] and (not value or value not in errors[0].split("sink.notify", 1)[1])


@pytest.mark.parametrize("extra", [
    "ring_cooldown_minutes = 0", "ring_cooldown_minutes = -5", "ring_cooldown_minutes = 2.5",
    'ring_cooldown_minutes = "10"', "ring_cooldown_minutes = true",
])
def test_cooldown_must_be_a_positive_whole_number(tmp_path, extra):
    targets, errors = parse(tmp_path, ["telegram:1000"], extra)
    assert targets == [] and "ring_cooldown_minutes" in errors[0]


@pytest.mark.parametrize("value", ["25:00-07:00", "22:00-22:00", "22:00", "10pm-7am", "22:60-07:00", "", "7:00-8:00"])
def test_quiet_hours_must_be_a_valid_range(tmp_path, value):
    targets, errors = parse(tmp_path, ["telegram:1000"], f'ring_quiet_hours = "{value}"')
    assert targets == [] and "ring_quiet_hours" in errors[0]


def test_quiet_hours_parse_and_window():
    assert parse_quiet_hours("22:00-07:00") == (1320, 420)
    wrap, day = (1320, 420), parse_quiet_hours("09:00-17:30")
    assert in_quiet_hours(wrap, 1320) and in_quiet_hours(wrap, 0) and in_quiet_hours(wrap, 419)
    assert not in_quiet_hours(wrap, 420) and not in_quiet_hours(wrap, 1319)
    assert in_quiet_hours(day, 540) and not in_quiet_hours(day, 1050) and not in_quiet_hours(day, 539)
    assert not in_quiet_hours(None, 0)
    with pytest.raises(TargetError):
        parse_quiet_hours(7)


def test_cooldown_and_quiet_hours_load(tmp_path):
    targets, errors = parse(tmp_path, [f"ring:{RING_TO}"], 'ring_cooldown_minutes = 15\nring_quiet_hours = "23:30-06:45"')
    assert errors == [] and targets[0].sink.ring_cooldown_minutes == 15 and targets[0].sink.ring_quiet_hours == (1410, 405)


def test_selftest_target_has_no_ring_channel():
    text = (SERVICE_ROOT / "targets" / "selftest.toml").read_text()
    assert "ring" not in text
    targets, errors = load_targets([str(SERVICE_ROOT / "targets" / "*.toml")])
    assert errors == [] and not any(c.startswith("ring:") for t in targets for c in t.sink.notify)


def test_sink_unit_carries_the_ring_environment_and_no_secret():
    text = (SERVICE_ROOT / "systemd" / "target-watch-sink.service").read_text()
    assert "Environment=TARGET_WATCH_TELNYX_CONNECTION_ID=3086124529433446133" in text
    assert "Environment=TARGET_WATCH_RING_FROM=+12154498437" in text
    assert "Environment=TARGET_WATCH_OP=%h/.local/bin/op" in text
    assert "KEY" not in text.replace("TARGET_WATCH_TELNYX_KEY_REF", "")  # no key material, only the name of the ref variable


def test_ring_stays_under_the_carrier_no_answer_forward():
    assert sink_module.RING_TIMEOUT_SECS <= 22 and sink_module.RING_TIME_LIMIT_SECS == 40


def test_mask_channel():
    assert mask_channel(f"ring:{RING_TO}") == "ring:...0142" and mask_channel("telegram:1000") == "telegram:1000"
