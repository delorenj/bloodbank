"""The "ntfy:<topic>" channel, the durable notification log and `target-watch status`.

Fake topics, tokens and numbers only (this repo is public). ntfy, Telnyx and `op` are all faked."""
import json
import sqlite3
from datetime import UTC, datetime
from email.header import decode_header, make_header

import pytest

from conftest import rcs, write_target
from target_watch import cli, sink as sink_module
from target_watch.matcher import detection_envelope, match
from target_watch.sink import SinkService, channel_rank, env_file_value, ntfy_header, url_after
from target_watch.state import State
from target_watch.targets import TargetRegistry, load_targets
from test_ring import (CONNECTION_ID, OP, RING_FROM, RING_TO, TARGET, Clock, FakeTelnyx, _Publisher, burst, clock,  # noqa: F401
                       detect, parse, ring_env)  # the fixtures are re-exported so pytest finds them here
from test_ring_outcome import (CHANNEL as SIP_CHANNEL, FakeEvents, LegTelnyx, PICKED_UP, TIMEOUT, failure_lines, fast,  # noqa: F401
                               events_of)

TOKEN_REF = "op://FakeVault/fake-ntfy-item/credential"
TOKEN = "fake-ntfy-token-DO-NOT-LEAK"
TOPIC = "fake-topic"
NTFY_OK = (200, json.dumps({"id": "msg123", "event": "message", "topic": TOPIC}).encode())


class FakeNtfy:
    """Stands in for the ntfy POST. Never sees a socket."""

    def __init__(self, *answers):
        self.answers = list(answers) or [NTFY_OK]
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "text": body.decode("utf-8"), "timeout": timeout})
        if self.error:
            raise self.error
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@pytest.fixture
def ntfy():
    return FakeNtfy()


@pytest.fixture(autouse=True)
def ntfy_token(monkeypatch, runner):
    monkeypatch.setenv("TARGET_WATCH_NTFY_TOKEN_REF", TOKEN_REF)
    runner.op_values[TOKEN_REF] = TOKEN


def make(tmp_path, runner, clock, ntfy, *, notify=None, extra="", events=None, telnyx=None, name="w"):
    notify = notify or [f"ntfy:{TOPIC}", "telegram:1000"]
    write_target(tmp_path / name, "jane", TARGET.format(notify=json.dumps(notify), extra=extra))
    registry = TargetRegistry([str(tmp_path / name / "*.toml")])
    sink = SinkService(registry, State(tmp_path / "state" / "state.db"), _Publisher(), runner=runner, clock=clock,
                       http=telnyx or LegTelnyx(), op=OP, http_get=events, ntfy_http=ntfy)
    return sink, registry


# --- target file ------------------------------------------------------------------------


def test_an_ntfy_channel_loads(tmp_path):
    targets, errors = parse(tmp_path, ["ntfy:jim-alerts_1", "telegram:1000"])
    assert errors == [] and targets[0].sink.notify == ("ntfy:jim-alerts_1", "telegram:1000")


@pytest.mark.parametrize("channel", ["ntfy:", "ntfy:a b", "ntfy:a/b", "ntfy:a?x=1", "ntfy:a#b", "ntfy:" + "x" * 65, "ntfy:café"])
def test_a_bad_ntfy_topic_is_refused_without_echoing_it(tmp_path, channel):
    targets, errors = parse(tmp_path, [channel])
    assert targets == [] and len(errors) == 1
    topic = channel.split(":", 1)[1]
    assert "sink.notify" in errors[0] and (not topic or topic not in errors[0])


def test_channels_are_sent_ntfy_then_telegram_then_ring():
    mixed = [f"ring:{RING_TO}", "telegram:1000", "ntfy:a", "telegram:2000", "ring:+12155550143", "ntfy:b"]
    assert sorted(mixed, key=channel_rank) == ["ntfy:a", "ntfy:b", "telegram:1000", "telegram:2000", f"ring:{RING_TO}", "ring:+12155550143"]


# --- publishing -------------------------------------------------------------------------


