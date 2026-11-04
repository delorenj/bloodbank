import io
import json
import tomllib
from pathlib import Path

from conftest import call_ended, rcs, transcription, write_target
from target_watch import cli
from target_watch.replay import fetch_events, replay

SERVICE = Path(__file__).resolve().parents[1]


class FakeCandystore:
    def __init__(self, by_domain):
        self.by_domain = by_domain
        self.urls = []

    def __call__(self, url):
        from urllib.parse import parse_qs, urlparse

        self.urls.append(url)
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        events = self.by_domain.get(q["domain"], [])
        offset, limit = int(q["offset"]), int(q["limit"])
        return io.BytesIO(json.dumps({"events": events[offset:offset + limit], "total": None}).encode())


def test_fetch_pages_and_sorts(monkeypatch):
    import target_watch.replay as replay_mod

    monkeypatch.setattr(replay_mod, "PAGE", 2)
    texts = [rcs(message_id=f"m{i}", event_id=f"0b9c6a62-1111-4d0e-8f5f-00000000010{i}") for i in range(3)]
    for i, event in enumerate(texts):
        event["time"] = f"2026-11-04T15:0{3 - i}:00Z"
    store = FakeCandystore({"messaging": texts, "telephony": [call_ended()]})
    events = fetch_events("2026-11-04T00:00:00Z", base_url="http://candystore.test", opener=store)
    assert len(events) == 4
    assert [e["time"] for e in events] == sorted(e["time"] for e in events)
    assert any("domain=telephony" in u for u in store.urls) and any("offset=2" in u for u in store.urls)
    assert all("from=2026-11-04T00%3A00%3A00Z" in u for u in store.urls)


def test_replay_reports_detections_and_folds_near_duplicates(index, registry):
    events = [
        rcs(message_id="a"),
        rcs(message_id="b", received_at="2026-11-04T15:00:00.010Z", event_id="0b9c6a62-1111-4d0e-8f5f-0000000000d1"),
        rcs(sender="+12155550999", message_id="c", event_id="0b9c6a62-1111-4d0e-8f5f-0000000000d2"),
        call_ended(), transcription(),
        {"type": "bloodbank.telephony.message.received", "id": "old", "data": {"from": "+12155550100"}},
    ]
    rows = replay(events, index, registry.current())
    assert [(r["provider"], r["near_duplicate"]) for r in rows] == [
        ("rcs", False), ("rcs", True), ("voicemail", False), ("voicemail", False),
    ]
    assert replay(events, index, registry.current(), target_id="someone-else") == []


def test_check_passes_on_good_targets_and_fails_on_bad(jane_file, schema_tree, capsys, tmp_path):
    assert cli.main(["--targets", str(jane_file), "--schemas-dir", str(schema_tree), "check"]) == 0
    out = capsys.readouterr().out
    assert "jane-doe" in out and "bloodbank.messaging.rcs.received" in out and out.strip().endswith("ok")

    write_target(tmp_path / "bad", "bad", '[target]\nid="Bad"\n')
    assert cli.main(["--targets", str(tmp_path / "bad" / "*.toml"), "--schemas-dir", str(schema_tree), "check"]) == 1

    empty = tmp_path / "empty-schemas"
    (empty / "bloodbank").mkdir(parents=True)
    assert cli.main(["--targets", str(jane_file), "--schemas-dir", str(empty), "check"]) == 1
    assert "nothing can be matched" in capsys.readouterr().out


def test_check_flags_a_plane_repo_without_project_json(tmp_path, schema_tree, capsys):
    write_target(tmp_path / "w", "t", f'[target]\nid="t"\ndisplay_name="T"\n[target.handles]\nlabel=["T"]\n'
                 f'[sink]\nkind="plane"\nrepo="{tmp_path / "nowhere"}"')
    assert cli.main(["--targets", str(tmp_path / "w" / "*.toml"), "--schemas-dir", str(schema_tree), "check"]) == 1
    assert ".project.json" in capsys.readouterr().out


def test_match_and_sink_dry_run_from_file(jane_file, schema_tree, tmp_path, capsys):
    source = tmp_path / "events.jsonl"
    source.write_text("\n".join(json.dumps(e) for e in (rcs(), rcs(sender="+12155550999", message_id="x"))))
    common = ["--targets", str(jane_file), "--schemas-dir", str(schema_tree), "--state-dir", str(tmp_path / "st")]
    assert cli.main([*common, "match", "--dry-run", "--from-file", str(source)]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    [published] = [line["dry_run_publish"] for line in lines]
    assert published["type"] == "bloodbank.watch.target.detected"

    detection = tmp_path / "detection.json"
    detection.write_text(json.dumps(published))
    assert cli.main([*common, "sink", "--dry-run", "--from-file", str(detection)]) == 0
    plan = json.loads(capsys.readouterr().out.splitlines()[0])
    assert plan["action"] == "ticket_created" and plan["px"]["argv"][:3] == ["px", "task", "create"]
    assert not (tmp_path / "st" / "state.db").exists()


def test_candybar_manifest():
    path = SERVICE / ".candybars" / "target-watch-gmail-poll.candybar.toml"
    doc = tomllib.loads(path.read_text())
    bar, runner = doc["candybar"], doc["candybar"]["runner"]
    assert bar["name"] == path.name.removesuffix(".candybar.toml")
    assert bar["schedule"] == "*/5 * * * *" and bar["timezone"] == "America/New_York" and bar["enabled"] is False
    assert runner["kind"] == "exec" and runner["argv"][0].endswith("/.venv/bin/target-watch") and runner["argv"][1:] == ["gmail-poll"]
    assert all(Path(p).is_absolute() for p in (runner["argv"][0], runner["workdir"]))
    assert "ntfy-alert" in " ".join(runner["on_failure"]) and "$CANDYBAR_FAILURE_MESSAGE" in runner["on_failure"][-1]


def test_systemd_units():
    for name, verb in (("target-watch-match.service", "match"), ("target-watch-sink.service", "sink")):
        text = (SERVICE / "systemd" / name).read_text()
        exec_start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
        assert exec_start.endswith(f"target-watch {verb}") and "%h/code/33GOD/bloodbank/services/target-watch" in exec_start
        assert "Restart=always" in text and "RestartMaxDelaySec=" in text and "StartLimitIntervalSec=0" in text
        assert ".timer" not in text.replace("no .timer", "")
