"""The Create Ticket button (ticket = "button", the signed tap, the HTTP listener) and the "earshot:<channel>" channel.

Fake topics, tokens, secrets and numbers only (this repo is public). ntfy, Telnyx, px, `op` and the earshot CLI are
faked; the HMAC is real, and the listener tests use real sockets on 127.0.0.1.
"""
import asyncio
import hashlib
import hmac
import json
import socket
import sqlite3
import urllib.parse
from datetime import timedelta
from io import StringIO

import pytest

from conftest import FakeRunner, write_target
from target_watch import button as btn
from target_watch import sink as sink_module
from target_watch.bus import DryRunPublisher
from target_watch.sink import SinkService, channel_rank, spoken_line, trim_words
from target_watch.state import State
from target_watch.status import render
from target_watch.targets import TargetRegistry, load_targets
from test_ntfy_status import NTFY_OK, TOKEN, TOKEN_REF, TOPIC, FakeNtfy
from test_ring import (CONNECTION_ID, OP, RING_FROM, RING_TO, TARGET, Clock, FakeTelnyx, _Publisher, burst, clock,  # noqa: F401
                       detect, ring_env)  # the fixtures are re-exported so pytest finds them here

SECRET = "fake-button-secret-DO-NOT-LEAK-" * 3  # 93 characters, as long as a real item needs to be (32+)
SECRET_REF = "op://FakeVault/fake-button-item/password"
EARSHOT = "/fake/bin/earshot"
PX_URL = "https://plane.example/ws/projects/x/issues/abc"  # what FakeRunner's px prints after a create

PLANE_TARGET = """
[target]
id = "jane-doe"
display_name = "Jane Doe"
[target.handles]
e164 = ["+12155550100"]
[sink]
kind = "plane"
repo = "{repo}"
state = "Backlog"
labels = ["submitted-by-client"]
coalesce_hours = 24
notify = {notify}
{extra}
"""


@pytest.fixture(autouse=True)
def button_env(monkeypatch, runner):
    monkeypatch.setenv("TARGET_WATCH_NTFY_TOKEN_REF", TOKEN_REF)
    runner.op_values[TOKEN_REF] = TOKEN
    monkeypatch.setenv("TARGET_WATCH_BUTTON_SECRET_REF", SECRET_REF)
    runner.op_values[SECRET_REF] = SECRET
    monkeypatch.delenv("TARGET_WATCH_BUTTON_URL", raising=False)
    monkeypatch.setenv("TARGET_WATCH_EARSHOT", EARSHOT)


@pytest.fixture
def ntfy():
    return FakeNtfy()


def make(tmp_path, runner, clock, ntfy, repo, *, notify=None, extra="", ticket="button", state_name="state"):
    notify = notify or [f"ntfy:{TOPIC}"]
    body = PLANE_TARGET.format(repo=repo, notify=json.dumps(notify), extra=f'ticket = "{ticket}"\n{extra}')
    write_target(tmp_path / "w", "jane", body)
    registry = TargetRegistry([str(tmp_path / "w" / "*.toml")])
    sink = SinkService(registry, State(tmp_path / state_name / "state.db"), _Publisher(), runner=runner, clock=clock,
                       http=FakeTelnyx(), op=OP, ntfy_http=ntfy)
    return sink, registry


def make_notify_only(tmp_path, runner, clock, ntfy, *, notify, extra=""):
    write_target(tmp_path / "n", "jane", TARGET.format(notify=json.dumps(notify), extra=extra))
    registry = TargetRegistry([str(tmp_path / "n" / "*.toml")])
    sink = SinkService(registry, State(tmp_path / "nstate" / "state.db"), _Publisher(), runner=runner, clock=clock,
                       http=FakeTelnyx(), op=OP, ntfy_http=ntfy)
    return sink, registry


def pushes_with_button(ntfy):
    return [json.loads(c["headers"]["Actions"]) for c in ntfy.calls if "Actions" in c["headers"]]


def button_urls(ntfy):
    urls = []
    for actions in pushes_with_button(ntfy):
        [action] = actions
        urls.append(action["url"])
    return urls


def split(url):
    parts = urllib.parse.urlsplit(url)
    return parts.path, parts.query


async def tap(sink, url, method="POST"):
    path, query = split(url)
    return await sink.button_request(method, path, query)


