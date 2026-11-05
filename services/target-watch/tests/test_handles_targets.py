from pathlib import Path

import pytest

from conftest import write_target
from target_watch import paths
from target_watch.handles import label_candidates, norm_e164, norm_email, norm_label, norm_platform
from target_watch.targets import TargetRegistry, load_targets


@pytest.mark.parametrize("raw,expected", [
    ("Jane.Doe@Example.com", "jane.doe@example.com"),
    ("  Jane Doe <Jane.Doe@Example.com> ", "jane.doe@example.com"),
    ("mailto:jd@example.org", "jd@example.org"),
    ("not an address", None),
    ("jane@", None),
    (None, None),
])
def test_email(raw, expected):
    assert norm_email(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("+12155550100", "+12155550100"),
    ("(215) 555-0100", "+12155550100"),
    ("215.555.0100", "+12155550100"),
    ("1 215 555 0100", "+12155550100"),
    ("tel:+44 20 7946 0958", "+442079460958"),
    ("12155550100@vtext.example", "+12155550100"),
    ("Jane Doe", None),
    ("anonymous", None),
    ("555-0100", None),
])
def test_e164(raw, expected):
    assert norm_e164(raw) == expected


def test_labels_and_group_titles():
    assert norm_label("  Jane   DOE ") == "jane doe"
    assert label_candidates("Jane Doe") == [("jane doe", None)]
    assert label_candidates("Crew Chat: Jane Doe") == [("crew chat: jane doe", None), ("jane doe", "Crew Chat")]
    # The LAST ': ' splits, so a group title with a colon keeps it.
    assert label_candidates("Ops: Night: Jane Doe")[-1] == ("jane doe", "Ops: Night")
    assert label_candidates("") == []


def test_platform():
    assert norm_platform("slack:U0JANE") == "slack:u0jane"
    assert norm_platform("U0JANE", "slack") == "slack:u0jane"
    assert norm_platform("slack:U0JANE", "slack") == "slack:u0jane"
    assert norm_platform("4242", "telegram") == "telegram:4242"
    assert norm_platform("no-platform") is None


def test_target_file_normalises_every_handle(jane_file, plane_repo):
    targets, errors = load_targets([str(jane_file)])
    assert errors == []
    jane = targets[0]
    assert jane.id == "jane-doe"
    assert jane.email == {"jane.doe@example.com", "jd@example.org"}
    assert jane.e164 == {"+12155550100"}
    assert jane.label == {"jane doe", "janie d"}
    assert jane.platform == {"slack:u0jane", "telegram:4242"}
    assert jane.sink.kind == "plane" and jane.sink.repo == plane_repo
    assert jane.sink.labels == ("submitted-by-client",) and jane.sink.priority == "medium"
    assert jane.sink.notify == ("telegram:1000",)


@pytest.mark.parametrize("body,needle", [
    ('[target]\nid="Bad Id"\ndisplay_name="x"\n[target.handles]\nlabel=["x"]\n[sink]\nkind="notify_only"', "lowercase slug"),
    ('[target]\nid="x"\ndisplay_name="x"\n[sink]\nkind="notify_only"', "at least one handle"),
    ('[target]\nid="x"\ndisplay_name="x"\n[target.handles]\nphone=["1"]\n[sink]\nkind="notify_only"', "unknown handle kinds"),
    ('[target]\nid="x"\ndisplay_name="x"\n[target.handles]\nemail=["nope"]\n[sink]\nkind="notify_only"', "not a valid email"),
    ('[target]\nid="x"\ndisplay_name="x"\n[target.handles]\nlabel=["x"]\n[sink]\nkind="plane"', "needs sink.repo"),
    ('[target]\nid="x"\ndisplay_name="x"\n[target.handles]\nlabel=["x"]\n[sink]\nkind="notify_only"\nnotify=["7564"]', "telegram:<chat id>"),
    ('[target]\nid="x"\ndisplay_name="x"\n[target.handles]\nlabel=["x"]\n[sink]\nkind="email"', "sink.kind"),
    ('not toml [', "line 1"),
])
def test_bad_target_files_are_reported_not_fatal(tmp_path, body, needle):
    bad = write_target(tmp_path, "bad", body)
    good = write_target(tmp_path, "good", '[target]\nid="ok"\ndisplay_name="Ok"\n[target.handles]\nlabel=["Ok"]\n[sink]\nkind="notify_only"')
    targets, errors = load_targets([str(tmp_path / "*.toml")])
    assert [t.id for t in targets] == ["ok"]
    assert len(errors) == 1 and str(bad) in errors[0] and needle in errors[0]
    assert good.exists()