async def test_ntfy_goes_first_and_carries_the_loud_headers_and_the_telegram_line(tmp_path, index, runner, clock, ntfy, capsys):
    sink, registry = make(tmp_path, runner, clock, ntfy, notify=[f"ring:{RING_TO}", "telegram:1000", f"ntfy:{TOPIC}"])
    routed = await sink.handle(detect(index, registry, burst(1)))

    [call] = ntfy.calls
    assert call["url"] == f"https://ntfy.delo.sh/{TOPIC}"
    headers = call["headers"]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Title"] == "Jane Doe - rcs" and headers["Priority"] == "urgent" and headers["Tags"] == "rotating_light"
    assert "Click" not in headers  # a notify_only target files no ticket
    assert headers["User-Agent"] and "urllib" not in headers["User-Agent"].lower()
    assert call["timeout"] == 15
    [telegram] = runner.of("send")
    assert call["text"] == telegram[-1] == "Jane Doe via rcs: message number 1 -> notified_only"
    # ntfy token lookup, then the telegram line, then the ring's own key: ntfy was first
    assert [runner.verb(argv) for argv, _ in runner.calls] == ["read", "send", "read"]
    assert runner.of("read")[0] == [OP, "read", "--no-newline", TOKEN_REF]
    assert routed["data"]["notify_channels"] == [f"ntfy:{TOPIC}", "telegram:1000", f"ring:{RING_TO}"]
    assert routed["data"]["reason"] is None
    out = capsys.readouterr()
    assert TOKEN not in out.err and TOKEN not in json.dumps(routed) and all(TOKEN not in " ".join(a) for a, _ in runner.calls)
    assert TOKEN.encode() not in (tmp_path / "state" / "state.db").read_bytes()


async def test_the_base_url_is_configurable(tmp_path, index, runner, clock, ntfy, monkeypatch):
    monkeypatch.setenv("TARGET_WATCH_NTFY_URL", "https://ntfy.example.test/")
    sink, registry = make(tmp_path, runner, clock, ntfy)
    await sink.handle(detect(index, registry, burst(1)))
    assert ntfy.calls[0]["url"] == f"https://ntfy.example.test/{TOPIC}"


async def test_the_token_is_cached_and_a_401_drops_it(tmp_path, index, runner, clock, ntfy):
    sink, registry = make(tmp_path, runner, clock, ntfy, notify=[f"ntfy:{TOPIC}"])
    await sink.handle(detect(index, registry, burst(1)))
    await sink.handle(detect(index, registry, burst(2)))
    assert len(runner.of("read")) == 1 and len(ntfy.calls) == 2  # no cooldown for ntfy: every text is an urgent push
    ntfy.answers = [(401, b'{"code": 40101, "http": 401, "error": "unauthorized"}')]
    await sink.handle(detect(index, registry, burst(3)))
    runner.op_values[TOKEN_REF] = "fake-rotated-token"
    ntfy.answers = [NTFY_OK]
    await sink.handle(detect(index, registry, burst(4)))
    assert len(runner.of("read")) == 2 and ntfy.calls[-1]["headers"]["Authorization"] == "Bearer fake-rotated-token"


async def test_the_token_reference_falls_back_to_the_infra_alerts_env_file(tmp_path, index, runner, clock, ntfy, monkeypatch):
    monkeypatch.delenv("TARGET_WATCH_NTFY_TOKEN_REF")
    env_file = tmp_path / "infra-alerts-env"
    env_file.write_text(f'# loud channel\nNTFY_URL=https://ntfy.delo.sh/infra\nexport NTFY_TOKEN_REF="{TOKEN_REF}"\n')
    monkeypatch.setenv("TARGET_WATCH_NTFY_ENV_FILE", str(env_file))
    sink, registry = make(tmp_path, runner, clock, ntfy, notify=[f"ntfy:{TOPIC}"])
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert runner.of("read") == [[OP, "read", "--no-newline", TOKEN_REF]]
    assert routed["data"]["notify_channels"] == [f"ntfy:{TOPIC}"] and len(ntfy.calls) == 1
    assert ntfy.calls[0]["url"] == f"https://ntfy.delo.sh/{TOPIC}"  # the env file's NTFY_URL (an infra topic) is not used


