"""Swapping an agent's runner must not orphan the hook it replaces.

The hook-hub cutover changes each agent's `runner` from `bloodbank/publish.py`
to the `bb-hook` re-trigger. `_merge_hooks()` finds "our" hook by command
SUBSTRING via `_publisher_markers()`, so if the old path is dropped from the
marker list at the same time the runner changes, the installer no longer
recognizes the live entry as its own: it appends the new hook and leaves the old
one in place, firing forever.

The failure is NOT unbounded growth -- `_merge_hooks` appends only when no live
hook matches, so it stabilizes at one orphan. That is worse than it sounds,
because a stable orphan looks like a working install while publishing every
event twice.

The fix is data-only: keep the retired path in `legacy_publishers` so the marker
list still matches the live entry and the merge updates it in place.

Run: python3 -m pytest services/agent-hooks/tests/test_runner_swap_idempotency.py
"""
from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("bbsync", SERVICE_DIR / "sync.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

OLD_PUBLISHER = (
    "cat | mise x -- python /h/.agents/hooks/bloodbank/publish.py "
    "--client claude --hook tool-request"
)
FOREIGN = "if [ -f '/h/.orca/agent-hooks/claude-hook.sh' ]; then . ; fi"
NEW_RETRIGGER = "/h/.agents/hooks/bb-hook claude PreToolUse"


def live_fixture() -> dict:
    """Today's real shape: our hook nested beside a foreign sibling in one group."""
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [
                        {"type": "command", "command": OLD_PUBLISHER, "timeout": 3},
                        {"type": "command", "command": FOREIGN, "timeout": 3},
                    ],
                }
            ]
        }
    }


GENERATED = {
    "PreToolUse": [
        {
            "matcher": ".*",
            "hooks": [{"type": "command", "command": NEW_RETRIGGER, "timeout": 3}],
        }
    ]
}


def commands(live: dict, event: str = "PreToolUse") -> list[str]:
    return [h["command"] for g in live["hooks"].get(event, []) for h in g["hooks"]]


class TestRunnerSwap(unittest.TestCase):
    def _install(self, agent: dict, times: int = 3) -> dict:
        markers = sync._publisher_markers("claude", agent)
        live = live_fixture()
        for _ in range(times):
            sync._merge_hooks(live, copy.deepcopy(GENERATED), markers)
        return live

    def test_dropping_the_old_path_orphans_the_old_hook(self):
        """Regression guard: this is the shape the cutover must NOT ship."""
        live = self._install({"publisher": "bb-hook", "legacy_publishers": []})
        cmds = commands(live)
        self.assertIn(NEW_RETRIGGER, cmds)
        self.assertIn(
            OLD_PUBLISHER, cmds,
            "if this no longer holds, _merge_hooks learned to retire its own "
            "old entry and legacy_publishers may no longer be load-bearing",
        )
        # Stable, not unbounded -- the orphan is appended exactly once.
        self.assertEqual(len(cmds), 3)

    def test_retaining_the_old_path_updates_in_place(self):
        """The fix: old path stays in legacy_publishers, so the merge recognizes it."""
        live = self._install({
            "publisher": "bb-hook",
            "legacy_publishers": ["bloodbank/publish.py", "claude/publish.py"],
        })
        cmds = commands(live)
        self.assertEqual(
            cmds, [NEW_RETRIGGER, FOREIGN],
            "expected in-place replacement preserving the foreign sibling",
        )

    def test_repeated_installs_are_byte_identical(self):
        agent = {
            "publisher": "bb-hook",
            "legacy_publishers": ["bloodbank/publish.py", "claude/publish.py"],
        }
        one = self._install(agent, times=1)
        five = self._install(agent, times=5)
        self.assertEqual(one, five, "install is not idempotent")

    def test_foreign_hook_survives_every_path(self):
        """Whatever else happens, orca must never be touched."""
        for legacy in ([], ["bloodbank/publish.py"]):
            live = self._install({"publisher": "bb-hook", "legacy_publishers": legacy})
            self.assertIn(FOREIGN, commands(live), f"foreign hook lost (legacy={legacy})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