def test_errors_never_echo_a_handle(tmp_path):
    write_target(tmp_path, "bad", '[target]\nid="x"\ndisplay_name="x"\n[target.handles]\nemail=["secret-person-at-example"]\n[sink]\nkind="notify_only"')
    _, errors = load_targets([str(tmp_path / "*.toml")])
    assert "secret-person" not in errors[0]


def test_duplicate_ids_keep_the_first(tmp_path):
    body = '[target]\nid="dup"\ndisplay_name="{}"\n[target.handles]\nlabel=["x"]\n[sink]\nkind="notify_only"'
    write_target(tmp_path, "a", body.format("A"))
    write_target(tmp_path, "b", body.format("B"))
    targets, errors = load_targets([str(tmp_path / "*.toml")])
    assert [t.display_name for t in targets] == ["A"] and "duplicate target id" in errors[0]


def test_default_globs_include_the_builtin_selftest(monkeypatch):
    monkeypatch.delenv("TARGET_WATCH_PATHS", raising=False)
    globs = paths.target_globs()
    assert "~/code/*/.watch/*.toml" in globs and "~/code/33GOD/*/.watch/*.toml" in globs
    assert str(paths.BUILTIN_TARGETS / "*.toml") in globs
    targets, errors = load_targets([str(paths.BUILTIN_TARGETS / "*.toml")])
    assert errors == []
    selftest = next(t for t in targets if t.id == "watch-selftest")
    assert selftest.email == {"watch-selftest@delo.sh"} and selftest.label == {"watch selftest"}
    assert selftest.sink.kind == "notify_only" and selftest.sink.notify == ("telegram:7564050286",)


def test_env_paths_replace_the_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("TARGET_WATCH_PATHS", f"{tmp_path}/a/*.toml:{tmp_path}/b/*.toml")
    assert paths.target_globs() == [f"{tmp_path}/a/*.toml", f"{tmp_path}/b/*.toml"]


def test_registry_reloads_when_a_target_file_appears(tmp_path):
    registry = TargetRegistry([str(tmp_path / "*.toml")], recheck_s=0)
    assert registry.current() == []
    write_target(tmp_path, "new", '[target]\nid="new"\ndisplay_name="New"\n[target.handles]\nlabel=["New"]\n[sink]\nkind="notify_only"')
    assert [t.id for t in registry.current()] == ["new"]
    assert registry.get("new").display_name == "New"


def test_repo_ships_no_real_handles():
    """Public repo: target files and docs carry fake or operator-owned handles only."""
    import re

    service = Path(__file__).resolve().parents[1]
    allowed_numbers = re.compile(r"^\+1\d{3}555\d{4}$")
    for path in [*service.glob("targets/*.toml"), service / "README.md"]:
        text = path.read_text()
        for number in re.findall(r"\+\d{10,15}", text):
            assert allowed_numbers.match(number), f"{path.name} carries a real-looking number"
        for address in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
            if address.endswith("@sip.telnyx.com"):  # a SIP ring target: only the documented fake user may appear
                assert address in {"example-user@sip.telnyx.com", "...er@sip.telnyx.com"}, "a real-looking SIP user"
                continue
            assert address.split("@")[1] in {"example.com", "example.org", "example.net", "delo.sh", "gmail.com"}, address
