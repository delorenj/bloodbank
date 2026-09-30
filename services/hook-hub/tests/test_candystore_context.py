from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import concerns


class CandyStoreContextTests(unittest.TestCase):
    def test_startup_excludes_native_session_and_returns_each_native_dialect(self):
        for cli, native in (("claude", "SessionStart"), ("codex", "SessionStart"),
                            ("gemini", "SessionStart"), ("kimi", "SessionStart"),
                            ("copilot", "sessionStart"), ("hermes", "on_session_start"),
                            ("opencode", "session.created"), ("antigravity", "PreInvocation")):
            with self.subTest(cli=cli), mock.patch.dict(os.environ, {
                "BB_HOOK_CLI": cli, "BB_HOOK_NATIVE": native, "CANDYSTORE_CONTEXT": "1",
            }), mock.patch.object(concerns, "disabled", return_value=False), \
                 mock.patch.object(concerns, "invoke", return_value=concerns.result(
                     "succeeded", "handler_completed", "A prior session's handoff"
                 )) as invoke:
                output = concerns.dispatch("candystore-context", {
                    "sessionId": "current-native", "cwd": "/code/project",
                })
                command = invoke.call_args[0][0]
                self.assertEqual(command[1:3], ["context", "latest"])
                self.assertEqual(command[command.index("--cwd") + 1], "/code/project")
                self.assertEqual(command[command.index("--exclude-session") + 1], "current-native")
                stdout = output["stdout"]
                if cli in {"claude", "codex", "gemini"}:
                    envelope = json.loads(stdout)["hookSpecificOutput"]
                    self.assertEqual(envelope["hookEventName"], native)
                    self.assertIn("handoff", envelope["additionalContext"])
                elif cli == "hermes":
                    self.assertIn("handoff", json.loads(stdout)["context"])
                elif cli == "antigravity":
                    self.assertIn("handoff", json.loads(stdout)["injectSteps"][0]["ephemeralMessage"])
                else:
                    self.assertIn("handoff", stdout)

    def test_failure_never_injects_diagnostics_or_blocks_the_cli(self):
        with mock.patch.object(concerns, "disabled", return_value=False), \
             mock.patch.object(concerns, "invoke", return_value=concerns.result(
                 "failed", "handler_deadline_exceeded", "Partial output", exit_code=1
             )):
            output = concerns.dispatch("candystore-context", {"cwd": "/code/project"})
        self.assertEqual(output["stdout"], "")
        self.assertEqual(output["_hook_hub"]["status"], "failed")

    def test_opt_out_and_antigravity_only_first_invocation(self):
        with mock.patch.object(concerns, "disabled", return_value=False), \
             mock.patch.object(concerns, "invoke") as invoke:
            with mock.patch.dict(os.environ, {"CANDYSTORE_CONTEXT": "0"}):
                output = concerns.dispatch("candystore-context", {})
                self.assertEqual(output["_hook_hub"]["reason"], "context_disabled")
            with mock.patch.dict(os.environ, {
                "CANDYSTORE_CONTEXT": "1", "BB_HOOK_CLI": "antigravity",
            }):
                output = concerns.dispatch("candystore-context", {"invocationNum": 2})
                self.assertEqual(output["_hook_hub"]["reason"], "not_first_invocation")
            invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