def test_env_file_value(tmp_path):
    path = tmp_path / "env"
    path.write_text("A=1\n  export B='two words'\nC=\"3\"\nD=\n#E=5\nNTFY_TOKEN_REF_X=nope\n")
    assert env_file_value(str(path), "A") == "1" and env_file_value(str(path), "B") == "two words"
    assert env_file_value(str(path), "C") == "3" and env_file_value(str(path), "D") is None
    assert env_file_value(str(path), "E") is None and env_file_value(str(path), "NTFY_TOKEN_REF") is None
    assert env_file_value(str(tmp_path / "missing"), "A") is None


@pytest.mark.parametrize("why", ["no ref", "plaintext ref", "op fails"])
async def test_no_usable_token_is_a_failed_channel_without_a_post(tmp_path, index, runner, clock, ntfy, monkeypatch, why):
    if why == "no ref":
        monkeypatch.delenv("TARGET_WATCH_NTFY_TOKEN_REF")
    elif why == "plaintext ref":
        monkeypatch.setenv("TARGET_WATCH_NTFY_TOKEN_REF", "tk_plaintext_not_a_reference")  # never passed to op or logged
    else:
        runner.op_code = 1
    sink, registry = make(tmp_path, runner, clock, ntfy)
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert ntfy.calls == []
    assert routed["data"]["notify_channels"] == ["telegram:1000"]
    assert routed["data"]["reason"] == f"notify failed: ntfy:{TOPIC}"  # and telegram still went out
    assert not any("tk_plaintext" in " ".join(argv) for argv, _ in runner.calls)


@pytest.mark.parametrize("answer,detail", [
    ((200, b'{"event": "open"}'), "HTTP 200: no message id in the answer"),
    ((200, b"not json"), "HTTP 200: no message id in the answer"),
    ((500, b'{"error": "boom"}'), "HTTP 500: boom"),
    ((403, b'{"code": 40301, "http": 403, "error": "forbidden"}'), "HTTP 403: forbidden"),
    ((502, b"<html>bad gateway</html>"), "HTTP 502"),
])
async def test_anything_but_a_200_with_an_id_is_a_failed_channel(tmp_path, index, runner, clock, ntfy, answer, detail):
    ntfy.answers = [answer]
    sink, registry = make(tmp_path, runner, clock, ntfy)
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert routed["data"]["notify_channels"] == ["telegram:1000"] and f"ntfy:{TOPIC}" in routed["data"]["reason"]
    [row] = [n for n in sink.state.recent_routes(5)[0]["notifications"] if n["channel"].startswith("ntfy")]
    assert row["outcome"] == "failed" and row["detail"] == detail


@pytest.mark.parametrize("error", [TimeoutError("t"), ConnectionResetError("r"), RuntimeError("boom")])
async def test_an_ntfy_transport_failure_never_raises(tmp_path, index, runner, clock, ntfy, error):
    ntfy.error = error
    sink, registry = make(tmp_path, runner, clock, ntfy)
    routed = await sink.handle(detect(index, registry, burst(1)))  # a raise would nak the detection into five redeliveries
    assert routed["data"]["notify_channels"] == ["telegram:1000"] and len(runner.of("send")) == 1


async def test_a_slow_ntfy_is_abandoned_at_the_deadline(tmp_path, index, runner, clock, monkeypatch):
    import time

    def slow(url, headers, body, timeout):
        time.sleep(0.5)
        return NTFY_OK

    monkeypatch.setattr(sink_module, "NTFY_DEADLINE_S", 0.05)
    sink, registry = make(tmp_path, runner, clock, slow)
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert routed["data"]["notify_channels"] == ["telegram:1000"]


