"""Every agent event must say which repo it came from.

Consumers attribute an event to a project by `data.working_directory`. Deckard
does exactly that, and logged `event REFUSED: ... carries no working_directory`
for 16,014 hermes events over three days — the entire PM fleet was invisible to
the deck, and to anything else that attributes by repo.

The cwd was never missing. Both adapters carried it in `data.payload.cwd` and
simply never promoted it. Hermes additionally set `working_directory` on
`session.started` only, and set it from `os.getcwd()` — the HOOK process's
directory, which under systemd is the unit's, not the agent's.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

from clients import get_adapter          # noqa: E402
from core.session import SessionState    # noqa: E402

REPO = "/home/delorenj/code/james-brennan"

# (cli, payload carrying the agent's real cwd). Every CLI that publishes
# lifecycle events belongs here; a new adapter that forgets working_directory
# should fail this, not be discovered months later in a consumer's reject log.
CASES = [
    ("hermes", {"cwd": REPO, "tool_name": "read_file", "session_id": "s"}),
    ("copilot", {"cwd": REPO, "toolName": "read", "sessionId": "s"}),
    ("codex", {"cwd": REPO, "tool_name": "Bash", "session_id": "s"}),
    ("claude", {"cwd": REPO, "tool_name": "Bash"}),
]

TYPES = [
    "bloodbank.agent.tool.requested",
    "bloodbank.agent.tool.completed",
    "bloodbank.agent.session.started",
    "bloodbank.agent.session.ended",
    "bloodbank.conversation.turn.started",
]


class WorkingDirectoryTest(unittest.TestCase):
    def _shape(self, cli: str, ce_type: str, payload: dict) -> dict:
        adapter = get_adapter(cli)
        session = SessionState(path=Path("/tmp/asm-wd-test-session.json"))
        return adapter.shape_data(session, ce_type, "hook", payload, [])

    def test_every_cli_reports_a_working_directory_on_every_event(self):
        missing = []
        for cli, payload in CASES:
            for ce_type in TYPES:
                data = self._shape(cli, ce_type, dict(payload))
                if not data.get("working_directory"):
                    missing.append(f"{cli}/{ce_type.rsplit('.', 1)[-1]}")
        self.assertEqual(missing, [],
                         f"unattributable events (consumers will drop these): {missing}")

    def test_the_payload_cwd_wins_over_the_hook_processes_cwd(self):
        """Hermes runs under systemd: os.getcwd() is the unit's directory, not
        the agent's. Reporting it would attribute every PM to the wrong repo."""
        for cli in ("hermes", "copilot"):
            data = self._shape(cli, "bloodbank.agent.tool.completed",
                               {"cwd": REPO, "tool_name": "x", "toolName": "x"})
            self.assertEqual(data["working_directory"], REPO,
                             f"{cli} ignored the payload cwd")
            self.assertNotEqual(data["working_directory"], os.getcwd())

    def test_a_payload_without_a_cwd_still_yields_something(self):
        """Fail soft: an event with a plausible-but-wrong directory is still
        attributable, where one with none is dropped on the floor."""
        for cli in ("hermes", "copilot"):
            data = self._shape(cli, "bloodbank.agent.tool.completed",
                               {"tool_name": "x", "toolName": "x"})
            self.assertTrue(data.get("working_directory"))


if __name__ == "__main__":
    unittest.main()
