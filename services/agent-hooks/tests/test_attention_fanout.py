from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

AGENT_HOOKS_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_HOOKS_DIR))

from clients.base import ClientAdapter
from core.publisher import (
    DECKARD_ATTENTION_SUBJECT,
    DECKARD_ATTENTION_TYPE,
    DECKARD_PUBLISH_TIMEOUT,
    run,
)


def _load_sync_module():
    path = AGENT_HOOKS_DIR / "sync.py"
    spec = importlib.util.spec_from_file_location("bloodbank_sync_attention", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Bloodbank hook sync")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SYNC = _load_sync_module()


class _AlertAdapter(ClientAdapter):
    name = "test"
    source = "urn:test:agent"
    producer = "test-agent"
    service = "test-hooks"
    actor_base = {"type": "agent_cli", "cli": "test"}
    nats_client_name = "test-hooks"

    def __init__(self, agent_dir: Path) -> None:
        self._agent_dir = agent_dir

    @property
    def agent_dir(self) -> Path:
        return self._agent_dir

    def read_payload(self, argv: list[str]):
        del argv
        return {
            "message": "agent is waiting",
            "toolName": "Bash",
            "cwd": "/workspace",
        }


class AttentionFanoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.agent_dir = Path(self.temporary.name)
        (self.agent_dir / "event_map.generated.json").write_text(
            json.dumps({"map": {}, "alerts": {"nativeSignal": "attention"}})
        )
        self.adapter = _AlertAdapter(self.agent_dir)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_registry_declared_signal_emits_normalized_exact_alert(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    "BLOODBANK_ENABLED": "true",
                    "ZELLIJ_PANE_ID": "41",
                    "ZELLIJ_SESSION_NAME": "Workspace",
                },
                clear=False,
            ),
            mock.patch("core.publisher.nats_publish") as publish,
            mock.patch("sys.stdin", io.StringIO("")),
        ):
            self.assertEqual(run(self.adapter, ["publish.py", "nativeSignal"]), 0)

        publish.assert_called_once()
        subject, body = publish.call_args.args
        envelope = json.loads(body)
        self.assertEqual(subject, DECKARD_ATTENTION_SUBJECT)
        self.assertEqual(envelope["type"], DECKARD_ATTENTION_TYPE)
        self.assertEqual(envelope["data"]["alert_kind"], "attention")
        self.assertEqual(envelope["data"]["source_event"], "nativeSignal")
        self.assertEqual(envelope["data"]["zellij_pane_id"], 41)
        self.assertEqual(envelope["data"]["zellij_session_name"], "Workspace")
        self.assertEqual(envelope["data"]["message"], "agent is waiting")
        self.assertEqual(envelope["data"]["tool_name"], "Bash")
        self.assertEqual(publish.call_args.kwargs["timeout"], DECKARD_PUBLISH_TIMEOUT)

    def test_missing_exact_session_or_pane_refuses_without_publishing(self) -> None:
        for env in (
            {"ZELLIJ_PANE_ID": "41", "ZELLIJ_SESSION_NAME": ""},
            {"ZELLIJ_PANE_ID": "", "ZELLIJ_SESSION_NAME": "Workspace"},
            {"ZELLIJ_PANE_ID": "not-a-pane", "ZELLIJ_SESSION_NAME": "Workspace"},
        ):
            with self.subTest(env=env):
                with (
                    mock.patch.dict(
                        os.environ,
                        {"BLOODBANK_ENABLED": "true", **env},
                        clear=False,
                    ),
                    mock.patch("core.publisher.nats_publish") as publish,
                ):
                    self.assertEqual(
                        run(self.adapter, ["publish.py", "nativeSignal"]), 0
                    )
                publish.assert_not_called()

    def test_nats_failure_is_fail_open_and_bounded_by_the_alert_timeout(self) -> None:
        def unavailable(*args, **kwargs):
            del args
            self.assertEqual(kwargs["timeout"], DECKARD_PUBLISH_TIMEOUT)
            raise OSError("NATS unavailable")

        with (
            mock.patch.dict(
                os.environ,
                {
                    "BLOODBANK_ENABLED": "true",
                    "BLOODBANK_HOOK_STRICT": "1",
                    "ZELLIJ_PANE_ID": "41",
                    "ZELLIJ_SESSION_NAME": "Workspace",
                },
                clear=False,
            ),
            mock.patch("core.publisher.nats_publish", side_effect=unavailable),
        ):
            began = time.monotonic()
            result = run(self.adapter, ["publish.py", "nativeSignal"])
            elapsed = time.monotonic() - began

        self.assertEqual(result, 0, "strict mode may not make an alert fail closed")
        self.assertLess(elapsed, 3.0)

    def test_alert_bindings_generate_configs_without_replacing_foreign_hooks(self) -> None:
        master = SYNC.load_master()
        lock = SYNC.load_lock()
        claude = master["agents"]["claude"]
        generated = SYNC.render_config(claude, master["lifecycle"], lock)
        self.assertIsNotNone(generated)
        notification = generated["hooks"]["Notification"]

        foreign = {
            "hooks": {
                "Notification": [
                    {
                        "matcher": "foreign",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "foreign-notification-hook",
                                "timeout": 9,
                            }
                        ],
                    }
                ]
            }
        }
        merged = SYNC._merge_hooks(
            foreign,
            {"Notification": notification},
            ["bloodbank/publish.py"],
        )
        commands = [
            hook["command"]
            for group in merged["hooks"]["Notification"]
            for hook in group["hooks"]
        ]
        self.assertIn("foreign-notification-hook", commands)
        self.assertTrue(any("bloodbank/publish.py" in command for command in commands))

    def test_legacy_deckard_recognition_is_path_and_event_exact(self) -> None:
        canonical = (
            "/home/test/.local/share/deckard/hooks/"
            "deckard-attention-hook.sh Notification"
        )
        self.assertTrue(SYNC._is_legacy_deckard_attention(canonical, "Notification"))
        self.assertTrue(
            SYNC._is_legacy_deckard_attention(
                "'/tmp/data home/deckard/hooks/deckard-attention-hook.sh' "
                "PermissionRequest",
                "PermissionRequest",
            )
        )
        for command, event in (
            (canonical, "PermissionRequest"),
            (
                "/opt/foreign/deckard-attention-hook.sh Notification",
                "Notification",
            ),
            (canonical + " --foreign", "Notification"),
            ("echo deckard/hooks/deckard-attention-hook.sh Notification", "Notification"),
            ("'unterminated", "Notification"),
        ):
            with self.subTest(command=command, event=event):
                self.assertFalse(SYNC._is_legacy_deckard_attention(command, event))

    def test_install_replaces_only_legacy_attention_hooks_and_is_idempotent(self) -> None:
        master = SYNC.load_master()
        lock = SYNC.load_lock()
        claude = copy.deepcopy(master["agents"]["claude"])
        attention_events = SYNC._attention_replacement_events(claude)
        self.assertEqual(
            attention_events,
            {"Notification", "PermissionRequest", "TeammateIdle"},
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = root / "agent-hooks"
            generated_path = service / "claude" / "settings.hooks.json"
            generated_path.parent.mkdir(parents=True)
            live_path = root / "claude" / "settings.json"
            live_path.parent.mkdir(parents=True)

            claude["config_target"] = "claude/settings.hooks.json"
            claude["live_target"] = str(live_path)
            generated = SYNC.render_config(claude, master["lifecycle"], lock)
            self.assertIsNotNone(generated)
            generated_path.write_text(json.dumps(generated, indent=2) + "\n")

            legacy_script = (
                "/home/test/.local/share/deckard/hooks/"
                "deckard-attention-hook.sh"
            )
            live_hooks: dict[str, list[dict]] = {}
            foreign_commands: dict[str, list[str]] = {}
            for event in sorted(attention_events):
                foreign = [
                    f"foreign-{event}",
                    f"/opt/foreign/deckard-attention-hook.sh {event}",
                    f"{legacy_script} DifferentEvent",
                ]
                foreign_commands[event] = foreign
                live_hooks[event] = [
                    {
                        "matcher": "legacy-only",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{legacy_script} {event}",
                                "timeout": 3,
                            }
                        ],
                    },
                    {
                        "matcher": "mixed",
                        "condition": {"preserve": True},
                        "hooks": [
                            {"type": "command", "command": foreign[0]},
                            {
                                "type": "command",
                                "command": f"{legacy_script} {event}",
                                "timeout": 9,
                            },
                            {"type": "command", "command": foreign[1]},
                            {"type": "command", "command": foreign[2]},
                        ],
                    },
                ]

            # Even a command at the canonical path is foreign to this cutover
            # unless the registry says that native event is being replaced.
            live_hooks["Stop"] = [
                {
                    "matcher": "keep-stop",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{legacy_script} Stop",
                        },
                        {"type": "command", "command": "foreign-stop"},
                    ],
                }
            ]
            live = {
                "theme": "foreign-theme",
                "hooks": live_hooks,
                "foreign_top": {"preserve": [1, 2, 3]},
            }
            live_path.write_text(json.dumps(live, indent=2) + "\n")

            install_master = {"agents": {"claude": claude}}
            with (
                mock.patch.object(SYNC, "SERVICE_DIR", service),
                mock.patch.object(SYNC, "_ensure_bloodbank_hook_link", return_value=0),
            ):
                self.assertEqual(SYNC.cmd_install(install_master), 0)
                first_bytes = live_path.read_bytes()
                first_inode = live_path.stat().st_ino
                first_backups = sorted(live_path.parent.glob("settings.json.bak-*"))
                self.assertEqual(len(first_backups), 1)

                changed = json.loads(first_bytes)
                self.assertEqual(changed["theme"], live["theme"])
                self.assertEqual(changed["foreign_top"], live["foreign_top"])
                markers = SYNC._publisher_markers("claude", claude)
                for event in attention_events:
                    groups = changed["hooks"][event]
                    commands = [
                        hook.get("command", "")
                        for group in groups
                        for hook in group.get("hooks", [])
                    ]
                    self.assertFalse(
                        any(
                            SYNC._is_legacy_deckard_attention(command, event)
                            for command in commands
                        ),
                        f"legacy {event} publisher survived: {commands}",
                    )
                    self.assertEqual(
                        sum(SYNC._has_marker(command, markers) for command in commands),
                        1,
                        f"normalized {event} publisher was not unique: {commands}",
                    )
                    for foreign in foreign_commands[event]:
                        self.assertIn(foreign, commands)
                    mixed = next(group for group in groups if group.get("matcher") == "mixed")
                    self.assertEqual(mixed["condition"], {"preserve": True})

                stop_commands = [
                    hook["command"]
                    for group in changed["hooks"]["Stop"]
                    for hook in group.get("hooks", [])
                ]
                self.assertIn(f"{legacy_script} Stop", stop_commands)
                self.assertIn("foreign-stop", stop_commands)

                self.assertEqual(SYNC.cmd_install(install_master), 0)
                self.assertEqual(live_path.read_bytes(), first_bytes)
                self.assertEqual(live_path.stat().st_ino, first_inode)
                self.assertEqual(
                    sorted(live_path.parent.glob("settings.json.bak-*")),
                    first_backups,
                )

    def test_only_registry_declared_alerts_are_projected(self) -> None:
        master = SYNC.load_master()
        lock = SYNC.load_lock()
        expected = {
            "claude": {"Notification", "PermissionRequest", "TeammateIdle"},
            "codex": {"PermissionRequest"},
            "copilot": {"permissionRequest"},
        }
        for agent_name, native_events in expected.items():
            rendered = SYNC.render_event_map(
                master["agents"][agent_name], master["lifecycle"], lock
            )
            self.assertEqual(set(rendered["alerts"]), native_events)


if __name__ == "__main__":
    unittest.main()
