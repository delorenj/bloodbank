"""Pane attribution on the general agent lifecycle stream.

Before this, only the dedicated Deckard attention envelope carried an exact
pane id. Every other lifecycle event (session.started, tool.requested, ...)
identified its origin by `working_directory` + `actor.cli` alone, which cannot
separate two tabs sitting in the same repo. Any per-tab state machine built on
the stream was therefore guessing.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

AGENT_HOOKS_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_HOOKS_DIR))

from core.publisher import zellij_origin


class ZellijOriginTest(unittest.TestCase):
    def test_inside_zellij_stamps_pane_and_session(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"ZELLIJ_PANE_ID": "41", "ZELLIJ_SESSION_NAME": "Workspace"},
            clear=False,
        ):
            self.assertEqual(
                zellij_origin(),
                {"zellij_pane_id": 41, "zellij_session_name": "Workspace"},
            )

    def test_pane_id_is_an_int_not_a_string(self) -> None:
        # Consumers key state by pane id; a string here would silently create a
        # second, parallel key space for the same tab.
        with mock.patch.dict(
            "os.environ",
            {"ZELLIJ_PANE_ID": "7", "ZELLIJ_SESSION_NAME": "Workspace"},
            clear=False,
        ):
            self.assertIsInstance(zellij_origin()["zellij_pane_id"], int)

    def test_outside_zellij_stamps_nothing(self) -> None:
        env = {k: v for k, v in __import__("os").environ.items()}
        env.pop("ZELLIJ_PANE_ID", None)
        env.pop("ZELLIJ_SESSION_NAME", None)
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(zellij_origin(), {})

    def test_malformed_pane_id_is_dropped_not_raised(self) -> None:
        # A telemetry field must never be able to fail a hook. Every bad shape
        # degrades to "no attribution", never to an exception.
        for bad in ("abc", "", "99999999999", "-1", "1.5"):
            with self.subTest(pane=bad):
                with mock.patch.dict(
                    "os.environ",
                    {"ZELLIJ_PANE_ID": bad, "ZELLIJ_SESSION_NAME": "Workspace"},
                    clear=False,
                ):
                    self.assertEqual(zellij_origin(), {})

    def test_missing_session_name_is_dropped(self) -> None:
        env = {k: v for k, v in __import__("os").environ.items()}
        env.pop("ZELLIJ_SESSION_NAME", None)
        env["ZELLIJ_PANE_ID"] = "41"
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(zellij_origin(), {})

    def test_oversized_session_name_is_dropped(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"ZELLIJ_PANE_ID": "41", "ZELLIJ_SESSION_NAME": "x" * 4096},
            clear=False,
        ):
            self.assertEqual(zellij_origin(), {})

    def test_adapter_supplied_value_wins(self) -> None:
        # publish() merges with setdefault, so an adapter that already knows its
        # pane keeps its own answer. This pins that contract.
        data = {"hook": "Stop", "zellij_pane_id": 99}
        with mock.patch.dict(
            "os.environ",
            {"ZELLIJ_PANE_ID": "41", "ZELLIJ_SESSION_NAME": "Workspace"},
            clear=False,
        ):
            for key, value in zellij_origin().items():
                data.setdefault(key, value)
        self.assertEqual(data["zellij_pane_id"], 99)
        self.assertEqual(data["zellij_session_name"], "Workspace")


if __name__ == "__main__":
    unittest.main()