def sign(detection_id, exp):
    return hmac.new(SECRET.encode(), f"{detection_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def rows_of(sink):
    [route] = sink.state.recent_routes(5)
    return route["notifications"]


async def pushed(tmp_path, index, runner, clock, ntfy, plane_repo, *events, **kwargs):
    """A button-mode sink that has handled `events`: (sink, registry, detections)."""
    sink, registry = make(tmp_path, runner, clock, ntfy, plane_repo, **kwargs)
    detections = []
    for event in events or (burst(1),):
        det = detect(index, registry, event)
        await sink.handle(det)
        detections.append(det)
    return sink, registry, detections


# --- the target file ------------------------------------------------------------------------


def load(tmp_path, repo, *, kind="plane", notify, ticket=None):
    lines = [f'kind = "{kind}"']
    if kind == "plane":
        lines.append(f'repo = "{repo}"')
    lines.append(f"notify = {json.dumps(notify)}")
    if ticket is not None:
        lines.append(f"ticket = {json.dumps(ticket)}")
    body = '[target]\nid = "jane-doe"\ndisplay_name = "Jane Doe"\n[target.handles]\ne164 = ["+12155550100"]\n[sink]\n' + "\n".join(lines)
    return load_targets([str(write_target(tmp_path / "v", "t", body))])


def test_ticket_defaults_to_auto_and_button_loads(tmp_path, plane_repo):
    targets, errors = load(tmp_path, plane_repo, notify=[f"ntfy:{TOPIC}"])
    assert errors == [] and targets[0].sink.ticket == "auto"
    targets, errors = load(tmp_path, plane_repo, notify=[f"ntfy:{TOPIC}", "earshot:alarm"], ticket="button")
    assert errors == [] and targets[0].sink.ticket == "button" and targets[0].sink.notify == (f"ntfy:{TOPIC}", "earshot:alarm")


def test_button_mode_needs_a_ticket_sink_and_an_ntfy_push_to_carry_it(tmp_path, plane_repo):
    _, errors = load(tmp_path, plane_repo, kind="notify_only", notify=[f"ntfy:{TOPIC}"], ticket="button")
    assert len(errors) == 1 and 'kind = "plane"' in errors[0]
    _, errors = load(tmp_path, plane_repo, notify=["telegram:1000", "earshot:alarm"], ticket="button")
    assert len(errors) == 1 and "ntfy:<topic>" in errors[0]


@pytest.mark.parametrize("ticket", ["manual", "", "Button", True, 1])
def test_an_unknown_ticket_mode_is_refused(tmp_path, plane_repo, ticket):
    targets, errors = load(tmp_path, plane_repo, notify=[f"ntfy:{TOPIC}"], ticket=ticket)
    assert targets == [] and len(errors) == 1 and "sink.ticket" in errors[0]


def test_an_earshot_channel_loads(tmp_path, plane_repo):
    targets, errors = load(tmp_path, plane_repo, kind="notify_only", notify=["earshot:alarm", "earshot:house_2-b"])
    assert errors == [] and targets[0].sink.notify == ("earshot:alarm", "earshot:house_2-b")


@pytest.mark.parametrize("channel", ["earshot:", "earshot:Alarm", "earshot:a b", "earshot:-x", "earshot:a/b", "earshot:" + "x" * 33, "earshot:café"])
def test_a_bad_earshot_channel_is_refused_without_echoing_it(tmp_path, plane_repo, channel):
    targets, errors = load(tmp_path, plane_repo, kind="notify_only", notify=[channel])
    assert targets == [] and len(errors) == 1 and "earshot:<channel>" in errors[0]
    name = channel.split(":", 1)[1]
    assert not name or name not in errors[0]


def test_channels_are_sent_ntfy_earshot_telegram_ring():
    mixed = [f"ring:{RING_TO}", "telegram:1000", "earshot:alarm", "ntfy:a", "telegram:2000", "earshot:other"]
    assert sorted(mixed, key=channel_rank) == ["ntfy:a", "earshot:alarm", "earshot:other", "telegram:1000", "telegram:2000", f"ring:{RING_TO}"]


# --- the signed link ------------------------------------------------------------------------


def test_the_signature_is_hmac_sha256_over_id_and_expiry():
    secret = b"k" * 40
    expected = hmac.new(secret, b"det-1:1800000000", hashlib.sha256).hexdigest()
    assert btn.signature(secret, "det-1", 1800000000) == expected
    assert btn.signed_url("https://agents.example/watch/ticket/", secret, "det-1", 1800000000) == \
        f"https://agents.example/watch/ticket/det-1?exp=1800000000&sig={expected}"


def test_check_accepts_the_genuine_link_and_refuses_everything_else():
    secret = b"k" * 40
    sig = btn.signature(secret, "det-1", 2000)
    assert btn.check(secret, "det-1", "2000", sig, now=1000) is None
    assert btn.check(secret, "det-1", "2000", sig, now=2001) == "expired"
    assert btn.check(secret, "det-1", "2000", sig, now=2000) is None  # expires after, not at, its second
    assert btn.check(secret, "det-2", "2000", sig, now=1000) == "bad signature"
    assert btn.check(secret, "det-1", "2001", sig, now=1000) == "bad signature"
    assert btn.check(b"j" * 40, "det-1", "2000", sig, now=1000) == "bad signature"
    for exp, bad in (("", sig), ("x", sig), ("2000", ""), ("2000", "00"), ("2000", sig[:-1]), ("2000", sig + "0"), ("-1", sig)):
        assert btn.check(secret, "det-1", exp, bad, now=1000) == "bad signature"
    assert btn.check(secret, "det-1", "2000", "z" * 64, now=1000) == "bad signature"
    assert btn.check(secret, "det-1", "2000", sig.upper(), now=1000) is None  # hex is case-insensitive


def test_the_signature_is_compared_in_constant_time(monkeypatch):
    seen = []
    real = hmac.compare_digest
    monkeypatch.setattr(btn.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))
    btn.check(b"k" * 40, "det-1", "2000", "0" * 64, now=1000)
    assert len(seen) == 1


def test_the_ntfy_action_is_one_post_button_that_clears():
    [action] = json.loads(btn.ntfy_action("https://agents.example/watch/ticket/d?exp=1&sig=ab"))
    assert action == {"action": "http", "label": "Create Ticket", "url": "https://agents.example/watch/ticket/d?exp=1&sig=ab",
                      "method": "POST", "clear": True}


@pytest.mark.parametrize("value,expected", [("172.17.0.1:18796", ("172.17.0.1", 18796)), ("[::1]:80", ("::1", 80)), (" 127.0.0.1:1 ", ("127.0.0.1", 1))])
def test_listen_addresses_parse(value, expected):
    assert btn.parse_listen(value) == expected


@pytest.mark.parametrize("value", ["", "x", ":80", "1.2.3.4:0", "1.2.3.4:99999", "1.2.3.4:abc", "1.2.3.4"])
def test_a_bad_listen_address_is_refused(value):
    with pytest.raises(ValueError):
        btn.parse_listen(value)


# --- button mode: the push -------------------------------------------------------------------


