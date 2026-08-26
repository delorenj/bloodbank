from __future__ import annotations

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
