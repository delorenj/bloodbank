"""Ephemeral (git worktree) attribution on the agent lifecycle stream.

Worktree sessions are disposable by fleet policy: the worktree is deleted on
merge, and every agent CLI keys its session history on the absolute path, so
the session's on-disk trail dies with it. The `ephemeral` envelope extension
is the breadcrumb that survives — worktree identity plus harness-native
session data, stamped at publish time, only when the publisher actually ran
inside a worktree.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

AGENT_HOOKS_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_HOOKS_DIR))

from core import ephemeral
from core.envelope import build_envelope
from core.ephemeral import ephemeral_context, worktree_context

REPO = "/home/operator/code/33GOD"
WT = f"{REPO}/.worktrees/feat-auth"


def _fake_git(worktree: bool) -> object:
    """_git stand-in: answers the three rev-parse probes for one layout."""
    def run(*args: str, cwd: str | None = None) -> str:
        joined = " ".join(args)
        if "--git-common-dir" in joined:
            return f"{REPO}/.git"
        if "--git-dir" in joined:
            return f"{REPO}/.git/worktrees/feat-auth" if worktree else f"{REPO}/.git"
        if "--show-toplevel" in joined:
            return WT if worktree else REPO
        if "--abbrev-ref" in joined:
            return "feat-auth" if worktree else "main"
        return ""
    return run


class WorktreeContextTest(unittest.TestCase):
    def test_inside_worktree_detected(self) -> None:
        with mock.patch.object(ephemeral, "_git", _fake_git(worktree=True)):
            ctx = worktree_context(WT)
        self.assertEqual(ctx["path"], WT)
        self.assertEqual(ctx["branch"], "feat-auth")
        self.assertEqual(ctx["repo"], "33GOD")
        self.assertEqual(ctx["main_checkout"], REPO)

    def test_main_checkout_is_not_ephemeral(self) -> None:
        with mock.patch.object(ephemeral, "_git", _fake_git(worktree=False)):
            self.assertIsNone(worktree_context(REPO))

    def test_non_git_dir_is_not_ephemeral(self) -> None:
        with mock.patch.object(ephemeral, "_git", lambda *a, cwd=None: ""):
            self.assertIsNone(worktree_context("/tmp"))

    def test_detection_ignores_path_convention(self) -> None:
        # A worktree created outside repo/.worktrees (sibling dir, central
        # hub) still reports ephemeral: detection keys on git's answer, not
        # on the path containing ".worktrees".
        with mock.patch.object(ephemeral, "_git", _fake_git(worktree=True)):
            ctx = worktree_context("/home/operator/random-hub/feat-auth")
        self.assertIsNotNone(ctx)


class EphemeralContextTest(unittest.TestCase):
    def test_session_data_lifted_from_payload(self) -> None:
        with mock.patch.object(ephemeral, "_git", _fake_git(worktree=True)):
            ctx = ephemeral_context(
                cwd=WT,
                harness="claude",
                turn_number=7,
                payload={
                    "session_id": "claude-native-uuid",
                    "transcript_path": f"{WT}/.claude/transcript.jsonl",
                },
            )
        self.assertEqual(ctx["session"]["harness"], "claude")
        self.assertEqual(ctx["session"]["harness_session_id"], "claude-native-uuid")
        self.assertEqual(ctx["session"]["transcript_path"], f"{WT}/.claude/transcript.jsonl")
        self.assertEqual(ctx["session"]["turn_number"], 7)
        self.assertEqual(ctx["worktree"]["path"], WT)

    def test_thread_id_accepted_as_harness_id(self) -> None:
        with mock.patch.object(ephemeral, "_git", _fake_git(worktree=True)):
            ctx = ephemeral_context(
                cwd=WT, harness="codex", turn_number=1, payload={"thread_id": "thr_9"}
            )
        self.assertEqual(ctx["session"]["harness_session_id"], "thr_9")
        self.assertNotIn("transcript_path", ctx["session"])

    def test_missing_payload_fields_degrade_not_raise(self) -> None:
        with mock.patch.object(ephemeral, "_git", _fake_git(worktree=True)):
            for bad in (None, "not-a-dict", {}, {"session_id": None}):
                with self.subTest(payload=bad):
                    ctx = ephemeral_context(
                        cwd=WT, harness="claude", turn_number=0, payload=bad
                    )
                    self.assertIn("worktree", ctx)
                    self.assertNotIn("harness_session_id", ctx["session"])

    def test_no_extension_outside_worktree(self) -> None:
        with mock.patch.object(ephemeral, "_git", _fake_git(worktree=False)):
            self.assertIsNone(
                ephemeral_context(
                    cwd=REPO,
                    harness="claude",
                    turn_number=3,
                    payload={"session_id": "x"},
                )
            )


class EnvelopeEphemeralTest(unittest.TestCase):
    def _envelope(self, eph: dict | None) -> dict:
        return build_envelope(
            ce_type="bloodbank.agent.session.started",
            kind="event",
            source="test",
            producer="test",
            service="agent-hooks",
            actor={"type": "agent_cli", "agent_id": "bloodbank.agent.claude"},
            data={"session_id": "s"},
            correlation_id="11111111-1111-4111-8111-111111111111",
            causation_id="11111111-1111-4111-8111-111111111111",
            ordering_key="cli_session:11111111-1111-4111-8111-111111111111",
            ephemeral=eph,
        )

    def test_ephemeral_included_when_present(self) -> None:
        eph = {"worktree": {"path": WT}, "session": {"harness": "claude"}}
        self.assertEqual(self._envelope(eph)["ephemeral"], eph)

    def test_ephemeral_omitted_when_none(self) -> None:
        self.assertNotIn("ephemeral", self._envelope(None))


if __name__ == "__main__":
    unittest.main()