async def test_click_is_px_own_ticket_link_when_a_ticket_was_filed(tmp_path, index, runner, clock, ntfy, plane_repo):
    write_target(tmp_path / "p", "jane", f"""
[target]
id = "jane-doe"
display_name = "Jane Doe"
[target.handles]
e164 = ["+12155550100"]
[sink]
kind = "plane"
repo = "{plane_repo}"
notify = ["ntfy:{TOPIC}"]
""")
    registry = TargetRegistry([str(tmp_path / "p" / "*.toml")])
    sink = SinkService(registry, State(tmp_path / "s.db"), _Publisher(), runner=runner, clock=clock, op=OP, ntfy_http=ntfy)
    await sink.handle(detect(index, registry, burst(1)))
    assert ntfy.calls[0]["headers"]["Click"] == "https://plane.example/ws/projects/x/issues/abc"
    assert ntfy.calls[0]["text"].endswith("-> ticket_created JIMB-450")
    await sink.handle(detect(index, registry, burst(2)))  # same conversation: a comment, whose output carries its own URL
    assert ntfy.calls[1]["headers"]["Click"] == "https://plane.example/x"
    runner.create_output, runner.create_code = "boom\n", 1  # px confirms nothing: no ticket, so no link
    other = rcs(text="other thread", message_id="r9", thread_id="t-other", event_id="0b9c6a62-1111-4d0e-8f5f-000000000999")
    await sink.handle(detect(index, registry, other))
    assert "Click" not in ntfy.calls[2]["headers"] and ntfy.calls[2]["text"].endswith("-> failed")


def test_url_after_only_trusts_a_bare_url_line_after_the_confirming_line():
    created = sink_module.CREATED
    assert url_after(created, "created JIMB-1 [Todo] t\n  https://p.example/i/1\n") == "https://p.example/i/1"
    assert url_after(created, "https://p.example/before\ncreated JIMB-1 [Todo] t\n") is None
    assert url_after(created, "created JIMB-1 t see https://p.example/inline\n") is None
    assert url_after(created, "created JIMB-1 t\n  http://p.example/not-https\n") is None
    assert url_after(created, "boom\n") is None


def test_non_ascii_titles_travel_as_an_rfc_2047_word_and_ascii_is_untouched():
    assert ntfy_header("Jane  Doe\n- rcs") == "Jane Doe - rcs"
    encoded = ntfy_header("José Doe - rcs")
    assert encoded.isascii() and str(make_header(decode_header(encoded))) == "José Doe - rcs"


async def test_a_ring_that_did_not_get_through_is_also_pushed_to_ntfy(tmp_path, index, runner, clock, ntfy, fast):
    sink, registry = make(tmp_path, runner, clock, ntfy, notify=[f"ntfy:{TOPIC}", "telegram:1000", SIP_CHANNEL], events=FakeEvents(TIMEOUT))
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    assert len(ntfy.calls) == 2
    assert ntfy.calls[1]["headers"]["Title"] == "Jane Doe - ring did not get through"
    assert "nobody answered" in ntfy.calls[1]["text"] and "example-user" not in ntfy.calls[1]["text"]
    assert len(failure_lines(runner)) == 1


async def test_dry_run_prints_the_ntfy_plan_and_sends_nothing(tmp_path, index, runner, clock, ntfy, capsys):
    from target_watch.bus import DryRunPublisher

    sink, registry = make(tmp_path, runner, clock, ntfy)
    dry = SinkService(registry, State(tmp_path / "dry" / "state.db", read_only=True), DryRunPublisher(), runner=runner, clock=clock,
                      dry_run=True, ntfy_http=ntfy, op=OP)
    await dry.handle(detect(index, registry, burst(1)))
    first, second = json.loads(capsys.readouterr().out.strip().splitlines()[0])["notify"]
    assert first == {"to": f"ntfy:{TOPIC}", "text": "Jane Doe via rcs: message number 1 -> notified_only <new>", "title": "Jane Doe - rcs",
                     "priority": "urgent", "url": f"https://ntfy.delo.sh/{TOPIC}"}
    assert second["to"] == "telegram:1000" and ntfy.calls == [] and runner.calls == []


# --- the notification log -----------------------------------------------------------------


def rows_of(sink):
    [route] = sink.state.recent_routes(5)
    return route["notifications"]


