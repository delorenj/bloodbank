"""Session write-back: every substantive turn leaves its ask and outcome behind.

Pins the 2026-09-26 replacement of the SessionEnd "Files edited:" + code
excerpt summary (DeLoContainers stacks/ai/hindsight/docs/2026-09-26-leverage-audit.md,
plan item 2a). A fake Hindsight server records every request, so these tests
assert on the exact wire shape: one append-mode document per session, shared
observation scope, deterministic operation ids, and retries that never drop a
batch silently.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import concerns  # noqa: E402
import hindsight  # noqa: E402
import session_capture as sc  # noqa: E402


class FakeHindsight:
    """Just enough of the Hindsight HTTP API: retain and operation status."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.post_codes: list[int] = []     # consumed per POST; empty = 200
        self.op_status: dict[str, str] = {}  # operation id -> status; default completed
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code: int, body: dict):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, body))
                code = owner.post_codes.pop(0) if owner.post_codes else 200
                if code == 200:
                    self._reply(200, {"success": True, "operation_id": body.get("operation_id"), "async": True})
                else:
                    self._reply(code, {"detail": "boom"})

            def do_GET(self):
                owner.requests.append(("GET", self.path, None))
                op = self.path.rsplit("/", 1)[-1]
                status = owner.op_status.get(op, "completed")
                if status == "not_found":
                    self._reply(404, {"detail": "Operation not found"})
                else:
                    self._reply(200, {"operation_id": op, "status": status,
                                      "error_message": "Budget limit exceeded" if status == "failed" else None})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def posts(self) -> list[dict]:
        return [body for method, _, body in self.requests if method == "POST"]

    def gets(self) -> list[str]:
        return [path for method, path, _ in self.requests if method == "GET"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Clock:
    def __init__(self, start: float = 1_790_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


class CaptureTestCase(unittest.TestCase):
    cli = "claude"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.api = FakeHindsight()
        self.clock = Clock()
        self.patches = [
            mock.patch.dict(os.environ, {
                "HINDSIGHT_CAPTURE_DIR": str(self.root / "capture"),
                "HS_JOURNAL_DIR": str(self.root / "journal"),
                "HINDSIGHT_API_URL": self.api.url, "HINDSIGHT_API_KEY": "test-key",
                "HINDSIGHT_CAPTURE_FLUSH_CHARS": "100000", "BB_HOOK_INVOCATION_ID": "t",
                "HINDSIGHT_CAPTURE": "1", "HINDSIGHT_SESSION_STRATEGY": "conversation",
            }),
            mock.patch.object(hindsight, "bank", return_value="test-bank"),
            mock.patch.object(hindsight, "main_checkout", return_value=self.repo),
            mock.patch.object(sc, "_repo_label", return_value="test-repo"),
            mock.patch.object(sc, "_now", self.clock),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.api.close()
        self.temporary.cleanup()

    # -- helpers -----------------------------------------------------------
    def payload(self, **extra) -> dict:
        return concerns.normalized({"session_id": "s1", "cwd": str(self.repo), **extra})

    def ask(self, prompt: str) -> dict:
        return sc.record_ask(self.payload(prompt=prompt), self.cli)

    def edit(self, **extra) -> dict:
        return sc.record_edit(self.payload(**extra), self.cli)

    def stop(self, message: str, native: str = "Stop", **extra) -> dict:
        return sc.record_turn(self.payload(last_assistant_message=message, **extra), self.cli, native)

    def end(self) -> dict:
        return sc.end_session(self.payload(reason="prompt_input_exit"), self.cli)

    def state(self) -> dict | None:
        return sc.load(sc.state_path(self.cli, "s1"))

    def journal(self) -> list[dict]:
        path = self.root / "journal/sessions" / f"{self.cli}-s1.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def events(self, name: str) -> list[dict]:
        return [row for row in self.journal() if row.get("event") == name]

    def turn(self, n: int = 1, files: bool = False):
        self.ask(f"Why does the nightly backup fail, attempt {n}? Please find the root cause.")
        if files:
            self.edit(tool_name="Edit", tool_input={"file_path": str(self.repo / "scripts/backup.sh"),
                                                    "old_string": "x", "new_string": "SECRET_CODE_BODY " * 20})
        return self.stop(f"Turn {n}: the cron job used TCP auth without a password; I switched it to peer "
                         f"auth over the unix socket and verified all 26 databases dump cleanly.")


class AskAndEditTests(CaptureTestCase):
    def test_the_ask_gets_the_recall_paths_harness_hygiene(self):
        self.ask("<system-reminder>\nbe nice\n</system-reminder>\n/fix-it why does the backup fail on peer auth?")
        self.assertEqual(self.state()["pending_asks"], ["why does the backup fail on peer auth?"])

    def test_a_harness_only_prompt_buffers_no_ask(self):
        outcome = self.ask("<task-notification>\n<task-id>x</task-id>\n</task-notification>")
        self.assertEqual(outcome["_hook_hub"]["reason"], "ask_is_harness_only")
        self.assertEqual(self.state()["pending_asks"], [])

    def test_an_edit_records_the_path_relative_to_the_repo_and_never_the_content(self):
        self.edit(tool_name="Edit", tool_input={"file_path": str(self.repo / "a/b.py"), "new_string": "code " * 50})
        state = self.state()
        self.assertEqual(state["pending_files"], ["a/b.py"])
        self.assertNotIn("code code", json.dumps(state))

    def test_a_read_is_not_an_edit(self):
        outcome = self.edit(tool_name="Read", tool_input={"file_path": str(self.repo / "a.py")})
        self.assertEqual(outcome["_hook_hub"]["reason"], "no_file_edit")

    def test_codex_exec_code_calling_apply_patch_yields_its_paths(self):
        # Codex 0.157 wraps edits as `exec` JS; the patch sits in a string literal.
        js = ('text(await tools.apply_patch("*** Begin Patch\\n*** Update File: ' + str(self.repo / "x/y.md")
              + '\\n@@\\n-a\\n+b\\n*** Add File: ' + str(self.repo / "z.txt") + '\\n+hi\\n*** End Patch"))')
        self.edit(tool_name="exec", tool_input={"input": js})
        self.assertEqual(self.state()["pending_files"], ["x/y.md", "z.txt"])

    def test_codex_apply_patch_hook_payload_yields_its_path(self):
        # The real Codex 0.157 PostToolUse shape: one event per nested tool, the
        # patch under `command` (the old path looked only for `patch`).
        self.edit(tool_name="apply_patch", tool_input={
            "command": "*** Begin Patch\n*** Update File: notes/decision.md\n@@\n+Jitter is full jitter.\n*** End Patch"})
        self.assertEqual(self.state()["pending_files"], ["notes/decision.md"])

    def test_a_heredoc_write_yields_its_path(self):
        self.edit(tool_name="Bash", tool_input={"command": "mkdir -p notes\ncat > notes/decision.md <<'EOF'\nx\nEOF"})
        self.edit(tool_name="Bash", tool_input={"command": "cat <<EOF >> docs/log.md\ny\nEOF"})
        self.edit(tool_name="Bash", tool_input={"command": "tee -a CHANGELOG.md <<EOF\nz\nEOF"})
        self.assertEqual(self.state()["pending_files"], ["notes/decision.md", "docs/log.md", "CHANGELOG.md"])

    def test_scratch_and_device_writes_are_not_work(self):
        outcome = self.edit(tool_name="Bash", tool_input={"command": "cat > /tmp/x.json <<EOF\n{}\nEOF; echo hi > /dev/null"})
        self.assertEqual(outcome["_hook_hub"]["reason"], "no_file_edit")

    def test_a_raw_apply_patch_string_yields_its_paths(self):
        patch = "*** Begin Patch\n*** Delete File: old.py\n*** Update File: src/new.py\n*** Move to: src/newer.py\n*** End Patch"
        self.edit(tool_name="apply_patch", tool_input=patch)
        self.assertEqual(self.state()["pending_files"], ["old.py", "src/new.py", "src/newer.py"])

    def test_a_failed_tool_records_nothing(self):
        outcome = self.edit(tool_name="Write", tool_input={"file_path": "a.py"}, tool_response={"success": False})
        self.assertEqual(outcome["_hook_hub"]["reason"], "tool_failed")
        self.assertIsNone(self.state())


class TurnTests(CaptureTestCase):
    def test_a_turn_pairs_the_ask_with_the_outcome_and_the_files(self):
        self.turn(files=True)
        [turn] = self.state()["turns"]
        self.assertIn("Please find the root cause", turn["ask"])
        self.assertIn("peer auth", turn["outcome"])
        self.assertEqual(turn["files"], ["scripts/backup.sh"])
        self.assertEqual(self.state()["pending_asks"], [])
        self.assertEqual(self.events("turn_captured")[0]["source"], "payload")

    def test_a_trivial_turn_is_skipped(self):
        self.ask("thanks, that's all for now, have a great day")
        self.assertEqual(self.stop("You're welcome!")["_hook_hub"]["reason"], "turn_trivial")
        self.assertEqual(self.state()["turns"], [])
        self.assertEqual(self.state()["pending_asks"], [], "a dropped turn takes its ask with it")

    def test_a_short_outcome_with_edits_is_kept(self):
        self.edit(tool_name="Write", tool_input={"file_path": str(self.repo / "a.py"), "content": "x"})
        self.assertEqual(self.stop("Done.")["_hook_hub"]["reason"], "turn_buffered")

    def test_the_same_stop_twice_is_captured_once(self):
        self.turn()
        again = self.stop("Turn 1: the cron job used TCP auth without a password; I switched it to peer "
                          "auth over the unix socket and verified all 26 databases dump cleanly.")
        self.assertEqual(again["_hook_hub"]["reason"], "turn_already_captured")
        self.assertEqual(len(self.state()["turns"]), 1)

    def test_an_interrupt_is_not_a_turn(self):
        self.assertEqual(self.stop("partial", native="Interrupt")["_hook_hub"]["reason"], "turn_interrupted")

    def test_long_code_is_elided_and_the_outcome_capped(self):
        code = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(40)) + "\n```"
        self.stop("Here is the fix I applied to the parser.\n\n" + code + "\n\n" + "It now handles X. " * 400)
        outcome = self.state()["turns"][0]["outcome"]
        self.assertIn("[code block elided: 40 lines]", outcome)
        self.assertNotIn("line_12", outcome)
        self.assertLessEqual(len(outcome), sc.limits()["outcome_chars"])
        self.assertIn("[…]", outcome)

    def test_short_code_stays(self):
        self.stop("Run this to verify the dump restores:\n\n```bash\npg_restore -l x.dump | wc -l\n```\n\nNon-zero means readable.")
        self.assertIn("pg_restore -l", self.state()["turns"][0]["outcome"])

    def test_an_uncaptured_cli_is_skipped(self):
        outcome = sc.record_turn(self.payload(last_assistant_message="x" * 200), "hermes", "on_session_end")
        self.assertEqual(outcome["_hook_hub"]["reason"], "cli_not_captured")

    def test_sessions_are_isolated_by_cli(self):
        self.turn(1)
        self.cli = "codex"
        self.assertIsNone(self.state())
        self.assertEqual(sc.end_session(self.payload(), "codex")["_hook_hub"]["reason"], "no_captured_turns")
        self.cli = "claude"
        self.assertEqual(len(self.state()["turns"]), 1)

    def test_the_kill_switch(self):
        with mock.patch.dict(os.environ, {"HINDSIGHT_CAPTURE": "0"}):
            self.assertEqual(self.turn()["_hook_hub"]["reason"], "capture_disabled")


class FlushTests(CaptureTestCase):
    def test_nothing_is_sent_below_the_threshold(self):
        self.turn(1)
        self.turn(2)
        self.assertEqual(self.api.posts(), [])

    def test_the_threshold_flushes_one_append_mode_document(self):
        with mock.patch.dict(os.environ, {"HINDSIGHT_CAPTURE_FLUSH_CHARS": "200"}):
            outcome = self.turn(1, files=True)
        self.assertEqual(outcome["_hook_hub"]["reason"], "turn_buffered_flush_submitted")
        [body] = self.api.posts()
        [item] = body["items"]
        self.assertTrue(body["async"])
        uuid.UUID(body["operation_id"])
        self.assertEqual(item["document_id"], "session-claude-s1")
        self.assertEqual(item["update_mode"], "append")
        self.assertEqual(item["observation_scopes"], "shared")
        self.assertEqual(item["strategy"], "conversation")
        self.assertEqual(item["tags"], ["agent:claude", f"host:{sc._host()}"])
        self.assertEqual(item["metadata"]["session_id"], "s1")
        self.assertIn("test-repo", item["context"])
        messages = json.loads(item["content"])
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant"])
        self.assertTrue(messages[0]["content"].startswith("Session in test-repo (claude)"))
        self.assertIn("Files edited: scripts/backup.sh", messages[2]["content"])
        self.assertNotIn("SECRET_CODE_BODY", item["content"], "paths only, never code")
        self.assertEqual(self.api.requests[0][1], "/v1/default/banks/test-bank/memories")

    def test_later_flushes_append_to_the_same_document_without_a_second_header(self):
        with mock.patch.dict(os.environ, {"HINDSIGHT_CAPTURE_FLUSH_CHARS": "200"}):
            self.turn(1)
            self.clock.now += 5
            self.turn(2)
        first, second = self.api.posts()
        self.assertEqual(first["items"][0]["document_id"], second["items"][0]["document_id"])
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self.assertEqual([m["role"] for m in json.loads(second["items"][0]["content"])], ["user", "assistant"])

    def test_session_end_flushes_and_a_confirmed_session_is_forgotten(self):
        self.turn(1)
        self.turn(2)
        self.assertEqual(self.end()["_hook_hub"]["reason"], "session_flush_submitted")
        [body] = self.api.posts()
        self.assertEqual(len(json.loads(body["items"][0]["content"])), 5)
        self.assertIsNotNone(self.state(), "kept until the server confirms")
        self.clock.now += sc.CONFIRM_AFTER_S + 1
        sc.sweep()
        self.assertIsNone(self.state())
        self.assertEqual(self.events("session_flush_confirmed")[0]["turns"], [1, 2])

    def test_session_end_without_turns_writes_nothing(self):
        self.ask("a question that never got an answer before the user quit")
        self.assertEqual(self.end()["_hook_hub"]["reason"], "nothing_to_flush")
        self.assertEqual(self.api.posts(), [])
        self.assertIsNone(self.state())

    def test_an_idle_session_that_died_is_flushed_by_the_sweeper(self):
        self.turn(1)
        self.clock.now += sc.limits()["idle_s"] + 1
        report = sc.sweep()
        self.assertEqual(report["flushed"], 1)
        self.assertEqual(len(self.api.posts()), 1)

    def test_the_sweeper_command_runs(self):
        self.turn(1)
        self.clock.now += sc.limits()["idle_s"] + 1
        with mock.patch("builtins.print") as printed:
            self.assertEqual(sc.main(["session_capture.py", "sweep"]), 0)
        self.assertEqual(json.loads(printed.call_args[0][0])["flushed"], 1)


class RetryTests(CaptureTestCase):
    def test_a_rejected_flush_stays_buffered_and_retries_after_backoff(self):
        self.turn(1)
        self.api.post_codes = [500]
        self.assertEqual(self.end()["_hook_hub"]["reason"], "session_flush_deferred_for_retry")
        [batch] = self.state()["batches"]
        self.assertEqual((batch["status"], batch["attempt"]), ("retry", 1))
        self.assertEqual(self.events("session_flush")[0]["status"], "retry")
        self.clock.now += 10
        sc.sweep()
        self.assertEqual(len(self.api.posts()), 1, "not before its backoff")
        self.clock.now += sc.backoff(1)
        sc.sweep()
        self.assertEqual(len(self.api.posts()), 2)
        self.clock.now += sc.CONFIRM_AFTER_S + sc.RECHECK_S
        sc.sweep()
        self.assertIsNone(self.state())

    def test_a_failed_operation_is_resent_under_a_new_operation_id(self):
        self.turn(1)
        self.end()
        first = self.api.posts()[0]["operation_id"]
        self.api.op_status[first] = "failed"   # e.g. the LLM key's daily cap 403'd extraction
        self.clock.now += sc.CONFIRM_AFTER_S + 1
        sc.sweep()
        self.assertEqual(self.events("session_flush_failed")[0]["detail"], "Budget limit exceeded")
        self.clock.now += sc.backoff(1) + 1
        sc.sweep()
        second = self.api.posts()[1]["operation_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.api.posts()[0]["items"][0]["content"], self.api.posts()[1]["items"][0]["content"])

    def test_the_attempt_cap_dead_letters_the_exact_request(self):
        self.turn(1)
        self.api.post_codes = [500, 500]
        with mock.patch.dict(os.environ, {"HINDSIGHT_CAPTURE_MAX_ATTEMPTS": "2"}):
            self.end()
            self.clock.now += sc.backoff(1) + 1
            sc.sweep()
        [abandoned] = self.events("session_flush_abandoned")
        letter = json.loads(Path(abandoned["dead_letter"]).read_text())
        self.assertEqual(letter["request"]["items"][0]["document_id"], "session-claude-s1")
        self.assertEqual(letter["bank"], "test-bank")
        self.assertIsNone(self.state(), "a closed session with nothing left is forgotten")

    def test_an_unknown_outcome_is_checked_before_it_is_resent(self):
        self.turn(1)
        with mock.patch.object(sc, "send", return_value=("unknown", "TimeoutError")):
            self.assertEqual(self.end()["_hook_hub"]["reason"], "session_flush_unknown")
        op = sc.operation_id(self.state(), self.state()["batches"][0])
        self.api.op_status[op] = "not_found"
        self.clock.now += sc.RECHECK_S
        sc.sweep()
        self.assertEqual(self.api.posts(), [])
        self.assertEqual(self.state()["batches"][0]["status"], "retry")
        sc.sweep()
        [body] = self.api.posts()
        self.assertEqual(body["operation_id"], op, "never reached the server: same id, so no duplicate")

    def test_an_accepted_unknown_outcome_is_not_sent_twice(self):
        self.turn(1)
        with mock.patch.object(sc, "send", return_value=("unknown", "TimeoutError")):
            self.end()
        op = sc.operation_id(self.state(), self.state()["batches"][0])
        self.api.op_status[op] = "processing"
        self.clock.now += sc.RECHECK_S
        sc.sweep()
        self.assertEqual(self.state()["batches"][0]["status"], "submitted")
        self.api.op_status[op] = "completed"
        self.clock.now += sc.RECHECK_S + sc.CONFIRM_AFTER_S
        sc.sweep()
        self.assertEqual(self.api.posts(), [])
        self.assertIsNone(self.state())

    def test_new_turns_join_an_undelivered_batch_instead_of_racing_it(self):
        self.api.post_codes = [500]
        with mock.patch.dict(os.environ, {"HINDSIGHT_CAPTURE_FLUSH_CHARS": "200"}):
            self.turn(1)
            self.clock.now += 5
            self.turn(2)    # the first batch is backing off: no second request
        self.assertEqual(len(self.api.posts()), 1)
        [batch] = self.state()["batches"]
        self.assertEqual([t["n"] for t in batch["turns"]], [1, 2])
        self.clock.now += sc.backoff(1)
        sc.sweep()
        messages = json.loads(self.api.posts()[1]["items"][0]["content"])
        self.assertEqual(messages[0]["role"], "system", "the retried batch keeps its header")
        self.assertEqual(sum(m["role"] == "assistant" for m in messages), 2)


class TranscriptTests(CaptureTestCase):
    def write(self, name: str, rows: list[dict]) -> Path:
        path = self.root / name
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path

    def test_claude(self):
        path = self.write("claude.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "Pin the image please"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "assistant", "uuid": "u9", "message": {"content": [{"type": "text", "text": "Pinned by digest."}]}},
        ])
        self.assertEqual(sc.read_transcript(path), ("Pin the image please", "Pinned by digest.", "u9"))

    def test_codex(self):
        path = self.write("rollout.jsonl", [
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text", "text": "fix recall"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "Recall fixed."}]}},
        ])
        self.assertEqual(sc.read_transcript(path)[:2], ("fix recall", "Recall fixed."))

    def test_copilot(self):
        path = self.write("events.jsonl", [
            {"type": "user.message", "data": {"content": "review 33GOD-53"}},
            {"type": "assistant.message", "data": {"messageId": "m1", "content": "", "toolRequests": [{}]}},
            {"type": "assistant.message", "data": {"messageId": "m2", "content": "AC-7 fails."}},
        ])
        self.assertEqual(sc.read_transcript(path), ("review 33GOD-53", "AC-7 fails.", "m2"))

    def test_antigravity(self):
        path = self.write("transcript.jsonl", [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nrenew delo.sh\n</USER_REQUEST>\n<ADDITIONAL_METADATA>t</ADDITIONAL_METADATA>"},
            {"step_index": 5, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command"}]},
            {"step_index": 9, "type": "PLANNER_RESPONSE", "content": "Cloudflare rejected the transfer."},
        ])
        self.assertEqual(sc.read_transcript(path), ("renew delo.sh", "Cloudflare rejected the transfer.", "step:9"))

    def test_kimi_stop_finds_the_wire_by_session_id(self):
        wire = self.root / "kimi/sessions/wd_repo_1/session_s1/agents/main/wire.jsonl"
        wire.parent.mkdir(parents=True)
        wire.write_text("\n".join(json.dumps(row) for row in [
            {"type": "turn.prompt", "input": [{"type": "text", "text": "why is jot dead?"}]},
            {"type": "context.append_loop_event", "event": {"type": "content.part", "turnId": "0", "stepUuid": "a",
                                                            "part": {"type": "text", "text": "Checking."}}},
            {"type": "context.append_loop_event", "event": {"type": "content.part", "turnId": "0", "stepUuid": "b",
                                                            "part": {"type": "text", "text": "The DeepSeek normalizer returns Insufficient Balance and the script exits 0, so nothing alerts."}}},
        ]) + "\n")
        self.cli = "kimi"
        with mock.patch.dict(os.environ, {"KIMI_CODE_HOME": str(self.root / "kimi")}):
            outcome = sc.record_turn(self.payload(), "kimi", "Stop")
        self.assertEqual(outcome["_hook_hub"]["reason"], "turn_buffered")
        [turn] = self.state()["turns"]
        self.assertTrue(turn["outcome"].startswith("The DeepSeek normalizer"))
        self.assertEqual(turn["ask"], "why is jot dead?", "no prompt hook fired, so the wire's ask is used")

    def test_antigravity_stop_payload_end_to_end(self):
        transcript = self.write("ag.jsonl", [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nCan Cloudflare renew a Namecheap domain?\n</USER_REQUEST>"},
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "No: Cloudflare Registrar only renews domains transferred to it. I released the Namecheap lock and started a transfer."},
        ])
        self.cli = "antigravity"
        raw = {"conversationId": "s1", "workspacePaths": [str(self.repo)], "transcriptPath": str(transcript),
               "terminationReason": "done", "fullyIdle": True}
        outcome = sc.record_turn(concerns.normalized(raw), "antigravity", "Stop")
        self.assertEqual(outcome["_hook_hub"]["reason"], "turn_buffered")
        self.assertEqual(self.state()["turns"][0]["ask"], "Can Cloudflare renew a Namecheap domain?")
        busy = concerns.normalized({**raw, "fullyIdle": False})
        self.assertEqual(sc.record_turn(busy, "antigravity", "Stop")["_hook_hub"]["reason"], "turn_not_idle")

    def test_gemini_prompt_response_is_the_final_message(self):
        self.assertEqual(concerns.normalized({"prompt_response": "done"})["last_assistant_message"], "done")


class DispatchTests(CaptureTestCase):
    def run_concern(self, concern: str, role: str, native: str, raw: dict) -> dict:
        with mock.patch.dict(os.environ, {"BB_HOOK_CLI": "codex", "BB_HOOK_ROLE": role, "BB_HOOK_NATIVE": native}), \
                mock.patch.object(concerns, "disabled", return_value=False):
            return concerns.dispatch(concern, {"session_id": "s1", "cwd": str(self.repo), **raw})

    def test_a_codex_session_is_captured_through_the_registry_concerns(self):
        self.cli = "codex"
        self.run_concern("hindsight-turn", "prompt_submit", "UserPromptSubmit",
                         {"prompt": "<environment_context>cwd</environment_context>\nAdd a retry cap to the flush path"})
        self.run_concern("hindsight-retain", "post_tool", "PostToolUse",
                         {"tool_name": "exec", "tool_input": {"input": 'tools.apply_patch("*** Begin Patch\\n*** Update File: '
                                                                       + str(self.repo / "flush.py") + '\\n+x\\n*** End Patch")'}})
        turn = self.run_concern("hindsight-turn", "turn_completed", "Stop",
                                {"turn_id": "t1", "last_assistant_message": "Added a retry cap of eight attempts; failures past it go to a dead-letter file instead of being dropped."})
        self.assertEqual(turn["_hook_hub"]["reason"], "turn_buffered")
        end = self.run_concern("hindsight-session-end", "session_end", "SessionEnd", {"reason": "other"})
        self.assertEqual(end["_hook_hub"]["reason"], "session_flush_submitted")
        messages = json.loads(self.api.posts()[0]["items"][0]["content"])
        self.assertEqual(messages[1], {"role": "user", "content": "Add a retry cap to the flush path"})
        self.assertIn("Files edited: flush.py", messages[2]["content"])
        self.assertEqual(self.api.posts()[0]["items"][0]["document_id"], "session-codex-s1")

    def test_the_old_snippet_path_is_gone(self):
        self.cli = "codex"
        self.run_concern("hindsight-retain", "post_tool", "PostToolUse",
                         {"tool_name": "Write", "tool_input": {"file_path": str(self.repo / "a.py"), "content": "print(1)\n" * 30}})
        journal = self.root / "journal/sessions/codex-s1.jsonl"
        self.assertFalse(journal.exists() and "retain_candidate" in journal.read_text())
        self.assertFalse(hasattr(hindsight, "candidate"))
        self.assertFalse(hasattr(hindsight, "end"))


if __name__ == "__main__":
    unittest.main()