async def test_button_mode_files_nothing_and_the_push_carries_a_button_that_verifies(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo,
                                         **{"notify": [f"ntfy:{TOPIC}", "telegram:1000"]})
    assert runner.of("task") == [] and runner.of("comment") == []  # px never ran
    [route] = sink.state.recent_routes(5)
    assert route["action"] == "awaiting_button" and route["px_state"] == "none" and route["ticket_ref"] is None
    [published] = sink.publisher.published
    assert published["data"]["action"] == "awaiting_button" and published["data"]["ticket_ref"] is None
    assert published["data"]["notify_channels"] == [f"ntfy:{TOPIC}", "telegram:1000"] and published["data"]["reason"] is None

    [call] = ntfy.calls
    headers = call["headers"]
    assert headers["Priority"] == "urgent" and "Click" not in headers
    [action] = json.loads(headers["Actions"])
    assert (action["action"], action["label"], action["method"], action["clear"]) == ("http", "Create Ticket", "POST", True)
    parts = urllib.parse.urlsplit(action["url"])
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"https://agents.delo.sh/watch/ticket/{det['id']}"
    query = urllib.parse.parse_qs(parts.query)
    exp = int(query["exp"][0])
    assert exp == int(clock.now.timestamp()) + 7 * 86400  # seven days
    assert query["sig"] == [sign(det["id"], exp)]  # a real HMAC over detection id + expiry
    assert "no ticket yet, tap Create Ticket" in call["text"]
    [telegram] = runner.of("send")
    assert telegram[-1] == call["text"]  # the quiet channel says the same, without a button
    stored = sink.state.button(det["id"])
    assert stored["state"] == "awaiting" and stored["channels"] == [f"ntfy:{TOPIC}"] and stored["detection"] == det
    notes = {n["channel"]: n for n in rows_of(sink)}
    assert notes[f"ntfy:{TOPIC}"]["outcome"] == "sent" and "Create Ticket button" in notes[f"ntfy:{TOPIC}"]["detail"]


async def test_the_secret_never_reaches_a_file_argv_or_the_log(tmp_path, index, runner, clock, ntfy, plane_repo, capsys):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    await tap(sink, button_urls(ntfy)[0])
    await sink.settle()
    out = capsys.readouterr()
    assert SECRET not in out.err and SECRET not in out.out
    assert all(SECRET not in " ".join(argv) for argv, _ in runner.calls)
    assert [argv for argv in runner.of("read") if SECRET_REF in argv] == [[OP, "read", "--no-newline", SECRET_REF]]
    for suffix in ("", "-wal"):
        path = tmp_path / "state" / f"state.db{suffix}"
        assert not path.exists() or SECRET.encode() not in path.read_bytes()
    assert SECRET not in json.dumps(sink.publisher.published) and SECRET not in json.dumps(det)
    assert len(runner.of("read")) == 2  # the ntfy token and the button secret, each read once and cached


async def test_auto_mode_is_unchanged_it_files_at_once_and_clicks_through_with_no_button(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo, ticket="auto")
    [task] = runner.of("task")
    assert task[:3] == ["px", "task", "create"]
    [call] = ntfy.calls
    assert call["headers"]["Click"] == PX_URL and "Actions" not in call["headers"]
    assert sink.publisher.published[0]["data"]["action"] == "ticket_created"
    assert sink.state.button(det["id"]) is None
    assert runner.of("read") == [[OP, "read", "--no-newline", TOKEN_REF]]  # the button secret was never asked for


async def test_without_a_readable_secret_the_push_still_goes_out_and_says_nothing_was_filed(tmp_path, index, runner, clock, ntfy, plane_repo, monkeypatch):
    monkeypatch.delenv("TARGET_WATCH_BUTTON_SECRET_REF")
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    [call] = ntfy.calls
    assert "Actions" not in call["headers"] and "the Create Ticket button is unavailable" in call["text"]
    [published] = sink.publisher.published
    assert published["data"]["action"] == "awaiting_button"
    assert "button unavailable" in published["data"]["reason"] and "nothing was filed" in published["data"]["reason"]
    assert sink.state.button(det["id"]) is None and runner.of("task") == []


async def test_a_secret_that_is_too_short_is_not_used(tmp_path, index, runner, clock, ntfy, plane_repo):
    runner.op_values[SECRET_REF] = "short"
    sink, registry, _ = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    assert "Actions" not in ntfy.calls[0]["headers"]


# --- the tap ----------------------------------------------------------------------------------


