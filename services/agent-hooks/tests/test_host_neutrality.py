"""Multi-peer host-neutrality: template rendering, canonical comparison, overlays."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

AGENT_HOOKS_DIR = Path(__file__).resolve().parents[1]
HOOK_HUB_DIR = AGENT_HOOKS_DIR.parent / "hook-hub"


def _load_sync_module():
    spec = importlib.util.spec_from_file_location("bloodbank_sync_multihost", AGENT_HOOKS_DIR / "sync.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SYNC = _load_sync_module()
MASTER = SYNC.load_master()
LOCK = SYNC.load_lock()


@pytest.fixture(autouse=True)
def _expose_active_hooks_dir(monkeypatch):
    """sync.py resolves the hooks dir per call; expose it under SYNC.HOOKS_DIR for tests."""
    monkeypatch.setattr(SYNC, "HOOKS_DIR", SYNC._hooks_dir())
    monkeypatch.setattr(SYNC, "BLOODBANK_HOOK_LINK", SYNC._hooks_dir() / "bloodbank")


class TestTemplateRendering:
    """The committed artifacts must carry no host-specific home path."""

    def test_committed_config_targets_carry_no_host_home(self):
        for agent in MASTER["agents"].values():
            target = agent.get("config_target")
            if not target:
                continue
            raw = (AGENT_HOOKS_DIR / target).read_text()
            assert str(Path.home()) not in raw, f"{target} contains this host's home"
            assert "$HOME/.agents/hooks/bb-hook" in raw or agent["dialect"] in {
                "watcher", "runtime"}, f"{target} lost the hooks-dir token"


class TestRenderModes:
    """Template and install modes describe the same commands on this host."""

    def _install_render(self, name: str) -> dict:
        return SYNC.render_config(MASTER["agents"][name], MASTER["lifecycle"], LOCK)

    def test_template_localizes_to_install_render(self):
        for name, agent in MASTER["agents"].items():
            if agent.get("dialect") in {"watcher", "runtime"} or not agent.get("config_target"):
                continue
            install = self._install_render(name)
            template = SYNC.render_config(
                MASTER["agents"][name], MASTER["lifecycle"], LOCK, mode=SYNC.RENDER_TEMPLATE
            )
            assert SYNC.localize_template(template) == install, name

    def test_hermes_install_render_is_literal(self):
        hooks = self._install_render("hermes")["hooks"]
        commands = [e["command"] for entries in hooks.values() for e in entries]
        assert commands
        assert all(SYNC.HOOKS_DIR.as_posix() in c for c in commands)

    def test_generate_defaults_to_template(self):
        generated = SYNC.generate(MASTER, LOCK)
        claude = generated[AGENT_HOOKS_DIR / "claude/settings.hooks.json"]
        raw = json.dumps(claude)
        assert "$HOME/.agents/hooks/bb-hook" in raw
        assert str(Path.home()) not in raw


class TestCanonical:
    def test_spelling_variants_are_one_command(self):
        home = str(SYNC.HOOKS_DIR / "bb-hook")
        variants = [
            "$HOME/.agents/hooks/bb-hook",
            "${HOME}/.agents/hooks/bb-hook",
            "~/.agents/hooks/bb-hook",
            home,
        ]
        canonical = [SYNC.canonical_command(v) for v in variants]
        assert len(set(canonical)) == 1

    def test_non_home_tokens_survive(self):
        assert SYNC.canonical_command("echo $HOMEWORK") == "echo $HOMEWORK"
        assert SYNC.canonical_command("cd ~/code") == f"cd {Path.home()}/code"

    def test_marker_recognizes_all_spellings(self):
        markers = SYNC._publisher_markers("claude", MASTER["agents"]["claude"])
        for variant in ("$HOME/.agents/hooks/bb-hook", "~/.agents/hooks/bb-hook"):
            assert SYNC._has_marker(f"{variant} --cli claude --native Stop", markers)


class TestMergeConvergence:
    """The merge must converge: one managed row per event, content kept fresh."""

    def _entry(self, timeout: int, dollar_spelling: bool = False) -> dict:
        command = ("$HOME/.agents/hooks/bb-hook" if dollar_spelling
                   else str(SYNC._hooks_dir() / "bb-hook"))
        return {"type": "command",
                "command": f"{command} --cli claude --native SessionStart --deadline 15",
                "timeout": timeout}

    def test_merge_replaces_managed_hook_but_never_duplicates_it(self):
        # Old-spelling managed entry with a content diff (timeout 99): the
        # merge must end with EXACTLY ONE SessionStart hook carrying the NEW
        # content (timeout 16) - not the old row kept, not both rows.
        live = {"hooks": {"SessionStart": [{"hooks": [self._entry(99, dollar_spelling=True)]}]}}
        generated = SYNC.render_config(
            MASTER["agents"]["claude"], MASTER["lifecycle"], LOCK
        )["hooks"]
        merged = SYNC._merge_hooks(live, generated, SYNC._publisher_markers("claude", MASTER["agents"]["claude"]))
        entries = merged["hooks"]["SessionStart"]
        assert len(entries) == 1, entries
        assert entries[0]["hooks"][0]["timeout"] == 16

    def test_merge_is_idempotent_once_converged(self):
        generated = SYNC.render_config(
            MASTER["agents"]["claude"], MASTER["lifecycle"], LOCK
        )["hooks"]
        markers = SYNC._publisher_markers("claude", MASTER["agents"]["claude"])
        live = {"hooks": {"SessionStart": [{"hooks": [self._entry(99, dollar_spelling=True)]}]}}
        once = copy.deepcopy(SYNC._merge_hooks(live, generated, markers))
        twice = copy.deepcopy(SYNC._merge_hooks(once, generated, markers))
        assert once == twice


class TestHermesConvergence:
    def test_old_spelling_is_replaced_not_duplicated(self, tmp_path):
        old_cmd = "$HOME/.agents/hooks/bb-hook --cli hermes --native on_session_start --deadline 15"
        new_cmd = f"{SYNC._hooks_dir()}/bb-hook --cli hermes --native on_session_start --deadline 15"
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "hooks:\n  on_session_start:\n"
            f"    - command: {json.dumps(old_cmd)}\n      timeout: 16\n"
        )
        alw_path = tmp_path / "allowlist.json"
        changed, status = SYNC._install_hermes_one(
            "test", cfg_path, alw_path,
            {"on_session_start": [{"command": new_cmd, "timeout": 16}]},
            SYNC._publisher_markers("hermes", MASTER["agents"]["hermes"]),
        )
        import yaml

        block = yaml.safe_load(cfg_path.read_text())["hooks"]["on_session_start"]
        assert len(block) == 1, block
        assert changed == 2  # config.yaml rewritten + allowlist seeded
        approvals = json.loads(alw_path.read_text())["approvals"]
        assert {(a["event"], a["command"]) for a in approvals} == {
            ("on_session_start", new_cmd)
        }

        # Second run is a no-op (converged).
        changed2, _ = SYNC._install_hermes_one(
            "test", cfg_path, alw_path,
            {"on_session_start": [{"command": new_cmd, "timeout": 16}]},
            SYNC._publisher_markers("hermes", MASTER["agents"]["hermes"]),
        )
        assert changed2 == 0


class TestOverlay:
    """Host overlay: committed base + host-local TOML replacement/append."""

    def _load_hub(self, monkeypatch, tmp_path):
        sys.path.insert(0, str(AGENT_HOOKS_DIR))
        monkeypatch.syspath_prepend(str(HOOK_HUB_DIR))
        monkeypatch.setenv("HOOK_HUB_OVERLAY_DIR", str(tmp_path))
        for name in list(sys.modules):
            if name in {"hub", "receipts", "facts", "observations"}:
                del sys.modules[name]
        import hub

        return hub

    def test_no_overlay_is_base_only(self, tmp_path, monkeypatch):
        hub = self._load_hub(monkeypatch, tmp_path)
        assert hub._overlay_registry() == [hub.REGISTRY]

    def test_overlay_replaces_by_id_and_appends(self, tmp_path, monkeypatch):
        hub = self._load_hub(monkeypatch, tmp_path)
        (tmp_path / "hook-hub-overlay.toml").write_text(
            '[[handler]]\nid = "skill-reminder"\nenabled = false\n\n'
            '[[handler]]\nid = "host-only"\nmode = "async"\non = ["session_end"]\n'
            'command = ["echo", "hi"]\ntimeout_ms = 100\n'
        )
        cfg = hub.Config()
        cfg.maybe_reload()
        assert cfg.error is None
        by_id = {h.id: h for h in cfg.handlers}
        assert by_id["skill-reminder"].enabled is False
        assert by_id["skill-reminder"].timeout_ms == 1000  # inherited from base
        assert by_id["host-only"].command == ["echo", "hi"]
        assert cfg.registry_paths[-1].name == "hook-hub-overlay.toml"
