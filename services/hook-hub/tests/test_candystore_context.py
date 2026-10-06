from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import concerns
from hub import Config


class CandyStoreContextTests(unittest.TestCase):
    def test_startup_excludes_native_session_and_returns_each_native_dialect(self):
        for cli, native in (("claude", "SessionStart"), ("codex", "SessionStart"),
                            ("gemini", "SessionStart"), ("kimi", "UserPromptSubmit"),
                            ("copilot", "sessionStart"), ("hermes", "pre_llm_call"),
                            ("opencode", "session.created"), ("antigravity", "PreInvocation")):
            with self.subTest(cli=cli), tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {
                "BB_HOOK_CLI": cli, "BB_HOOK_NATIVE": native, "CANDYSTORE_CONTEXT": "1",
                "XDG_STATE_HOME": state,
            }), mock.patch.object(concerns, "disabled", return_value=False), \
                 mock.patch.object(concerns, "invoke", return_value=concerns.result(
                     "succeeded", "handler_completed", "A prior session's handoff"
                 )) as invoke:
                output = concerns.dispatch("candystore-context", {
                    "sessionId": "current-native", "cwd": "/code/project",
                    "extra": {"is_first_turn": True},
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
                elif cli == "copilot":
                    self.assertIn("handoff", json.loads(stdout)["additionalContext"])
                elif cli == "hermes":
                    self.assertIn("handoff", json.loads(stdout)["context"])
                elif cli == "antigravity":
                    self.assertIn("handoff", json.loads(stdout)["injectSteps"][0]["ephemeralMessage"])
                else:
                    self.assertIn("handoff", stdout)

    def test_hermes_only_retrieves_on_first_model_bound_turn(self):
        payloads = [
            ("on_session_start", {"extra": {"is_first_turn": True}}),
            ("pre_llm_call", {"extra": {"is_first_turn": False}}),
            ("pre_llm_call", {}),
            ("pre_llm_call", {"extra": {"is_first_turn": "false"}}),
        ]
        with mock.patch.object(concerns, "disabled", return_value=False), \
             mock.patch.object(concerns, "invoke") as invoke:
            for native, payload in payloads:
                with self.subTest(native=native, payload=payload), mock.patch.dict(os.environ, {
                    "BB_HOOK_CLI": "hermes", "BB_HOOK_NATIVE": native,
                    "CANDYSTORE_CONTEXT": "1",
                }):
                    output = concerns.dispatch("candystore-context", payload)
                    self.assertEqual(output["_hook_hub"]["reason"], "not_first_turn")
                    self.assertEqual(output["stdout"], "")
            invoke.assert_not_called()

    def test_registry_covers_every_supported_cli_context_boundary(self):
        config = Config()
        with mock.patch("hub.log"):
            config.maybe_reload()
        self.assertIsNone(config.error)
        covered = set()
        for (cli, native), binding in config.bindings.items():
            if binding["support_status"] != "supported":
                continue
            if (cli, native) == ("kimi", "SessionStart"):
                self.assertNotIn("candystore-context", [handler.id for handler in config.select(
                    binding["role"], native, {}, {}, cli=cli,
                )])
                continue
            if binding["role"] != "session_start" and (cli, native) not in {
                ("hermes", "pre_llm_call"), ("antigravity", "PreInvocation"),
                ("kimi", "UserPromptSubmit"),
            }:
                continue
            handlers = config.select(binding["role"], native, {}, {}, cli=cli)
            expected = "candystore-context-kimi" if cli == "kimi" else "candystore-context"
            self.assertIn(expected, [handler.id for handler in handlers], (cli, native))
            covered.add(cli)
        self.assertEqual(covered, {"claude", "codex", "gemini", "kimi", "copilot",
                                   "hermes", "opencode", "antigravity"})

    def test_handler_executes_candystore_in_the_project_and_excludes_current_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            binary = home / ".local/bin/candystore"
            binary.parent.mkdir(parents=True)
            binary.write_text(
                f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
                f"Path({str(home / 'command.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
                "print('Recent project work')\n"
            )
            binary.chmod(0o700)
            process = subprocess.run(
                [sys.executable, str(Path(concerns.__file__)), "candystore-context"],
                input=json.dumps({"session_id": "current", "cwd": temporary,
                                  "extra": {"is_first_turn": True}}),
                env={**os.environ, "HOME": temporary, "BB_HOOK_CLI": "hermes",
                     "BB_HOOK_NATIVE": "pre_llm_call", "CANDYSTORE_CONTEXT": "1"},
                cwd=temporary, capture_output=True, text=True, check=True, timeout=5,
            )
            output = json.loads(process.stdout)
            self.assertEqual(output["_hook_hub"]["status"], "succeeded")
            self.assertEqual(json.loads(output["stdout"]), {"context": "Recent project work\n"})
            self.assertEqual(json.loads((home / "command.json").read_text()),
                             ["context", "latest", "--cwd", temporary, "--timeout", "4",
                              "--exclude-session", "current"])

    def test_kimi_retrieves_once_per_session_across_concurrent_handler_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            binary = home / ".local/bin/candystore"
            binary.parent.mkdir(parents=True)
            binary.write_text(f"#!{sys.executable}\nprint('Recent project work')\n")
            binary.chmod(0o700)

            def dispatch(session, native="UserPromptSubmit", enabled="1"):
                process = subprocess.run(
                    [sys.executable, str(Path(concerns.__file__)), "candystore-context"],
                    input=json.dumps({"session_id": session, "cwd": temporary}),
                    env={**os.environ, "HOME": temporary, "XDG_STATE_HOME": temporary,
                         "BB_HOOK_CLI": "kimi", "BB_HOOK_NATIVE": native,
                         "CANDYSTORE_CONTEXT": enabled},
                    cwd=temporary, capture_output=True, text=True, check=True, timeout=5,
                )
                return json.loads(process.stdout)

            self.assertEqual(dispatch("one", "SessionStart")["_hook_hub"]["status"], "skipped")
            self.assertEqual(dispatch("")["_hook_hub"]["reason"], "session_identity_missing")
            self.assertEqual(dispatch("one", enabled="0")["_hook_hub"]["reason"], "context_disabled")
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                outputs = list(pool.map(dispatch, ["one"] * 4))
            self.assertEqual(sum(output["stdout"] == "Recent project work\n" for output in outputs), 1)
            self.assertEqual(sum(output["_hook_hub"]["reason"] == "context_already_requested"
                                 for output in outputs), 3)
            self.assertEqual(dispatch("one")["stdout"], "")
            self.assertEqual(dispatch("two")["stdout"], "Recent project work\n")

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
