"""crank-keys-guard: autonomous Hermes agents type into panes only through zcrank-send.

Run: python3 -m pytest services/hook-hub/tests/test_crank_keys_guard.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
GUARD = SERVICE / "crank_keys_guard.py"
sys.path.insert(0, str(SERVICE))
import crank_keys_guard as g  # noqa: E402

B = "timeout 6 env -u ZELLIJ -u ZELLIJ_SESSION_NAME zellij -s Workspace action "


def terminal(cmd: str) -> dict:
    return {"tool_name": "terminal", "tool_input": {"command": cmd}}


class CrankKeysGuard(unittest.TestCase):
    def denied(self, payload: dict, cli: str = "hermes") -> bool:
        return g.decide(payload, cli)[0] == 2

    def test_raw_typing_is_denied_in_every_shape_seen(self):
        for cmd in (
            B + "write-chars -p terminal_4 'continue'",
            "zellij -s Workspace action write-chars --pane-id terminal_150 'yes'",
            "zellij action paste -p terminal_3 'x'",
            "zellij action write 13 -p terminal_3",
            # state.db 2026-11-10: an inline python batch through execute_code
            "b='" + B + "'\nprint(terminal(b+'write-chars --pane-id terminal_150 '+shell_quote(msg)))",
            "subprocess.run(['zellij','-s','Workspace','action','write-chars','-p',pane,msg])",
            "/home/delorenj/.local/bin/zcrank-send -p terminal_4 ok; zellij action write-chars -p terminal_4 x",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(self.denied(terminal(cmd)))
        code = {"tool_name": "execute_code",
                "tool_input": json.dumps({"code": "import subprocess\nsubprocess.run(['zellij','action','write-chars','hi'])"})}
        self.assertTrue(self.denied(code))

    def test_zcrank_send_and_reads_are_allowed(self):
        for cmd in (
            "/home/delorenj/.local/bin/zcrank-send --pane-id terminal_4 --tab HeyMa -- 'continue'",
            "zcrank-send -p terminal_4 --relay-draft",
            B + "dump-screen -p terminal_4",
            B + "send-keys -p terminal_4 Esc",
            B + 'send-keys -p terminal_4 "Ctrl c"',
            "zellij action list-tabs --json",
            "grep -rn write-chars ~/.agents/skills",   # no zellij: a read, not typing
            "echo 'write the paste file'",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.denied(terminal(cmd)))

    def test_other_clis_are_not_guarded(self):
        self.assertFalse(self.denied(terminal(B + "write-chars -p terminal_4 hi"), cli="claude"))

    def test_the_process_exits_2_with_the_reason_and_fails_open(self):
        env = {**os.environ, "BB_HOOK_CLI": "hermes"}
        run = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(terminal(B + "write-chars -p terminal_4 hi")),
                             capture_output=True, text=True, env=env)
        self.assertEqual(run.returncode, 2)
        self.assertIn("zcrank-send", run.stderr)
        self.assertEqual(json.loads(run.stdout)["_hook_hub"]["reason"], "raw_pane_typing_denied")
        broken = subprocess.run([sys.executable, str(GUARD)], input="{not json", capture_output=True, text=True, env=env)
        self.assertEqual(broken.returncode, 0)


if __name__ == "__main__":
    unittest.main()
