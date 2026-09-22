"""No key means enabled: the installed-hook inventory's `enabled` reading.

An absent `enabled` on an installed hub hook (or on Codex's native loader
report of it) means the hook is enabled. Only an explicit `false` disables
it; a present non-boolean is reported as invalid rather than silently read
as either state.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from health import installed_inventory
from health.installed_inventory import collect_installed_inventory, native_rows
import sync


def _codex_inventory(tmp_path, monkeypatch, *, native_enabled):
    import codex_native

    master = sync.load_master()
    agent = master["agents"]["codex"]
    master["agents"] = {"codex": agent}
    generated = sync.render_config(agent, master["lifecycle"], sync.load_lock())
    config = tmp_path / "default" / "hooks.json"
    config.parent.mkdir()
    config.write_text(json.dumps(generated))

    def loaded(_config):
        rows = []
        for row in native_rows(generated, "codex"):
            hook = {"sourcePath": str(config), "command": row["command"],
                    "trustStatus": "trusted", "timeoutSec": row["timeout"]}
            if native_enabled is not _ABSENT:
                hook["enabled"] = native_enabled
            rows.append(hook)
        return {"hooks": rows}

    monkeypatch.setattr(codex_native, "capture_trust", loaded)
    monkeypatch.setattr(installed_inventory, "config_paths", lambda *_: [("default", config, None)])
    monkeypatch.setattr(installed_inventory, "_binary_available", lambda *_: True)
    return collect_installed_inventory(master)


_ABSENT = object()


def _issues(result):
    return {issue for native in result["clis"][0]["natives"]
            for source in native["sources"] for issue in source["issues"]}


def test_native_loader_without_enabled_key_is_enabled(tmp_path, monkeypatch):
    result = _codex_inventory(tmp_path, monkeypatch, native_enabled=_ABSENT)
    assert "native_disabled" not in _issues(result)
    assert result["status"] == "healthy"


def test_native_loader_explicit_false_is_disabled(tmp_path, monkeypatch):
    result = _codex_inventory(tmp_path, monkeypatch, native_enabled=False)
    assert "native_disabled" in _issues(result)


@pytest.mark.parametrize("value", ["true", 1, None])
def test_native_loader_non_boolean_enabled_is_invalid(tmp_path, monkeypatch, value):
    result = _codex_inventory(tmp_path, monkeypatch, native_enabled=value)
    issues = _issues(result)
    assert "native_enabled_invalid" in issues
    assert "native_disabled" not in issues
