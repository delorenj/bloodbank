"""Hermes adapter: the native shell-hook wire -> bus event data.

Pins the real field names Hermes' shell_hooks bridge sends (hermes-agent
agent/shell_hooks.py _payload_fields + turn_finalizer on_session_end), so a
rename on either side fails here and not in a silent artifact-capture gap:
on_session_end carries the reply as `final_response` (under `extra`), the
session cwd as top-level `cwd`, and post_tool_call the call args as
top-level `tool_input`.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

from clients import get_adapter          # noqa: E402
from core.session import SessionState    # noqa: E402

PROJECT = "/home/delorenj/code/33GOD/bloodbank"


def _shape(ce_type: str, payload: dict) -> dict:
    session = SessionState(path=Path("/tmp/hermes-adapter-test-session.json"))
    return get_adapter("hermes").shape_data(session, ce_type, "hook", payload, [])


class HermesNativeWireTest(unittest.TestCase):
    def test_turn_completed_reads_final_response(self):
        reply = "Here:\n```mermaid\ngraph TD; A-->B\n```\n"
        data = _shape("bloodbank.conversation.turn.completed", {
            "hook_event_name": "on_session_end",
            "session_id": "s1",
            "cwd": PROJECT,
            "profile": "rererere",
            "extra": {
                "turn_id": "t1", "completed": True, "failed": False,
                "interrupted": False, "platform": "cli",
                "final_response": reply,
            },
        })
        self.assertEqual(data["reply_text"], reply)
        self.assertEqual(data["reply_length"], len(reply.encode()))
        self.assertEqual(data["working_directory"], PROJECT)
        self.assertEqual(data["outcome"], "completed")

    def test_final_response_wins_over_generic_keys(self):
        data = _shape("bloodbank.conversation.turn.completed", {
            "cwd": PROJECT,
            "extra": {"final_response": "real reply", "result": "noise"},
        })
        self.assertEqual(data["reply_text"], "real reply")

    def test_empty_final_response_stays_empty(self):
        data = _shape("bloodbank.conversation.turn.completed", {
            "cwd": PROJECT, "extra": {"final_response": ""},
        })
        self.assertEqual(data["reply_text"], "")

    def test_tool_completed_promotes_tool_input(self):
        args = {"path": f"{PROJECT}/_bmad-output/x.md", "content": "# hi"}
        data = _shape("bloodbank.agent.tool.completed", {
            "hook_event_name": "post_tool_call",
            "tool_name": "write_file",
            "tool_input": args,
            "session_id": "s1",
            "cwd": PROJECT,
            "extra": {"result": "{}", "tool_call_id": "c1"},
        })
        self.assertEqual(data["arguments"], args)
        self.assertEqual(data["working_directory"], PROJECT)


if __name__ == "__main__":
    unittest.main()
