"""turn.completed must carry the agent's final reply as data.reply_text.

Measured 2026-10-17 over 1,499 real turn.completed events: reply_text was
absent for claude-code/opencode/codex and "" for hermes, so the artifact
sidecar's conversation_reply_mermaid basis could never fire. Claude Code's Stop
payload has carried last_assistant_message all along; the adapter never read it.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

from clients import get_adapter                                   # noqa: E402
from clients.base import (                                        # noqa: E402
    REPLY_TEXT_MAX_BYTES, last_assistant_text_from_transcript, reply_fields)
from core.session import SessionState                             # noqa: E402

TURN = "bloodbank.conversation.turn.completed"
FENCE = "Here you go:\n\n```mermaid\nflowchart LR\n  A --> B\n```\n"


def shape(cli: str, payload: dict, hook: str = "Stop") -> dict:
    session = SessionState(path=Path("/tmp/reply-text-test-session.json"))
    return get_adapter(cli).shape_data(session, TURN, hook, payload, [])


class ReplyFieldsTest(unittest.TestCase):
    def test_blank_or_missing_yields_nothing(self):
        for v in (None, "", "   \n", 5, {"a": 1}):
            self.assertEqual(reply_fields(v), {})

    def test_small_reply_passes_through_untouched(self):
        out = reply_fields(FENCE)
        self.assertEqual(out["reply_text"], FENCE)
        self.assertEqual(out["reply_length"], len(FENCE.encode()))
        self.assertNotIn("reply_truncated", out)

    def test_oversize_reply_is_truncated_flagged_and_utf8_safe(self):
        text = "é" * (REPLY_TEXT_MAX_BYTES)          # 2 bytes each
        out = reply_fields(text)
        self.assertTrue(out["reply_truncated"])
        self.assertEqual(out["reply_length"], 2 * REPLY_TEXT_MAX_BYTES)
        self.assertLessEqual(len(out["reply_text"].encode()), REPLY_TEXT_MAX_BYTES)
        out["reply_text"].encode("utf-8")             # no half characters


class ClaudeReplyTest(unittest.TestCase):
    def test_stop_payload_last_assistant_message_is_promoted(self):
        data = shape("claude", {"cwd": "/x", "session_id": "s",
                                "hook_event_name": "Stop",
                                "last_assistant_message": FENCE})
        self.assertEqual(data["reply_text"], FENCE)
        self.assertEqual(data["reply_length"], len(FENCE.encode()))
        self.assertEqual(data["outcome"], "completed")

    def test_no_reply_means_no_reply_keys(self):
        data = shape("claude", {"cwd": "/x", "session_id": "s"})
        self.assertNotIn("reply_text", data)

    def _transcript(self, records: list[dict], pad: int = 0) -> str:
        d = tempfile.mkdtemp()
        p = Path(d) / "t.jsonl"
        with p.open("w") as fh:
            if pad:
                fh.write(json.dumps({"type": "user", "message": {"content": "x" * pad}}) + "\n")
            for r in records:
                fh.write(json.dumps(r) + "\n")
        return str(p)

    def _asst(self, *blocks):
        return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}

    def test_transcript_fallback_takes_trailing_assistant_text(self):
        path = self._transcript([
            {"type": "user", "message": {"role": "user", "content": "draw it"}},
            self._asst({"type": "text", "text": "Let me look."}),
            self._asst({"type": "tool_use", "id": "1", "name": "Bash", "input": {}}),
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "1", "content": "ok"}]}},
            self._asst({"type": "thinking", "thinking": "hm"}),
            self._asst({"type": "text", "text": FENCE}),
        ])
        data = shape("claude", {"cwd": "/x", "transcript_path": path})
        self.assertEqual(data["reply_text"], FENCE.strip())

    def test_transcript_fallback_reads_only_the_tail(self):
        path = self._transcript([self._asst({"type": "text", "text": "final"})],
                                pad=2_000_000)
        self.assertEqual(last_assistant_text_from_transcript(path, tail_bytes=4096),
                         "final")

    def test_transcript_fallback_fails_open(self):
        for bad in (None, "/nonexistent/t.jsonl", "/etc/passwd", 7):
            self.assertIsNone(last_assistant_text_from_transcript(bad))
        d = tempfile.mkdtemp()
        p = Path(d) / "junk.jsonl"
        p.write_text("not json\n{broken\n")
        self.assertIsNone(last_assistant_text_from_transcript(str(p)))

    def test_prompt_with_no_reply_yet_is_not_misattributed(self):
        path = self._transcript([
            self._asst({"type": "text", "text": "previous turn answer"}),
            {"type": "user", "message": {"role": "user", "content": "new prompt"}},
        ])
        self.assertIsNone(last_assistant_text_from_transcript(path))


class CodexReplyTest(unittest.TestCase):
    def test_stop_payload_reply_is_promoted_and_raw_kept(self):
        payload = {"cwd": "/x", "session_id": "s", "turn_id": "t",
                   "hook_event_name": "Stop", "last_assistant_message": FENCE}
        data = shape("codex", payload)
        self.assertEqual(data["reply_text"], FENCE)
        self.assertEqual(data["payload"], payload)

    def test_notify_kebab_key_is_promoted(self):
        data = shape("codex", {"type": "agent-turn-complete",
                               "last-assistant-message": FENCE}, hook="notify")
        self.assertEqual(data["reply_text"], FENCE)

    def test_opencode_adapter_inherits_the_promotion(self):
        data = shape("opencode", {"session_id": "s", "turn_id": "t",
                                  "last_assistant_message": FENCE}, hook="session.idle")
        self.assertEqual(data["reply_text"], FENCE)

    def test_no_reply_no_keys(self):
        self.assertNotIn("reply_text", shape("codex", {"cwd": "/x"}))


class OpenCodeReplyTest(unittest.TestCase):
    """session.idle has no text; the adapter reads opencode's own store."""

    def setUp(self):
        import sqlite3
        self.dir = tempfile.mkdtemp()
        self.db = Path(self.dir) / "opencode.db"
        con = sqlite3.connect(self.db)
        con.execute("create table session_message (id text primary key, session_id text, "
                    "type text, seq integer, data text)")
        rows = [
            ("s1", "user", 1, {"text": "old"}),
            ("s1", "assistant", 2, {"content": [{"type": "text", "text": "old reply"}]}),
            ("s1", "idle", 3, {"outcome": "succeeded"}),
            ("s1", "user", 4, {"text": "draw"}),
            ("s1", "assistant", 5, {"content": [{"type": "tool", "name": "shell"}]}),
            ("s1", "assistant", 6, {"content": [{"type": "reasoning", "text": "hm"},
                                                {"type": "text", "text": FENCE}]}),
            ("s1", "idle", 7, {"outcome": "succeeded"}),
            ("s2", "user", 1, {"text": "p"}),
            ("s2", "idle", 2, {"outcome": "succeeded"}),
        ]
        for i, (sid, kind, seq, data) in enumerate(rows):
            con.execute("insert into session_message values (?,?,?,?,?)",
                        (f"m{i}", sid, kind, seq, json.dumps(data)))
        con.commit()
        con.close()
        import os
        os.environ["BB_OPENCODE_DB"] = str(self.db)

    def tearDown(self):
        import os
        os.environ.pop("BB_OPENCODE_DB", None)

    def test_idle_turn_carries_the_final_assistant_text(self):
        data = shape("opencode", {"session_id": "s1", "turn_id": "t"}, hook="session.idle")
        self.assertEqual(data["reply_text"], FENCE)

    def test_a_turn_with_no_reply_never_inherits_the_previous_one(self):
        data = shape("opencode", {"session_id": "s2"}, hook="session.idle")
        self.assertNotIn("reply_text", data)

    def test_missing_db_or_session_fails_open(self):
        import os
        os.environ["BB_OPENCODE_DB"] = "/nonexistent/opencode.db"
        self.assertNotIn("reply_text", shape("opencode", {"session_id": "s1"}, hook="session.idle"))
        os.environ["BB_OPENCODE_DB"] = str(self.db)
        self.assertNotIn("reply_text", shape("opencode", {"session_id": "nope"}, hook="session.idle"))
        self.assertNotIn("reply_text", shape("opencode", {}, hook="session.idle"))


class LargePayloadTest(unittest.TestCase):
    def test_read_payload_survives_a_reply_over_64k(self):
        """read_stdin_text capped at 64 KiB cut the JSON mid-string, so the
        event lost its session id along with the reply."""
        import os
        big = {"session_id": "s", "last_assistant_message": "é" * 100_000}
        raw = json.dumps(big).encode()          # ensure_ascii: ~600 KB on the wire
        r, w = os.pipe()
        import threading
        threading.Thread(target=lambda: (os.write(w, raw), os.close(w)), daemon=True).start()
        old = sys.stdin
        try:
            sys.stdin = os.fdopen(r, "r")
            payload = get_adapter("claude").read_payload([])
        finally:
            sys.stdin = old
        self.assertEqual(payload.get("session_id"), "s")
        self.assertEqual(len(payload["last_assistant_message"]), 100_000)


if __name__ == "__main__":
    unittest.main()