async def test_every_channel_attempt_is_logged_with_masked_names(tmp_path, index, runner, clock, ntfy, fast):
    sink, registry = make(tmp_path, runner, clock, ntfy, notify=[f"ntfy:{TOPIC}", "telegram:1000", SIP_CHANNEL],
                          events=FakeEvents(PICKED_UP))
    await sink.handle(detect(index, registry, burst(1)))
    notes = rows_of(sink)
    assert [(n["channel"], n["attempt"], n["outcome"]) for n in notes] == [
        (f"ntfy:{TOPIC}", 1, "sent"), ("telegram:1000", 1, "sent"), ("ring:sip:...er@sip.telnyx.com", 1, "placed")]
    assert notes[2]["ref"] == "11111111-2222-3333-4444-555555555555" and notes[0]["detail"] == "id msg123"
    assert "example-user" not in json.dumps(notes)
    await sink.settle()  # the follower reaches a terminal outcome and updates the ring's row in place
    assert [(n["channel"], n["outcome"]) for n in rows_of(sink)][2:] == [("ring:sip:...er@sip.telnyx.com", "answered")]


@pytest.mark.parametrize("events,outcome,detail", [
    (TIMEOUT, "unanswered", "hangup_cause timeout, SIP 487, by unknown"),
    ([{"name": "call.initiated"}], "unknown", "error no hangup or answer event yet"),
])
async def test_the_ring_row_is_updated_with_the_terminal_outcome(tmp_path, index, runner, clock, ntfy, fast, events, outcome, detail):
    sink, registry = make(tmp_path, runner, clock, ntfy, notify=[SIP_CHANNEL], events=FakeEvents(events))
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    [row] = rows_of(sink)
    assert row["outcome"] == outcome and row["detail"] == detail and row["updated_at"] >= row["at"]


async def test_a_busy_phone_logs_both_attempts(tmp_path, index, runner, clock, ntfy, fast):
    from test_ring_outcome import BUSY

    sink, registry = make(tmp_path, runner, clock, ntfy, notify=[SIP_CHANNEL], events=FakeEvents(BUSY, PICKED_UP))
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    assert [(n["attempt"], n["outcome"]) for n in rows_of(sink)] == [(1, "busy"), (2, "answered")]


async def test_a_refused_dial_logs_why_and_the_channel_limit_retry(tmp_path, index, runner, clock, ntfy, fast):
    from test_ring_outcome import CHANNEL_LIMIT, Dials, OK_DIAL

    sink, registry = make(tmp_path, runner, clock, ntfy, notify=[SIP_CHANNEL], events=FakeEvents(PICKED_UP))
    sink.http = Dials(CHANNEL_LIMIT, OK_DIAL)
    await sink.handle(detect(index, registry, burst(1)))
    await sink.settle()
    first, retry = rows_of(sink)
    assert first["outcome"] == "channel_limit" and "90042" in first["detail"] and "OB profile channel limit exceeded" in first["detail"]
    assert (retry["attempt"], retry["outcome"]) == (2, "answered")


@pytest.mark.parametrize("setup,outcome", [("throttled", "throttled"), ("quiet", "quiet"), ("stale", "stale")])
async def test_skipped_rings_are_logged_too(tmp_path, index, runner, clock, ntfy, monkeypatch, setup, outcome):
    extra = 'ring_quiet_hours = "09:00-11:00"' if setup == "quiet" else ""  # the clock is 10:30 in New York
    if setup == "stale":
        monkeypatch.setattr(sink_module, "RING_MAX_AGE_S", 300)
    sink, registry = make(tmp_path, runner, clock, ntfy, notify=[f"ring:{RING_TO}"], extra=extra)
    if setup == "throttled":
        sink.state.record_ring("jane-doe", f"ring:{RING_TO}", clock().timestamp())
    await sink.handle(detect(index, registry, burst(1)))
    [row] = rows_of(sink)
    assert (row["channel"], row["outcome"]) == ("ring:...0142", outcome)


async def test_a_failed_telegram_and_a_failed_ring_dial_log_their_errors(tmp_path, index, runner, clock, ntfy):
    runner.hermes_code = 1
    telnyx = FakeTelnyx()
    telnyx.status, telnyx.body = 422, b'{"errors": [{"code": "10015", "title": "Bad Request"}]}'
    sink, registry = make(tmp_path, runner, clock, ntfy, notify=["telegram:1000", f"ring:{RING_TO}"], telnyx=telnyx)
    await sink.handle(detect(index, registry, burst(1)))
    telegram, ring = rows_of(sink)
    assert telegram["outcome"] == "failed" and "Platform not configured" in telegram["detail"]
    assert ring["outcome"] == "failed" and ring["detail"] == "HTTP 422: Bad Request"


