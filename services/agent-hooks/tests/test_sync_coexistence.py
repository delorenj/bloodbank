from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENT_HOOKS_DIR = Path(__file__).resolve().parents[1]
SYNC_PATH = AGENT_HOOKS_DIR / "sync.py"
PROJECT_NOTEBOOK_MASTER_PATH = (
    AGENT_HOOKS_DIR.parents[3]
    / "skillex"
    / "all-skills"
    / "project-notebook"
    / "hooks"
    / "hooks.master.json"
)
PROJECT_NOTEBOOK_ROOT = PROJECT_NOTEBOOK_MASTER_PATH.parents[1]
PROJECT_NOTEBOOK_FRAGMENT_PATH = PROJECT_NOTEBOOK_ROOT / "hooks" / "claude.settings.json"
PROJECT_NOTEBOOK_PROJECTOR_PATH = PROJECT_NOTEBOOK_ROOT / "scripts" / "project-hooks.py"
PROJECT_NOTEBOOK_PREFIX = "PJ_HOOK_OWNER=project-notebook.v1 "
PROJECT_NOTEBOOK_MASTER = json.loads(PROJECT_NOTEBOOK_MASTER_PATH.read_text(encoding="utf-8"))
PROJECT_NOTEBOOK_START = PROJECT_NOTEBOOK_MASTER["hooks"]["SessionStart"][0]["hooks"][0]
PROJECT_NOTEBOOK_END = PROJECT_NOTEBOOK_MASTER["hooks"]["SessionEnd"][0]["hooks"][0]