async def test_a_tap_files_exactly_what_auto_mode_would_have_and_says_so_on_ntfy(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    # the same detection through an auto-mode sink (its own state): the px command must be identical
    auto_runner = FakeRunner()
    auto_runner.op_values[TOKEN_REF] = TOKEN
    auto, auto_registry = make(tmp_path, auto_runner, clock, FakeNtfy(), plane_repo, ticket="auto", state_name="auto-state")
    await auto.handle(det)

    status, body = await tap(sink, button_urls(ntfy)[0])
    assert (status, body) == (200, {"ticket_ref": "JIMB-450"})
    assert runner.of("task") == auto_runner.of("task") and len(runner.of("task")) == 1
    [(argv, cwd)] = [(a, c) for a, c in runner.calls if runner.verb(a) == "task"]
    assert cwd == str(plane_repo) and "Jane Doe via rcs" in argv[3] and det["id"] in argv[5]
    stored = sink.state.button(det["id"])
    assert (stored["state"], stored["action"], stored["ticket_ref"], stored["ticket_url"]) == ("done", "ticket_created", "JIMB-450", PX_URL)
    assert sink.state.coalesce_ticket("jane-doe", "rcs:thread:thread-9", 0) == "JIMB-450"

    await sink.settle()
    follow_up = ntfy.calls[-1]
    assert follow_up["text"] == "JIMB-450 filed" and follow_up["url"] == f"https://ntfy.delo.sh/{TOPIC}"
    assert follow_up["headers"]["Priority"] == "low" and follow_up["headers"]["Click"] == PX_URL
    assert "Actions" not in follow_up["headers"] and len(ntfy.calls) == 2
    notes = {n["channel"]: n for n in rows_of(sink)}
    assert notes["button"]["outcome"] == "ticket_created" and notes["button"]["ref"] == "JIMB-450"
    assert notes[f"ntfy:{TOPIC} (button follow-up)"]["outcome"] == "sent"


async def test_a_second_tap_returns_the_same_ticket_and_files_nothing(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    url = button_urls(ntfy)[0]
    first = await tap(sink, url)
    second = await tap(sink, url)
    third = await tap(sink, url)
    await sink.settle()
    assert first == second == third == (200, {"ticket_ref": "JIMB-450"})
    assert len(runner.of("task")) == 1 and runner.of("comment") == []
    assert len(ntfy.calls) == 2  # the push and ONE follow-up
    assert [n["outcome"] for n in rows_of(sink) if n["channel"] == "button"] == ["ticket_created", "already_filed", "already_filed"]


async def test_a_tap_comments_on_the_conversations_open_ticket(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, dets = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo, burst(1), burst(2))
    first, second = button_urls(ntfy)
    assert await tap(sink, first) == (200, {"ticket_ref": "JIMB-450"})
    assert await tap(sink, second) == (200, {"ticket_ref": "JIMB-450"})
    await sink.settle()
    assert len(runner.of("task")) == 1
    [comment] = runner.of("comment")
    assert comment[2] == "JIMB-450" and "message number 2" in comment[4] and dets[1]["id"] in comment[4]
    assert [c["text"] for c in ntfy.calls[2:]] == ["JIMB-450 filed", "JIMB-450 filed (as a comment on the open ticket)"]
    assert sink.state.button(dets[1]["id"])["action"] == "ticket_commented"


async def test_two_simultaneous_taps_of_one_conversation_file_one_ticket_not_two(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, dets = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo, burst(1), burst(2))
    first, second = button_urls(ntfy)
    results = await asyncio.gather(tap(sink, first), tap(sink, second))
    assert results == [(200, {"ticket_ref": "JIMB-450"})] * 2
    assert len(runner.of("task")) == 1 and len(runner.of("comment")) == 1


async def test_a_tap_runs_after_a_message_in_the_same_conversation_was_filed_automatically(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    sink.state.record_ticket("jane-doe", "rcs:thread:thread-9", "JIMB-77", clock.now.timestamp() - 3600)
    assert await tap(sink, button_urls(ntfy)[0]) == (200, {"ticket_ref": "JIMB-77"})
    assert runner.of("task") == [] and runner.of("comment")[0][2] == "JIMB-77"


@pytest.mark.parametrize("tamper", ["flip", "exp", "other_secret", "other_id", "bare"])
async def test_a_forged_link_is_403_and_files_nothing(tmp_path, index, runner, clock, ntfy, plane_repo, tamper):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    path, query = split(button_urls(ntfy)[0])
    params = urllib.parse.parse_qs(query)
    exp, sig = int(params["exp"][0]), params["sig"][0]
    if tamper == "flip":
        sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    elif tamper == "exp":
        exp += 1
    elif tamper == "other_secret":
        sig = hmac.new(b"x" * 40, f"{det['id']}:{exp}".encode(), hashlib.sha256).hexdigest()
    elif tamper == "other_id":
        sig = sign("someone-elses-detection", exp)
    elif tamper == "bare":
        sig = "0" * 64
    status, body = await sink.button_request("POST", path, f"exp={exp}&sig={sig}")
    assert (status, body) == (403, {"error": "bad or expired signature"})
    assert runner.of("task") == [] and sink.state.button(det["id"])["state"] == "awaiting"


async def test_an_expired_link_is_403(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    url = button_urls(ntfy)[0]
    clock.now += timedelta(days=7, seconds=1)
    assert await tap(sink, url) == (403, {"error": "link expired"})
    path, query = split(url)
    exp = urllib.parse.parse_qs(query)["exp"][0]
    # an expired link that was never genuine learns nothing about expiry
    assert await sink.button_request("POST", path, f"exp={exp}&sig={'0' * 64}") == (403, {"error": "bad or expired signature"})
    assert runner.of("task") == [] and sink.state.button(det["id"])["state"] == "awaiting"
    clock.now -= timedelta(days=1)
    assert (await tap(sink, url))[0] == 200  # the same link, inside its seven days


async def test_a_link_good_on_day_six_still_files(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    clock.now += timedelta(days=6, hours=23)
    assert await tap(sink, button_urls(ntfy)[0]) == (200, {"ticket_ref": "JIMB-450"})


@pytest.mark.parametrize("query", ["", "exp=1", "sig=" + "0" * 64, "exp=1&sig=00", "exp=abc&sig=" + "0" * 64, "exp=1&exp=2&sig=" + "0" * 64,
                                   "exp=1&sig=" + "0" * 64 + "&sig=" + "1" * 64, "exp=%00&sig=" + "g" * 64])
async def test_a_malformed_link_is_403_without_asking_1password(tmp_path, runner, clock, ntfy, query):
    sink = SinkService(TargetRegistry([str(tmp_path / "none" / "*.toml")]), State(tmp_path / "s" / "state.db"), _Publisher(),
                       runner=runner, clock=clock, op=OP, ntfy_http=ntfy)
    assert await sink.button_request("POST", "/watch/ticket/does-not-exist", query) == (403, {"error": "bad or expired signature"})
    assert runner.calls == []


async def test_a_genuine_link_for_a_detection_that_is_not_stored_is_404(tmp_path, runner, clock, ntfy):
    sink = SinkService(TargetRegistry([str(tmp_path / "none" / "*.toml")]), State(tmp_path / "s" / "state.db"), _Publisher(),
                       runner=runner, clock=clock, op=OP, ntfy_http=ntfy)
    exp = int(clock.now.timestamp()) + 60
    assert await sink.button_request("POST", "/watch/ticket/does-not-exist", f"exp={exp}&sig={sign('does-not-exist', exp)}") == \
        (404, {"error": "unknown detection"})
    assert runner.of("task") == []


@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/watch/ticket/abc", 405), ("PUT", "/watch/ticket/abc", 405), ("HEAD", "/watch/ticket/abc", 405),
    ("POST", "/watch/other", 404), ("POST", "/watch/ticket/", 404), ("POST", "/watch/ticket/a/b", 404), ("POST", "/watch/", 404),
    ("POST", "/", 404), ("POST", "/upload/x", 404),
])
async def test_only_post_to_the_ticket_path_is_answered(tmp_path, runner, clock, ntfy, method, path, expected):
    sink = SinkService(TargetRegistry([str(tmp_path / "none" / "*.toml")]), State(tmp_path / "s" / "state.db"), _Publisher(),
                       runner=runner, clock=clock, op=OP, ntfy_http=ntfy)
    status, body = await sink.button_request(method, path, f"exp=1&sig={'0' * 64}")
    assert status == expected and "error" in body and runner.calls == []


async def test_with_no_secret_configured_a_well_formed_tap_is_503(tmp_path, runner, clock, ntfy, monkeypatch):
    monkeypatch.delenv("TARGET_WATCH_BUTTON_SECRET_REF")
    sink = SinkService(TargetRegistry([str(tmp_path / "none" / "*.toml")]), State(tmp_path / "s" / "state.db"), _Publisher(),
                       runner=runner, clock=clock, op=OP, ntfy_http=ntfy)
    status, body = await sink.button_request("POST", "/watch/ticket/x", f"exp=1&sig={'0' * 64}")
    assert status == 503 and "not configured" in body["error"]


async def test_an_unreadable_secret_is_not_asked_for_again_inside_a_minute(tmp_path, runner, clock, ntfy):
    runner.op_code = 1
    sink = SinkService(TargetRegistry([str(tmp_path / "none" / "*.toml")]), State(tmp_path / "s" / "state.db"), _Publisher(),
                       runner=runner, clock=clock, op=OP, ntfy_http=ntfy)
    for _ in range(5):
        assert (await sink.button_request("POST", "/watch/ticket/x", f"exp=1&sig={'0' * 64}"))[0] == 503
    assert len(runner.of("read")) == 1  # unsigned callers cannot drive op
    sink._button_key_retry_at = 0.0
    runner.op_code = 0
    assert (await sink.button_request("POST", "/watch/ticket/x", f"exp=1&sig={'0' * 64}"))[0] == 403  # readable again


async def test_a_failed_px_is_502_tells_the_operator_and_is_never_retried(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    runner.create_output, runner.create_code = "boom", 1
    status, body = await tap(sink, button_urls(ntfy)[0])
    await sink.settle()
    assert status == 502 and body["error"] == "ticket not filed" and "not retried" in body["reason"]
    stored = sink.state.button(det["id"])
    assert stored["state"] == "failed" and stored["action"] == "failed" and stored["ticket_ref"] is None
    assert ntfy.calls[-1]["text"].startswith("ticket not filed: px task create did not confirm")
    assert ntfy.calls[-1]["headers"]["Priority"] == "default"
    runner.create_output = None  # px is healthy again: a second tap must still not create
    status, body = await tap(sink, button_urls(ntfy)[0])
    assert status == 409 and len(runner.of("task")) == 1
    assert [n["outcome"] for n in rows_of(sink) if n["channel"] == "button"] == ["failed"]


async def test_a_run_interrupted_mid_px_is_failed_on_the_next_tap_never_re_run(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    sink.state.update_button(det["id"], state="pending", px_kind="ticket_created")  # the sink died while px ran
    status, body = await tap(sink, button_urls(ntfy)[0])
    await sink.settle()
    assert status == 409 and "interrupted while px was running" in body["reason"]
    assert runner.of("task") == [] and sink.state.button(det["id"])["state"] == "failed"
    assert "interrupted" in ntfy.calls[-1]["text"]


async def test_a_watch_that_no_longer_exists_is_409_and_the_button_stays_tappable(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    (tmp_path / "w" / "jane.toml").unlink()
    registry.reload()
    status, body = await tap(sink, button_urls(ntfy)[0])
    assert status == 409 and "no longer files tickets" in body["error"]
    assert runner.of("task") == [] and sink.state.button(det["id"])["state"] == "awaiting"


async def test_a_tap_never_blocks_on_a_slow_px(tmp_path, index, runner, clock, ntfy, plane_repo):
    """px runs in a worker thread: while it is busy the event loop still answers (another tap's 403 comes back at once)."""
    import threading

    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    release, entered = threading.Event(), threading.Event()
    real = runner.__call__

    def slow(argv, cwd=None, timeout=None):
        if runner.verb(argv) == "task":
            entered.set()
            release.wait(5)
        return real(argv, cwd=cwd, timeout=timeout)

    sink.runner = slow
    filing = asyncio.create_task(tap(sink, button_urls(ntfy)[0]))
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    assert await sink.button_request("POST", "/watch/ticket/x", "exp=1&sig=00") == (403, {"error": "bad or expired signature"})
    release.set()
    assert await filing == (200, {"ticket_ref": "JIMB-450"})


# --- status ------------------------------------------------------------------------------------


async def test_status_shows_whether_the_button_was_tapped_and_what_it_filed(tmp_path, index, runner, clock, ntfy, plane_repo):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    before = render(sink.state.recent_routes(5))
    assert "awaiting_button" in before and "Create Ticket button: not tapped yet" in before
    await tap(sink, button_urls(ntfy)[0])
    await sink.settle()
    after = render(sink.state.recent_routes(5))
    assert "Create Ticket button: filed: ticket_created JIMB-450" in after
    assert "JIMB-450" in after.splitlines()[0]  # the ticket column
    assert "button" in after and "[JIMB-450]" in after and "(button follow-up)" in after
    sink.state.update_button(det["id"], state="failed")
    assert "tapped; no ticket was filed" in render(sink.state.recent_routes(5))


# --- dry run ------------------------------------------------------------------------------------


async def test_dry_run_prints_the_button_and_earshot_plan_and_touches_nothing(tmp_path, index, runner, clock, ntfy, plane_repo, capsys):
    sink, registry = make(tmp_path, runner, clock, ntfy, plane_repo, notify=[f"ntfy:{TOPIC}", "earshot:alarm", "telegram:1000"])
    dry_path = tmp_path / "dry" / "state.db"
    dry = SinkService(registry, State(dry_path, read_only=True), DryRunPublisher(), runner=runner, clock=clock, dry_run=True,
                      ntfy_http=ntfy, op=OP)
    await dry.handle(detect(index, registry, burst(1)))
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert printed["action"] == "awaiting_button" and printed["px"] is None
    ntfy_plan, earshot_plan, telegram = printed["notify"]
    assert ntfy_plan["button"]["label"] == "Create Ticket" and ntfy_plan["button"]["method"] == "POST"
    assert "sig=" in ntfy_plan["button"]["url"] and ntfy_plan["priority"] == "urgent"
    assert earshot_plan == {"to": "earshot:alarm", "spoken": "Jane Doe texted: message number 1", "skipped": None,
                            "argv": [EARSHOT, "say", "-c", "alarm", "--", "Jane Doe texted: message number 1"]}
    assert telegram["to"] == "telegram:1000"
    assert runner.calls == [] and ntfy.calls == [] and not dry_path.exists()  # no op read, no push, no state, no button row


# --- the listener -------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def http(port: int, raw: bytes, timeout: float = 5) -> tuple[int, dict, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), timeout)  # one request per connection: the server closes
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:])}
    return int(lines[0].split()[1]), headers, json.loads(body)


def post(path_and_query: str, extra: str = "", body: bytes = b"") -> bytes:
    return (f"POST {path_and_query} HTTP/1.1\r\nHost: agents.example\r\n{extra}Content-Length: {len(body)}\r\n\r\n").encode() + body


@pytest.fixture
async def listener_for(tmp_path, runner, clock, ntfy):
    sink = SinkService(TargetRegistry([str(tmp_path / "none" / "*.toml")]), State(tmp_path / "s" / "state.db"), _Publisher(),
                       runner=runner, clock=clock, op=OP, ntfy_http=ntfy)
    started = []

    async def start(handler=None, port=None):
        port = port or free_port()
        listener = btn.ButtonListener(handler or sink.button_request, f"127.0.0.1:{port}", retry_s=0.05)
        await listener.start()
        started.append(listener)
        return listener, port

    yield start
    for listener in started:
        await listener.close(grace=1)


async def test_the_listener_answers_the_proof_request_with_json(listener_for):
    listener, port = await listener_for()
    status, headers, body = await http(port, post("/watch/ticket/does-not-exist?exp=1&sig=00"))
    assert (status, body) == (403, {"error": "bad or expired signature"})
    assert headers["content-type"] == "application/json" and headers["cache-control"] == "no-store" and headers["connection"] == "close"
    assert listener.address == ("127.0.0.1", port)


async def test_a_real_tap_over_http_files_the_ticket(tmp_path, index, runner, clock, ntfy, plane_repo, listener_for):
    sink, registry, [det] = await pushed(tmp_path, index, runner, clock, ntfy, plane_repo)
    _, port = await listener_for(sink.button_request)
    path, query = split(button_urls(ntfy)[0])
    status, _, body = await http(port, post(f"{path}?{query}"))
    assert (status, body) == (200, {"ticket_ref": "JIMB-450"})
    again = await http(port, post(f"{path}?{query}"))
    assert again[0] == 200 and again[2] == {"ticket_ref": "JIMB-450"} and len(runner.of("task")) == 1
    await sink.settle()
    assert ntfy.calls[-1]["text"] == "JIMB-450 filed"


async def test_the_listener_drains_a_chunked_body_and_a_content_length_body(listener_for):
    _, port = await listener_for()
    chunked = ("POST /watch/ticket/x?exp=1&sig=00 HTTP/1.1\r\nHost: a\r\nTransfer-Encoding: chunked\r\n\r\n"
               "5\r\nhello\r\n0\r\n\r\n").encode()
    assert (await http(port, chunked))[0] == 403
    assert (await http(port, post("/watch/ticket/x?exp=1&sig=00", body=b"x" * 1000)))[0] == 403


async def test_the_listener_refuses_other_methods_and_says_which_is_allowed(listener_for):
    _, port = await listener_for()
    status, headers, body = await http(port, b"GET /watch/ticket/x?exp=1&sig=00 HTTP/1.1\r\nHost: a\r\n\r\n")
    assert status == 405 and headers["allow"] == "POST" and body == {"error": "use POST"}


@pytest.mark.parametrize("raw", [
    b"GARBAGE\r\n\r\n", b"POST /x HTTP/2.0\r\n\r\n", b"POST /x HTTP/1.1\r\nContent-Length: nope\r\n\r\n",
    b"POST /" + b"a" * 20000 + b" HTTP/1.1\r\n\r\n", b"POST /x HTTP/1.1\r\n" + b"X: y\r\n" * 200 + b"\r\n",
    b"POST /x HTTP/1.1\r\nContent-Length: 99999999\r\n\r\n", b"POST /x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
])
async def test_the_listener_answers_a_bad_request_with_400(listener_for, raw):
    _, port = await listener_for()
    assert (await http(port, raw))[0] == 400


async def test_a_client_that_never_finishes_its_request_is_cut_off(listener_for, monkeypatch):
    monkeypatch.setattr(btn, "READ_TIMEOUT_S", 0.2)
    _, port = await listener_for()
    status, _, body = await http(port, b"POST /watch/ticket/x HTTP/1.1\r\nHost: a\r\n", timeout=3)  # headers never end
    assert status == 400 and body == {"error": "bad request"}


async def test_a_handler_that_raises_is_a_500_and_the_listener_keeps_serving(listener_for):
    calls = []

    async def handler(method, path, query):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return 200, {"ok": True}

    _, port = await listener_for(handler)
    assert (await http(port, post("/a")))[::2] == (500, {"error": "internal error"})
    assert (await http(port, post("/b")))[::2] == (200, {"ok": True})


async def test_the_listener_stays_off_without_an_address(caplog):
    for value in (None, "", "not-an-address"):
        listener = btn.ButtonListener(lambda *a: None, value)
        await listener.start()
        assert listener.server is None and listener.address is None
        await listener.close()


async def test_a_taken_port_is_retried_in_the_background_until_it_frees(listener_for):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    listener, _ = await listener_for(port=port)
    assert listener.server is None  # start() returned: the sink never waits for the port
    blocker.close()
    for _ in range(100):
        if listener.server is not None:
            break
        await asyncio.sleep(0.02)
    assert listener.server is not None
    assert (await http(port, post("/watch/ticket/x?exp=1&sig=00")))[0] == 403


async def test_close_lets_a_tap_in_flight_finish_and_then_refuses_new_ones(listener_for):
    gate = asyncio.Event()

    async def slow(method, path, query):
        await gate.wait()
        return 200, {"ticket_ref": "JIMB-1"}

    listener, port = await listener_for(slow)
    request = asyncio.create_task(http(port, post("/x")))
    await asyncio.sleep(0.1)
    closing = asyncio.create_task(listener.close(grace=5))
    await asyncio.sleep(0.1)
    assert not closing.done()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    gate.set()
    assert (await request)[::2] == (200, {"ticket_ref": "JIMB-1"})
    await closing


def test_the_listener_binds_a_free_bind_address_that_does_not_exist_yet():
    """IP_FREEBIND: the docker0 address may not exist when the sink starts at boot. 192.0.2.1 (TEST-NET-1) is never local."""
    try:
        sock = btn.listen_socket("192.0.2.1", free_port())
    except OSError as exc:  # a kernel without the option cannot prove it; the retry loop covers that host
        pytest.skip(f"no IP_FREEBIND here: {exc}")
    sock.close()


# --- earshot ----------------------------------------------------------------------------------


async def test_earshot_speaks_through_the_cli_between_ntfy_and_telegram(tmp_path, index, runner, clock, ntfy):
    sink, registry = make_notify_only(tmp_path, runner, clock, ntfy, notify=[f"ring:{RING_TO}", "telegram:1000", "earshot:alarm", f"ntfy:{TOPIC}"])
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert [runner.verb(argv) for argv, _ in runner.calls] == ["read", "say", "send", "read"]  # ntfy token, earshot, telegram, ring key
    [say] = runner.of("say")
    assert say == [EARSHOT, "say", "-c", "alarm", "--", "Jane Doe texted: message number 1"]
    assert [cwd for argv, cwd in runner.calls if runner.verb(argv) == "say"] == [sink_module.NOTIFY_CWD]
    assert routed["data"]["notify_channels"] == [f"ntfy:{TOPIC}", "earshot:alarm", "telegram:1000", f"ring:{RING_TO}"]
    notes = {n["channel"]: n for n in rows_of(sink)}
    assert notes["earshot:alarm"]["outcome"] == "sent" and notes["earshot:alarm"]["ref"].startswith("alarm/2026/")
    assert sink.state.last_ring("jane-doe", "earshot:alarm") == clock.now.timestamp()  # its own cooldown row
    assert sink.state.last_ring("jane-doe", f"ring:{RING_TO}") == clock.now.timestamp()  # independent of the ring's


async def test_earshot_follows_the_rings_cooldown(tmp_path, index, runner, clock, ntfy):
    sink, registry = make_notify_only(tmp_path, runner, clock, ntfy, notify=["earshot:alarm"])
    await sink.handle(detect(index, registry, burst(1)))
    clock.now += timedelta(minutes=1)
    routed = await sink.handle(detect(index, registry, burst(2)))
    assert len(runner.of("say")) == 1 and routed["data"]["notify_channels"] == []  # throttled: neither sent nor failed
    assert [n["outcome"] for n in sink.state.recent_routes(1)[0]["notifications"]] == ["throttled"]
    clock.now += timedelta(minutes=10)  # the default cooldown is 10 minutes
    await sink.handle(detect(index, registry, burst(3)))
    assert len(runner.of("say")) == 2


async def test_earshot_has_its_own_cooldown_per_channel(tmp_path, index, runner, clock, ntfy):
    sink, registry = make_notify_only(tmp_path, runner, clock, ntfy, notify=["earshot:alarm", "earshot:other"])
    await sink.handle(detect(index, registry, burst(1)))
    assert [s[3] for s in runner.of("say")] == ["alarm", "other"]


async def test_earshot_honours_a_custom_cooldown(tmp_path, index, runner, clock, ntfy):
    sink, registry = make_notify_only(tmp_path, runner, clock, ntfy, notify=["earshot:alarm"], extra="ring_cooldown_minutes = 1")
    await sink.handle(detect(index, registry, burst(1)))
    clock.now += timedelta(minutes=2)
    await sink.handle(detect(index, registry, burst(2)))
    assert len(runner.of("say")) == 2


async def test_earshot_keeps_quiet_hours(tmp_path, index, runner, clock, ntfy):
    sink, registry = make_notify_only(tmp_path, runner, clock, ntfy, notify=["earshot:alarm", "telegram:1000"],
                                      extra='ring_quiet_hours = "08:00-12:00"')  # the clock reads 10:30 in New York
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert runner.of("say") == [] and len(runner.of("send")) == 1  # telegram still goes out
    assert routed["data"]["notify_channels"] == ["telegram:1000"]
    assert [n["outcome"] for n in rows_of(sink)] == ["quiet", "sent"]


async def test_earshot_does_not_speak_a_stale_detection(tmp_path, index, runner, clock, ntfy, monkeypatch):
    monkeypatch.setattr(sink_module, "RING_MAX_AGE_S", 300)  # the burst is stamped 15:00, the clock reads 15:30
    sink, registry = make_notify_only(tmp_path, runner, clock, ntfy, notify=["earshot:alarm"])
    await sink.handle(detect(index, registry, burst(1)))
    assert runner.of("say") == [] and [n["outcome"] for n in rows_of(sink)] == ["stale"]


@pytest.mark.parametrize("code,out,cooldown", [
    (0, "uploaded, trust me\n", True),  # exit 0 without an object key: it may have played
    (124, None, True),  # a timeout: the line may already be uploaded
    (1, None, False),  # a plain failure dropped nothing
    (127, None, False),  # the binary is missing
])
async def test_earshot_success_needs_exit_0_and_the_object_key(tmp_path, index, runner, clock, ntfy, code, out, cooldown):
    runner.earshot_code, runner.earshot_out = code, out
    sink, registry = make_notify_only(tmp_path, runner, clock, ntfy, notify=["earshot:alarm"])
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert routed["data"]["notify_channels"] == [] and "notify failed: earshot:alarm" in routed["data"]["reason"]
    [note] = rows_of(sink)
    assert note["outcome"] == "failed" and note["ref"] is None
    assert (sink.state.last_ring("jane-doe", "earshot:alarm") is not None) is cooldown
    await sink.handle(detect(index, registry, burst(2)))
    assert len(runner.of("say")) == (1 if cooldown else 2)


async def test_an_object_key_from_another_channel_is_not_success(tmp_path, index, runner, clock, ntfy):
    runner.earshot_out = "other/2026/11/04/x.opus\n"
    sink, registry = make_notify_only(tmp_path, runner, clock, ntfy, notify=["earshot:alarm"])
    routed = await sink.handle(detect(index, registry, burst(1)))
    assert "notify failed: earshot:alarm" in routed["data"]["reason"]


async def test_a_ring_failure_report_never_goes_out_over_earshot_or_as_a_hermes_channel(tmp_path, runner, clock, ntfy):
    sink, registry = make_notify_only(tmp_path, runner, clock, ntfy,
                                      notify=["earshot:alarm", "telegram:1000", f"ntfy:{TOPIC}", f"ring:{RING_TO}"])
    await sink._tell_ring_failed(registry.get("jane-doe"), "ring:...0142", "nobody answered")
    assert [s[s.index("--to") + 1] for s in runner.of("send")] == ["telegram:1000"]
    assert runner.of("say") == [] and len(ntfy.calls) == 1


async def test_a_spoken_alarm_is_not_a_loud_push_the_ntfy_headers_are_unchanged(tmp_path, index, runner, clock, ntfy):
    sink, registry = make_notify_only(tmp_path, runner, clock, ntfy, notify=[f"ntfy:{TOPIC}", "earshot:alarm"])
    await sink.handle(detect(index, registry, burst(1)))
    [call] = ntfy.calls
    assert call["headers"]["Priority"] == "urgent" and call["headers"]["Tags"] == "rotating_light" and "Actions" not in call["headers"]


# --- what is spoken ---------------------------------------------------------------------------


def said(provider, *, text=None, subject=None, name="Jane Doe"):
    content = {k: v for k, v in (("text", text), ("subject", subject)) if v}
    return spoken_line({"target_display_name": name, "provider": provider, "content": content})


@pytest.mark.parametrize("provider", ["sms", "rcs", "mms", "imessage", "telegram", "slack", "google_chat"])
def test_a_message_is_spoken_as_texted(provider):
    assert said(provider, text="running ten minutes late") == "Jane Doe texted: running ten minutes late"


def test_an_email_is_spoken_by_its_subject():
    assert said("email", text="body words", subject="Invoice question") == "Jane Doe emailed: Invoice question"
    assert said("email", text="only a body") == "Jane Doe emailed: only a body"
    assert said("email") == "Jane Doe emailed"


def test_a_call_is_spoken_as_called_and_a_voicemail_carries_its_words():
    assert said("call") == "Jane Doe called"
    assert said("call", text="ignored for a plain call") == "Jane Doe called"
    assert said("voicemail", text="call me back") == "Jane Doe called and left a voicemail: call me back"
    assert said("voicemail") == "Jane Doe called and left a voicemail"
    assert said("sms") == "Jane Doe texted"


def test_no_url_is_read_aloud():
    assert said("sms", text="see https://example.com/a?b=1&c=2 now") == "Jane Doe texted: see link now"
    assert said("sms", text="www.example.com/x or http://example.org") == "Jane Doe texted: link or link"
    assert said("email", subject="HTTPS://EXAMPLE.COM/Z") == "Jane Doe emailed: link"
    assert "http" not in said("sms", text="a https://example.com/very/long/path?token=abcdef b").lower()


def test_whitespace_is_flattened():
    assert said("sms", text="line one\n\nline   two\ttabbed") == "Jane Doe texted: line one line two tabbed"


def test_a_long_line_is_cut_at_a_word_boundary_within_200_characters():
    line = said("sms", text="word " * 100)
    assert len(line) <= 200 and line.endswith("...") and line[:-3].split()[-1] == "word"
    assert said("sms", text="x" * 500).endswith("xx...") and len(said("sms", text="x" * 500)) == 200  # one huge token is cut hard
    exact = "y" * (200 - len("Jane Doe texted: "))
    assert said("sms", text=exact) == f"Jane Doe texted: {exact}"  # exactly 200 is left alone
    assert len(said("sms", text=exact + "z")) <= 200


def test_trim_words_leaves_short_text_alone():
    assert trim_words("short") == "short" and trim_words("a b c", 5) == "a b c"


# --- state ------------------------------------------------------------------------------------


def test_button_rows_are_stored_once_and_updated_only_in_their_known_fields(tmp_path):
    state = State(tmp_path / "s.db")
    detection = {"id": "d1", "data": {"x": 1}}
    state.open_button("d1", "w", detection, ["ntfy:a"])
    state.open_button("d1", "w", {"id": "d1", "data": {"x": 2}}, ["ntfy:b"])  # a re-sent push does not overwrite
    row = state.button("d1")
    assert row["detection"] == detection and row["channels"] == ["ntfy:a"] and row["state"] == "awaiting"
    state.update_button("d1", state="done", ticket_ref="JIMB-1")
    assert state.button("d1")["ticket_ref"] == "JIMB-1"
    with pytest.raises(KeyError):
        state.update_button("d1", detection="{}")
    with pytest.raises(KeyError):
        state.update_button("d1", watch_id="other")
    assert state.button("nope") is None


def test_a_state_file_from_before_the_button_still_reads_and_a_writer_adds_the_table(tmp_path):
    path = tmp_path / "state.db"
    state = State(path)
    state.open_route("d1", "w", "rcs:thread:t", None, 1.0)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE buttons")
    legacy = State(path, read_only=True)
    assert legacy.button("d1") is None
    [route] = legacy.recent_routes(5)
    assert route["button"] is None
    State(path)  # the sink recreates it on start
    assert State(path).button("d1") is None and State(path).recent_routes(5)[0]["button"] is None