async def test_a_notification_log_write_error_never_escapes_or_rings_again(tmp_path, index, runner, clock, ntfy, monkeypatch):
    telnyx = FakeTelnyx()
    sink, registry = make(tmp_path, runner, clock, ntfy, notify=["telegram:1000", f"ring:{RING_TO}"], telnyx=telnyx)

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sink.state, "record_notification", broken)
    monkeypatch.setattr(sink.state, "update_notification", broken)
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert routed["data"]["notify_channels"] == ["telegram:1000", f"ring:{RING_TO}"]
    await sink.handle(detect(index, registry, burst(1)))  # redelivery: no second ring
    assert len(telnyx.calls) == 1


async def test_a_duplicate_or_redelivered_detection_adds_no_rows(tmp_path, index, runner, clock, ntfy):
    sink, registry = make(tmp_path, runner, clock, ntfy)
    detection = detect(index, registry, burst(1))
    await sink.handle(detection)
    await sink.handle(detection)
    assert len(rows_of(sink)) == 2 and len(ntfy.calls) == 1


# --- additive migration ----------------------------------------------------------------------

LEGACY = """
CREATE TABLE routes (detection_id TEXT PRIMARY KEY, watch_id TEXT NOT NULL, conv_key TEXT NOT NULL, text_hash TEXT,
  received_epoch REAL NOT NULL, px_kind TEXT, px_state TEXT NOT NULL DEFAULT 'none', action TEXT, ticket_ref TEXT, reason TEXT,
  notified INTEGER NOT NULL DEFAULT 0, notify_channels TEXT NOT NULL DEFAULT '[]', envelope TEXT, published INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE rings (watch_id TEXT NOT NULL, channel TEXT NOT NULL, last_epoch REAL NOT NULL, PRIMARY KEY (watch_id, channel));
INSERT INTO routes VALUES ('d-old', 'jane-doe', 'rcs:thread:t1', NULL, 1793804400.0, 'ticket_created', 'done', 'ticket_created',
  'JIMB-9', NULL, 1, '["telegram:1000", "ring:+12155550142"]', NULL, 1, 1793804400.0, 1793804400.0);
INSERT INTO rings VALUES ('jane-doe', 'ring:+12155550142', 1793804400.0);
"""


def legacy_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(LEGACY)
    db.commit()
    db.close()


def test_the_live_state_file_gains_the_table_and_keeps_its_rows(tmp_path):
    path = tmp_path / "state" / "state.db"
    legacy_db(path)
    state = State(path)
    assert state.route("d-old")["ticket_ref"] == "JIMB-9" and state.last_ring("jane-doe", "ring:+12155550142") == 1793804400.0
    row = state.record_notification("d-old", "jane-doe", "telegram:1000", "sent")
    state.update_notification(row, "failed", "late")
    assert state.recent_routes(5)[0]["notifications"][0]["outcome"] == "failed"
    State(path)  # opening again is a no-op (CREATE TABLE IF NOT EXISTS)


def test_a_read_only_view_of_a_state_file_without_the_table_reads_as_no_rows(tmp_path):
    path = tmp_path / "state" / "state.db"
    legacy_db(path)
    [route] = State(path, read_only=True).recent_routes(5)
    assert route["detection_id"] == "d-old" and route["notifications"] == []


# --- target-watch status ------------------------------------------------------------------------


