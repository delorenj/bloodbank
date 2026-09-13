from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))
import concerns
import cutover
import hindsight
import ownership


class ConcernTests(unittest.TestCase):
    def test_normalizes_native_payload_without_dropping_fields(self):
        raw = {"sessionId": "session-a", "toolName": "write_file", "toolArgs": '{"path":"a.py","content":"hello"}', "userMessage": "A prompt", "custom": "preserved"}
        data = concerns.normalized(raw)
        self.assertEqual(data["session_id"], "session-a")
        self.assertEqual(data["tool_input"]["path"], "a.py")
        self.assertEqual(data["prompt"], "A prompt")
        self.assertEqual(data["custom"], "preserved")

    def test_apply_patch_extracts_each_edited_file(self):
        data = {"tool_input": {"patch": "*** Begin Patch\n*** Update File: a.py\n+first\n*** Add File: b.py\n+second\n*** End Patch"}}
        self.assertEqual(concerns.file_edits(data), [("a.py", "first"), ("b.py", "second")])

    def test_context_uses_native_dialect(self):
        for cli in ("claude", "codex", "gemini"):
            response = json.loads(concerns.context_output("useful memory", cli, "UserPromptSubmit"))
            self.assertEqual(response["hookSpecificOutput"]["additionalContext"], "useful memory")
        response = json.loads(concerns.context_output("skill reminder", "antigravity", "PreInvocation"))
        self.assertEqual(response["injectSteps"][0]["ephemeralMessage"], "skill reminder")
        self.assertEqual(concerns.context_output("context", "kimi", "UserPromptSubmit"), "context")

    def test_orca_without_pane_is_an_explicit_skip(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            response = concerns.orca({}, "claude", "SessionStart")
        self.assertEqual(response["_hook_hub"]["reason"], "not_an_orca_pane")

    def test_notebook_preserves_supported_cli_and_project_scope(self):
        with mock.patch.object(concerns, "repository", return_value=None), mock.patch.object(concerns, "invoke") as invoke:
            for name in ("project-notebook-start", "project-notebook-end"):
                with mock.patch.dict(os.environ, {"BB_HOOK_CLI": "codex"}):
                    output = concerns.dispatch(name, {})
                    self.assertEqual(output["_hook_hub"]["reason"], "notebook_cli_unsupported")
                with mock.patch.dict(os.environ, {"BB_HOOK_CLI": "claude"}):
                    output = concerns.dispatch(name, {})
                    self.assertEqual(output["_hook_hub"]["reason"], "not_a_git_repository")
            invoke.assert_not_called()

    def test_project_disabled_concern_is_not_invoked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".agents").mkdir()
            (root / ".agents/local.json").write_text('{"hooks":{"disabled":["skill-check-reminder"]}}')
            with mock.patch.object(concerns, "repository", return_value=root), mock.patch.object(concerns, "invoke") as invoke:
                response = concerns.dispatch("skill-reminder", {})
            invoke.assert_not_called()
            self.assertEqual(response["_hook_hub"]["reason"], "project_disabled")


class MemoryReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fake = self.root / "hindsight"
        self.calls = self.root / "calls.jsonl"
        self.fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\nwith Path(os.environ['CALL_LOG']).open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\nif os.environ.get('FAKE_RETAIN_FAIL')=='1' and 'retain' in sys.argv: raise SystemExit(1)\nprint(json.dumps({'results':[{'text':'A useful recalled fact'}]} if 'recall' in sys.argv else {'success':True,'document_id':'accepted'}))\n")
        self.fake.chmod(0o700)
        self.environment = mock.patch.dict(os.environ, {"HINDSIGHT_BIN": str(self.fake), "HINDSIGHT_BANK": "test-bank", "HS_JOURNAL_DIR": str(self.root / "journal"), "CALL_LOG": str(self.calls), "HINDSIGHT_FANOUT": "0", "HINDSIGHT_GLOBAL_BANKS": "", "BB_HOOK_INVOCATION_ID": "test-invocation"}, clear=False)
        self.environment.start()
        self.bank = mock.patch.object(hindsight, "bank", return_value="test-bank")
        self.bank.start()

    def tearDown(self):
        self.bank.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def payload(self):
        return {"session_id": "test-session", "tool_input": {"patch": "*** Begin Patch\n*** Update File: README.md\n+" + "substantial edit " * 8 + "\n*** End Patch"}}

    def test_candidate_is_not_a_fake_retain_and_session_retry_deduplicates(self):
        payload = self.payload()
        candidate = hindsight.candidate(payload, "codex")
        self.assertEqual(candidate["_hook_hub"]["reason"], "edit_candidate_recorded")
        self.assertFalse(self.calls.exists())
        self.assertEqual(hindsight.records(payload, "codex")[-1]["event"], "retain_candidate")
        first = hindsight.end(payload, "codex")
        second = hindsight.end(payload, "codex")
        self.assertEqual(first["_hook_hub"]["reason"], "session_summary_retained")
        self.assertEqual(second["_hook_hub"]["reason"], "session_summary_already_retained")
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        record = hindsight.records(payload, "codex")[-1]
        self.assertTrue(record["retained"])
        self.assertEqual(record["response_keys"], ["document_id", "success"])

    def test_failed_retain_can_retry_same_document_id(self):
        payload = self.payload()
        hindsight.candidate(payload, "codex")
        with mock.patch.dict(os.environ, {"FAKE_RETAIN_FAIL": "1"}):
            first = hindsight.end(payload, "codex")
        second = hindsight.end(payload, "codex")
        self.assertEqual(first["_hook_hub"]["status"], "failed")
        self.assertEqual(second["_hook_hub"]["status"], "succeeded")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(calls[0][calls[0].index("--doc-id") + 1], calls[1][calls[1].index("--doc-id") + 1])

    def test_sessions_are_isolated_by_cli(self):
        payload = self.payload()
        hindsight.candidate(payload, "codex")
        self.assertEqual(hindsight.end(payload, "claude")["_hook_hub"]["reason"], "no_retention_candidates")
        self.assertFalse(self.calls.exists())

    def test_failed_tool_does_not_record_an_edit_candidate(self):
        payload = {**self.payload(), "tool_response": {"success": False}}
        self.assertEqual(hindsight.candidate(payload, "codex")["_hook_hub"]["reason"], "tool_failed")
        self.assertFalse(hindsight.journal_path(payload, "codex").exists())

    def test_recall_returns_native_context_from_json_response(self):
        output = hindsight.recall({"session_id": "recall", "prompt": "Please explain the current project hook architecture"}, "codex", "UserPromptSubmit")
        self.assertEqual(output["_hook_hub"]["status"], "succeeded")
        self.assertIn("A useful recalled fact", json.loads(output["stdout"])["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)


class CutoverTests(unittest.TestCase):
    def test_removes_inner_owned_command_preserves_foreign_matcher(self):
        foreign = {"type": "command", "command": "my-private-hook", "timeout": 8}
        data = {"hooks": {"PostToolUse": [{"matcher": "Write|Edit", "hooks": [foreign, {"command": "/home/me/.agents/hooks/hindsight/hindsight-retain.sh"}]}]}}
        pruned, removed = cutover.prune(data)
        self.assertEqual(removed, 1)
        self.assertEqual(pruned["hooks"]["PostToolUse"], [{"matcher": "Write|Edit", "hooks": [foreign]}])
        self.assertEqual(cutover.prune(pruned), (pruned, 0))

    def test_keeps_project_fallback_and_new_hub_command(self):
        self.assertFalse(cutover.owned("python .agents/hooks/hindsight/hook.py recall"))
        self.assertFalse(cutover.owned("~/.agents/hooks/bb-hook --cli codex --native Stop"))
        self.assertTrue(cutover.owned(["bash", "-c", "HINDSIGHT_OUTPUT_FORMAT=gemini_json exec /home/me/.agents/hooks/hindsight/hindsight-recall.sh"]))

    def test_kimi_removal_preserves_following_sections(self):
        original = 'model="test"\n[[hooks]]\nevent="Stop"\ncommand="/home/me/.agents/hooks/hindsight/hindsight-session-end.sh"\n[[hooks]]\nevent="Stop"\ncommand="custom-hook"\n[foreign]\nvalue=true\n'
        updated, removed = cutover.strip_kimi(original)
        self.assertEqual(removed, 1)
        parsed = __import__("tomllib").loads(updated)
        self.assertEqual(parsed["hooks"], [{"event": "Stop", "command": "custom-hook"}])
        self.assertTrue(parsed["foreign"]["value"])

    def test_notify_retirement_preserves_foreign_notify(self):
        known = 'notify = ["bash", "-c", "/home/me/.agents/hooks/hindsight/hindsight-session-end.sh; claude-notify"]\n[features]\nhooks = true\n'
        updated, removed = cutover.strip_notify(known)
        self.assertEqual(removed, 1)
        self.assertIn("[features]", updated)
        foreign = 'notify = ["custom-notify", "argument"]\n'
        self.assertEqual(cutover.strip_notify(foreign), (foreign, 0))

    def test_ownership_requires_activation_and_survives_handler_pause(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "ownership.json"
            registry = root / "handlers.toml"
            registry.write_text('[[handler]]\nid="hindsight-recall"\nenabled=true\n')
            env = {"BB_HOOK_OWNERSHIP": str(manifest), "BB_HOOK_HUB": ""}
            with mock.patch.dict(os.environ, env):
                self.assertFalse(ownership.owns("hindsight-recall", "codex"))
                manifest.write_text(json.dumps({"version": 1, "registry": str(registry), "handler_ids": ["hindsight-recall"], "clis": ["codex"]}))
                self.assertTrue(ownership.owns("hindsight-recall", "codex"))
                self.assertFalse(ownership.owns("hindsight-recall", "claude"))
                registry.write_text('[[handler]]\nid="hindsight-recall"\nenabled=false\n')
                self.assertTrue(ownership.owns("hindsight-recall", "codex"))
                registry.write_text('[[handler]]\nid="another-concern"\n')
                self.assertFalse(ownership.owns("hindsight-recall", "codex"))


if __name__ == "__main__":
    unittest.main()