def _load_sync_module():
    spec = importlib.util.spec_from_file_location("bloodbank_sync_coexistence", SYNC_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Bloodbank hook sync")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SYNC = _load_sync_module()


def _project_notebook_objects(settings: dict) -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []
    for event, groups in settings.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                command = hook.get("command")
                if isinstance(command, str) and command.startswith(PROJECT_NOTEBOOK_PREFIX):
                    found.append((event, copy.deepcopy(hook)))
    return found


def _command_order(settings: dict, event: str) -> list[str]:
    return [
        hook.get("command", "")
        for group in settings.get("hooks", {}).get(event, [])
        for hook in group.get("hooks", [])
    ]


def _foreign_projection(settings: dict) -> list[tuple[str, dict, list[dict]]]:
    projection: list[tuple[str, dict, list[dict]]] = []
    for event, groups in settings.get("hooks", {}).items():
        for group in groups:
            foreign_hooks = []
            for hook in group.get("hooks", []):
                command = hook.get("command")
                if not (
                    isinstance(command, str)
                    and command.startswith(PROJECT_NOTEBOOK_PREFIX)
                ):
                    foreign_hooks.append(copy.deepcopy(hook))
            if foreign_hooks:
                extras = {key: copy.deepcopy(value) for key, value in group.items() if key != "hooks"}
                projection.append((event, extras, foreign_hooks))
    return projection


class BloodbankProjectNotebookCoexistenceTests(unittest.TestCase):
    def test_changing_sync_preserves_project_notebook_and_second_sync_is_bytes_identical(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = root / "agent-hooks"
            generated_path = service / "claude" / "settings.hooks.json"
            generated_path.parent.mkdir(parents=True)
            live_path = root / "claude" / "settings.json"
            live_path.parent.mkdir(parents=True)

            old_bloodbank = {
                "type": "command",
                "command": "python3 /old/bloodbank/claude/publish.py SessionStart",
                "timeout": 2,
            }
            new_bloodbank = {
                "type": "command",
                "command": "python3 /canonical/bloodbank/claude/publish.py SessionStart",
                "timeout": 5,
            }
            foreign_before = {
                "type": "command",
                "command": "foreign-before",
                "timeout": 4,
                "condition": {"mode": "interactive"},
            }
            foreign_after = {
                "type": "command",
                "command": "foreign-after",
                "extra": {"preserve": True},
            }
            stop_group = {
                "matcher": "foreign-stop",
                "hooks": [{"type": "command", "command": "foreign-stop"}],
            }
            live = {
                "theme": "dark",
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "head",
                            "hooks": [{"type": "command", "command": "foreign-head"}],
                        },
                        {
                            "matcher": "mixed",
                            "condition": {"project": "any"},
                            "hooks": [
                                foreign_before,
                                copy.deepcopy(PROJECT_NOTEBOOK_START),
                                old_bloodbank,
                                foreign_after,
                            ],
                            "group_extra": "preserve",
                        },
                        {
                            "matcher": "tail",
                            "hooks": [{"type": "command", "command": "foreign-tail"}],
                        },
                    ],
                    "SessionEnd": [
                        {
                            "matcher": "end",
                            "hooks": [
                                {"type": "command", "command": "foreign-end-before"},
                                copy.deepcopy(PROJECT_NOTEBOOK_END),
                                {"type": "command", "command": "foreign-end-after"},
                            ],
                        }
                    ],
                    "Stop": [stop_group],
                },
                "foreign_top": {"ordered": ["a", "b"]},
            }
            live_path.write_text(json.dumps(live, indent=4) + "\n", encoding="utf-8")
            generated_path.write_text(
                json.dumps(
                    {"hooks": {"SessionStart": [{"hooks": [new_bloodbank]}]}},
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            master = {
                "agents": {
                    "claude": {
                        "dialect": "claude_settings",
                        "publisher": "claude/publish.py",
                        "config_target": "claude/settings.hooks.json",
                        "live_target": str(live_path),
                    }
                }
            }

            notebook_before = _project_notebook_objects(live)
            start_order_before = _command_order(live, "SessionStart")
            end_order_before = _command_order(live, "SessionEnd")
            stop_before = copy.deepcopy(live["hooks"]["Stop"])

            with (
                mock.patch.object(SYNC, "SERVICE_DIR", service),
                mock.patch.object(SYNC, "_ensure_bloodbank_hook_link", return_value=0),
            ):
                self.assertEqual(SYNC.cmd_install(master), 0)
                first_bytes = live_path.read_bytes()
                first_inode = live_path.stat().st_ino
                backups_after_first = sorted(live_path.parent.glob("settings.json.bak-*"))

                changed = json.loads(first_bytes)
                self.assertEqual(_project_notebook_objects(changed), notebook_before)
                self.assertEqual(changed["theme"], live["theme"])
                self.assertEqual(changed["foreign_top"], live["foreign_top"])
                self.assertEqual(changed["hooks"]["Stop"], stop_before)
                self.assertEqual(
                    [group.get("matcher") for group in changed["hooks"]["SessionStart"]],
                    ["head", "mixed", "tail"],
                )
                self.assertEqual(
                    changed["hooks"]["SessionStart"][1]["condition"],
                    live["hooks"]["SessionStart"][1]["condition"],
                )
                self.assertEqual(
                    changed["hooks"]["SessionStart"][1]["group_extra"], "preserve"
                )
                self.assertEqual(
                    _command_order(changed, "SessionStart"),
                    [
                        command
                        if command != old_bloodbank["command"]
                        else new_bloodbank["command"]
                        for command in start_order_before
                    ],
                )
                self.assertEqual(_command_order(changed, "SessionEnd"), end_order_before)
                self.assertEqual(
                    sum(
                        1
                        for _, hook in _project_notebook_objects(changed)
                        if hook["command"] == PROJECT_NOTEBOOK_START["command"]
                    ),
                    1,
                )
                self.assertEqual(
                    sum(
                        1
                        for _, hook in _project_notebook_objects(changed)
                        if hook["command"] == PROJECT_NOTEBOOK_END["command"]
                    ),
                    1,
                )

                self.assertEqual(SYNC.cmd_install(master), 0)
                self.assertEqual(live_path.read_bytes(), first_bytes)
                self.assertEqual(live_path.stat().st_ino, first_inode)
                self.assertEqual(
                    sorted(live_path.parent.glob("settings.json.bak-*")),
                    backups_after_first,
                )

                reinstall_seed = json.loads(first_bytes)
                for group in reinstall_seed["hooks"]["SessionStart"]:
                    for hook in group.get("hooks", []):
                        if hook.get("command") == PROJECT_NOTEBOOK_START["command"]:
                            hook["timeout"] = 99
                reinstall_seed["hooks"]["SessionStart"].append(
                    {"hooks": [copy.deepcopy(PROJECT_NOTEBOOK_START)]}
                )
                live_path.write_text(
                    json.dumps(reinstall_seed, indent=2) + "\n", encoding="utf-8"
                )
                foreign_before_projector = _foreign_projection(reinstall_seed)
                projector_state = root / "project-notebook-state"
                projector_command = [
                    sys.executable,
                    "-B",
                    str(PROJECT_NOTEBOOK_PROJECTOR_PATH),
                    "install",
                    "--master",
                    str(PROJECT_NOTEBOOK_MASTER_PATH),
                    "--fragment",
                    str(PROJECT_NOTEBOOK_FRAGMENT_PATH),
                    "--target",
                    str(live_path),
                    "--state-home",
                    str(projector_state),
                ]
                environment = os.environ.copy()
                environment["PYTHONDONTWRITEBYTECODE"] = "1"

                reinstall = subprocess.run(
                    projector_command,
                    text=True,
                    capture_output=True,
                    check=False,
                    env=environment,
                )
                self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
                reinstalled = json.loads(live_path.read_text())
                self.assertEqual(_foreign_projection(reinstalled), foreign_before_projector)
                self.assertEqual(
                    _project_notebook_objects(reinstalled),
                    [
                        ("SessionStart", copy.deepcopy(PROJECT_NOTEBOOK_START)),
                        ("SessionEnd", copy.deepcopy(PROJECT_NOTEBOOK_END)),
                    ],
                )
                reinstall_bytes = live_path.read_bytes()
                reinstall_inode = live_path.stat().st_ino
                reinstall_second = subprocess.run(
                    projector_command,
                    text=True,
                    capture_output=True,
                    check=False,
                    env=environment,
                )
                self.assertEqual(reinstall_second.returncode, 0, reinstall_second.stderr)
                self.assertEqual(live_path.read_bytes(), reinstall_bytes)
                self.assertEqual(live_path.stat().st_ino, reinstall_inode)

                uninstall_command = projector_command.copy()
                uninstall_command[3] = "uninstall"
                uninstall = subprocess.run(
                    uninstall_command,
                    text=True,
                    capture_output=True,
                    check=False,
                    env=environment,
                )
                self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
                uninstalled = json.loads(live_path.read_text())
                self.assertEqual(_project_notebook_objects(uninstalled), [])
                self.assertEqual(_foreign_projection(uninstalled), foreign_before_projector)
                uninstall_bytes = live_path.read_bytes()
                uninstall_inode = live_path.stat().st_ino
                uninstall_second = subprocess.run(
                    uninstall_command,
                    text=True,
                    capture_output=True,
                    check=False,
                    env=environment,
                )
                self.assertEqual(uninstall_second.returncode, 0, uninstall_second.stderr)
                self.assertEqual(live_path.read_bytes(), uninstall_bytes)
                self.assertEqual(live_path.stat().st_ino, uninstall_inode)


if __name__ == "__main__":
    unittest.main()