def seed(tmp_path, state_dir_name="state"):
    state = State(tmp_path / state_dir_name / "state.db")
    t0 = datetime(2026, 11, 4, 15, 0, tzinfo=UTC).timestamp()  # 10:00 in New York (EST)
    for n, (watch, conv, ticket, action) in enumerate([
        ("jane-doe", "rcs:thread:t1", "JIMB-451", "ticket_created"),
        ("jane-doe", "email:thread:t2", "JIMB-452", "ticket_commented"),
        ("other-person", "sms:sender:+12155550177", None, "notified_only"),
    ]):
        state.open_route(f"d{n}", watch, conv, None, t0 + n * 3600)
        state.update_route(f"d{n}", action=action, ticket_ref=ticket, notified=1,
                           notify_channels=["telegram:1000"], reason="notify failed: ring:...0142" if n == 1 else None)
    state.record_notification("d1", "jane-doe", f"ntfy:{TOPIC}", "sent", "id msg123", at=t0 + 3600 + 1)
    state.record_notification("d1", "jane-doe", "telegram:1000", "failed", "hermes exit 1: Platform not configured", at=t0 + 3600 + 2)
    row = state.record_notification("d1", "jane-doe", "ring:sip:...er@sip.telnyx.com", "placed", "accepted by Telnyx", ref="leg-1",
                                    at=t0 + 3600 + 3)
    state.update_notification(row, "unanswered", "hangup_cause timeout, SIP 487")
    state.record_notification("d1", "jane-doe", "ring:sip:...er@sip.telnyx.com", "channel_limit", "HTTP 422 code 90042: x", attempt=2,
                              at=t0 + 3600 + 4)
    return state


def status(tmp_path, capsys, *argv):
    assert cli.main(["--state-dir", str(tmp_path / "state"), "status", *argv]) == 0
    return capsys.readouterr().out


def test_status_lists_the_newest_detections_first_with_each_channel(tmp_path, capsys):
    seed(tmp_path)
    out = status(tmp_path, capsys)
    lines = out.splitlines()
    assert lines[0] == "last 3 detection(s), newest first"
    assert lines[1].startswith("2026-11-04 12:00 EST  other-person  sms")  # newest: 17:00Z = 12:00 EST
    assert "notified_only" in lines[1] and "(no per-channel log" in lines[2] and "ring" not in lines[2].split("sent on:")[1]
    second = out.index("2026-11-04 11:00 EST")
    block = out[second:out.index("2026-11-04 10:00 EST")]
    assert "jane-doe  email" in block and "JIMB-452" in block and "ticket_commented" in block
    assert "reason: notify failed: ring:...0142" in block
    assert f"ntfy:{TOPIC}" in block and "sent  id msg123" in block
    assert "telegram:1000" in block and "failed  hermes exit 1: Platform not configured" in block
    assert "ring:sip:...er@sip.telnyx.com" in block and "unanswered  hangup_cause timeout, SIP 487" in block
    assert "(retry 1)" in block and "channel_limit  HTTP 422 code 90042: x" in block
    assert "JIMB-451" in out and out.index("JIMB-452") < out.index("JIMB-451")  # newest first


def test_status_limit_and_target_filter(tmp_path, capsys):
    seed(tmp_path)
    one = status(tmp_path, capsys, "--limit", "1")
    assert one.startswith("last 1 detection(s)") and "other-person" in one and "JIMB-45" not in one
    jane = status(tmp_path, capsys, "--target", "jane-doe")
    assert "other-person" not in jane and "JIMB-451" in jane and "target jane-doe" in jane.splitlines()[0]
    assert "no detections" in status(tmp_path, capsys, "--target", "nobody")


def test_status_masks_a_legacy_rows_ring_number(tmp_path, capsys):
    legacy_db(tmp_path / "state" / "state.db")
    out = status(tmp_path, capsys)
    assert "ring:...0142" in out and "+12155550142" not in out and "telegram:1000" in out and "JIMB-9" in out
    assert "2026-11-04 10:00 EST" in out


def test_status_on_a_missing_state_dir_is_an_empty_report(tmp_path, capsys):
    assert cli.main(["--state-dir", str(tmp_path / "nothing-here"), "status"]) == 0
    assert "no detections" in capsys.readouterr().out
    assert not (tmp_path / "nothing-here").exists()  # a read-only view creates nothing


def test_status_reads_the_state_dir_from_the_environment(tmp_path, capsys, monkeypatch):
    seed(tmp_path)
    monkeypatch.setenv("TARGET_WATCH_STATE_DIR", str(tmp_path / "state"))
    assert cli.main(["status", "--limit", "1"]) == 0
    assert "other-person" in capsys.readouterr().out


@pytest.mark.parametrize("limit", ["0", "-3", "abc"])
def test_status_refuses_a_bad_limit(tmp_path, limit):
    with pytest.raises(SystemExit):
        cli.main(["--state-dir", str(tmp_path), "status", "--limit", limit])
