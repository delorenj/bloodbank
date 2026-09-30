from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import automaticai


class AutomaticAIHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = mock.patch.object(automaticai, "STATE_ROOT", self.root / "proof")
        self.state.start()
        self.env = mock.patch.dict(os.environ, {"BB_HOOK_CLI": "codex", "BB_HOOK_NATIVE": "SessionStart",
                                               "AUTOMATICAI_PROOF_PROBE": "0", "AUTOMATICAI_MIGRATION_WORKER": "0"})
        self.env.start()
        self.payload = {"session_id": "test-native-session", "cwd": str(self.root)}
        self.snapshot = {"provider": "automaticai", "model": "automaticai/personal/sol",
                         "status": "gateway_configured", "authority": "launch_arguments",
                         "source_owner": str(automaticai.OWNER)}

    def tearDown(self):
        self.env.stop()
        self.state.stop()
        self.temporary.cleanup()

    def load(self):
        return json.loads((automaticai.STATE_ROOT / "test-native-session.json").read_text())

    def test_startup_challenges_future_request_and_never_claims_verified(self):
        with mock.patch.object(automaticai, "audit", return_value=self.snapshot), \
             mock.patch.object(automaticai, "completed_receipt") as receipt:
            output = automaticai.dispatch("start", self.payload)
        state = self.load()
        self.assertEqual(state["status"], "pending")
        self.assertRegex(state["marker"], r"^AAI_ROUTE_PROOF_[0-9a-f]{32}$")
        context = json.loads(output["stdout"])["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "SessionStart")
        self.assertIn(state["marker"], context["additionalContext"])
        self.assertIn("completed gateway usage", context["additionalContext"])
        receipt.assert_not_called()

    def test_bypass_requests_real_skill_once_and_preserves_blocker(self):
        snapshot = dict(self.snapshot, provider="openai", model="gpt-6.1-sol", status="bypass", authority="session_record")
        with mock.patch.object(automaticai, "audit", return_value=snapshot):
            first = automaticai.dispatch("start", self.payload)
            second = automaticai.dispatch("verify", self.payload)
        self.assertIn("Immediately fork", first["stdout"])
        self.assertIn(str(automaticai.SKILL), first["stdout"])
        self.assertIn("open blocker", first["stdout"])
        self.assertEqual(second["stdout"], "")
        self.assertEqual(self.load()["status"], "bypass")
        self.assertTrue(self.load()["migration_requested"])
        self.assertNotIn("worker_spawned", self.load())

    def test_missing_receipt_is_pending_and_never_triggers_migration(self):
        with mock.patch.object(automaticai, "audit", return_value=self.snapshot), \
             mock.patch.object(automaticai, "completed_receipt", return_value={"verified": False, "status": "unavailable"}):
            automaticai.dispatch("start", self.payload)
            output = automaticai.dispatch("verify", self.payload)
        self.assertEqual(output["stdout"], "")
        self.assertEqual(self.load()["status"], "pending")
        self.assertFalse(self.load()["migration_requested"])

    def test_completed_actual_receipt_verifies_once_and_next_prompt_resets(self):
        row = {"log_id": 123, "request_id": "actual-request", "route": self.snapshot["model"], "account": "openai-personal"}
        with mock.patch.object(automaticai, "audit", return_value=self.snapshot), \
             mock.patch.object(automaticai, "completed_receipt", return_value={"verified": True, "status": "verified", "receipts": [row]}) as receipt:
            automaticai.dispatch("start", self.payload)
            original = self.load()["marker"]
            output = automaticai.dispatch("verify", self.payload)
            self.assertEqual(self.load()["status"], "verified")
            self.assertIn("actual-request", output["stdout"])
            automaticai.dispatch("verify", self.payload)
            self.assertEqual(receipt.call_count, 1)
            automaticai.dispatch("start", dict(self.payload, prompt="private user request"))
            self.assertEqual(self.load()["status"], "pending")
            self.assertNotEqual(original, self.load()["marker"])
            self.assertNotIn("private user request", json.dumps(self.load()))

    def test_probe_worker_and_missing_session_do_not_recurse(self):
        with mock.patch.dict(os.environ, {"AUTOMATICAI_PROOF_PROBE": "1"}):
            output = automaticai.dispatch("start", self.payload)
        self.assertEqual(output["_hook_hub"]["reason"], "separate_proof_probe")
        self.assertFalse(automaticai.STATE_ROOT.exists())
        with mock.patch.dict(os.environ, {"AUTOMATICAI_MIGRATION_WORKER": "1"}), \
             mock.patch.object(automaticai, "audit", return_value=dict(self.snapshot, status="bypass")):
            output = automaticai.dispatch("start", self.payload)
        self.assertNotIn("Immediately fork", output["stdout"])
        self.assertEqual(automaticai.dispatch("start", {"session_id": "../escape"})["_hook_hub"]["reason"], "native_session_identity_missing")

    def test_launch_profile_and_overrides_win_over_native_global(self):
        (self.root / "config.toml").write_text('model_provider="openai"\nmodel="gpt-6.1-sol"\n')
        (self.root / "automaticai.config.toml").write_text('model_provider="automaticai"\nmodel="automaticai/personal/sol"\n[model_providers.automaticai]\nbase_url="https://api.automaticai.io/v1"\n')
        config = automaticai.routing_config(self.root, ["--profile", "automaticai", "-c", 'model="aai/personal/kimi-k3"'])
        self.assertEqual(config["model_provider"], "automaticai")
        self.assertEqual(config["model"], "aai/personal/kimi-k3")
        self.assertTrue(automaticai.gateway_origin(config["model_providers"]["automaticai"]["base_url"]))
        for url in ("http://api.automaticai.io/v1", "https://api.automaticai.io.attacker.test/v1",
                    "https://api.automaticai.io/v1?key=value", "https://user:password@api.automaticai.io/v1"):
            self.assertFalse(automaticai.gateway_origin(url))

    def test_actual_session_record_wins_over_changed_next_launch_configuration(self):
        meta = self.root / "rollout.jsonl"
        meta.write_text(json.dumps({"type": "session_meta", "payload": {"id": "test-native-session", "model_provider": "openai"}}) + "\n")
        data = dict(self.payload, transcript_path=str(meta))
        config = {"model_provider": "automaticai", "model": "automaticai/personal/sol",
                  "model_providers": {"automaticai": {"base_url": "https://api.automaticai.io/v1"}}}
        with mock.patch.object(automaticai, "routing_config", return_value=config):
            snapshot = automaticai.audit(data)
        self.assertEqual(snapshot["provider"], "openai")
        self.assertEqual(snapshot["status"], "bypass")
        self.assertEqual(snapshot["authority"], "session_record")
        self.assertEqual(automaticai.session_metadata(dict(data, session_id="another-session"), self.root), {})


if __name__ == "__main__":
    unittest.main()
