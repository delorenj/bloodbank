from __future__ import annotations

import sys
import unittest
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from state import (  # noqa: E402
    ATTENTION,
    clear_on_focus,
    focused_panes,
    ERROR,
    IDLE,
    WORKING,
    agent_panes,
    apply_event,
    origin_of,
    pane_key,
    reconcile,
)

PANE = ("Workspace", 41)
KEY = pane_key(*PANE)


def env(ce_type: str, *, pane: int | None = 41, session: str | None = "Workspace", **data):
    payload = dict(data)
    if pane is not None:
        payload["zellij_pane_id"] = pane
    if session is not None:
        payload["zellij_session_name"] = session
    return {"type": ce_type, "data": payload, "actor": {"cli": "claude"}}


class AttributionTest(unittest.TestCase):
    def test_reads_pane_and_session(self) -> None:
        self.assertEqual(origin_of(env("x")), PANE)

    def test_unattributable_shapes_are_skipped_not_guessed(self) -> None:
        # working_directory alone cannot separate two tabs in the same repo, so
        # an unstamped event must be ignored rather than approximated.
        for label, e in [
            ("no pane", env("x", pane=None)),
            ("no session", env("x", session=None)),
            ("pane is a string", {"type": "x", "data": {"zellij_pane_id": "41", "zellij_session_name": "W"}}),
            ("pane is a bool", {"type": "x", "data": {"zellij_pane_id": True, "zellij_session_name": "W"}}),
            ("pane negative", env("x", pane=-1)),
            ("data not a dict", {"type": "x", "data": "nope"}),
            ("no data", {"type": "x"}),
        ]:
            with self.subTest(label):
                self.assertIsNone(origin_of(e))


class FoldTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s: dict = {}

    def test_each_event_type_maps_to_its_state(self) -> None:
        cases = [
            ("bloodbank.agent.session.started", WORKING),
            ("bloodbank.conversation.turn.started", WORKING),
            ("bloodbank.agent.tool.requested", WORKING),
            ("bloodbank.agent.tool.completed", WORKING),
            ("bloodbank.agent.invocation.completed", WORKING),
            ("bloodbank.agent.session.ended", IDLE),
            ("bloodbank.agent.invocation.failed", ERROR),
            ("deckard.v1.agent.attention", ATTENTION),
        ]
        for ce_type, expected in cases:
            with self.subTest(ce_type):
                s: dict = {}
                apply_event(s, env(ce_type), now=100.0)
                self.assertEqual(s[KEY]["state"], expected)

    def test_unknown_type_and_unattributable_are_ignored(self) -> None:
        self.assertIsNone(apply_event(self.s, env("bloodbank.agent.nope"), 1.0))
        self.assertIsNone(
            apply_event(self.s, env("bloodbank.agent.tool.requested", pane=None), 1.0)
        )
        self.assertEqual(self.s, {})

    def test_midturn_traffic_does_not_clobber_a_waiting_state(self) -> None:
        # The bell says "answer me". A tool call firing mid-turn must not erase it.
        apply_event(self.s, env("deckard.v1.agent.attention"), 100.0)
        apply_event(self.s, env("bloodbank.agent.tool.requested"), 101.0)
        self.assertEqual(self.s[KEY]["state"], ATTENTION)
        # ...but it does refresh liveness, so reconcile does not decay it.
        self.assertEqual(self.s[KEY]["seen"], 101.0)

    def test_midturn_traffic_does_not_clobber_error(self) -> None:
        apply_event(self.s, env("bloodbank.agent.invocation.failed"), 100.0)
        apply_event(self.s, env("bloodbank.agent.tool.completed"), 101.0)
        self.assertEqual(self.s[KEY]["state"], ERROR)

    def test_a_new_prompt_resets_a_waiting_state(self) -> None:
        # A human typing a new prompt has plainly moved on; the old bell and the
        # dead turn's red are stale and must clear.
        for stale in (ATTENTION, ERROR):
            with self.subTest(stale):
                s: dict = {}
                seed = (
                    "deckard.v1.agent.attention"
                    if stale == ATTENTION
                    else "bloodbank.agent.invocation.failed"
                )
                apply_event(s, env(seed), 100.0)
                apply_event(s, env("bloodbank.conversation.turn.started"), 101.0)
                self.assertEqual(s[KEY]["state"], WORKING)

    def test_turn_end_returns_to_idle(self) -> None:
        apply_event(self.s, env("bloodbank.agent.tool.requested"), 100.0)
        apply_event(self.s, env("bloodbank.agent.session.ended"), 101.0)
        self.assertEqual(self.s[KEY]["state"], IDLE)

    def test_repeat_of_the_same_state_is_not_a_change(self) -> None:
        self.assertEqual(apply_event(self.s, env("bloodbank.agent.tool.requested"), 100.0), KEY)
        self.assertIsNone(apply_event(self.s, env("bloodbank.agent.tool.completed"), 101.0))
        self.assertEqual(self.s[KEY]["seen"], 101.0)

    def test_panes_are_tracked_independently(self) -> None:
        apply_event(self.s, env("bloodbank.agent.tool.requested", pane=1), 100.0)
        apply_event(self.s, env("deckard.v1.agent.attention", pane=2), 100.0)
        self.assertEqual(self.s[pane_key("Workspace", 1)]["state"], WORKING)
        self.assertEqual(self.s[pane_key("Workspace", 2)]["state"], ATTENTION)


class AgentPanesTest(unittest.TestCase):
    def test_matches_on_process_name_not_command_substring(self) -> None:
        # `codex mcp-server` and a hermes daemon's python contain an agent name
        # in their path without being an interactive agent. Substring matching
        # on the command line has already corrupted state here once.
        rows = [(1, "claude"), (2, "python"), (3, "node")]
        envs = {
            1: {"ZELLIJ_SESSION_NAME": "Workspace", "ZELLIJ_PANE_ID": "41"},
            2: {"ZELLIJ_SESSION_NAME": "Workspace", "ZELLIJ_PANE_ID": "42"},
            3: {"ZELLIJ_SESSION_NAME": "Workspace", "ZELLIJ_PANE_ID": "43"},
        }
        self.assertEqual(agent_panes(rows, envs.get), {("Workspace", 41): "claude"})

    def test_agent_outside_zellij_is_not_attributed(self) -> None:
        self.assertEqual(agent_panes([(1, "claude")], lambda _pid: {}), {})


class ReconcileTest(unittest.TestCase):
    def _working(self, seen: float = 0.0) -> dict:
        return {
            KEY: {
                "session": "Workspace", "pane": 41, "state": WORKING,
                "since": seen, "seen": seen, "agent": "claude", "cwd": "", "source": "t",
            }
        }

    def test_closed_pane_is_dropped(self) -> None:
        s = self._working()
        changed = reconcile(s, live_panes=set(), live_agents=set(), now=100.0)
        self.assertEqual(changed, [KEY])
        self.assertEqual(s, {})

    def test_working_with_no_agent_process_decays(self) -> None:
        # This is how a MISSED session.ended heals. Without it the tab claims
        # "working" forever and nothing corrects it.
        s = self._working()
        reconcile(s, live_panes={PANE}, live_agents=set(), now=100.0)
        self.assertEqual(s[KEY]["state"], IDLE)
        self.assertEqual(s[KEY]["source"], "reconcile:no-agent-process")

    def test_working_within_grace_is_left_alone(self) -> None:
        # A hook can fire microseconds before the process exists.
        s = self._working(seen=95.0)
        reconcile(s, live_panes={PANE}, live_agents=set(), now=100.0, working_grace=20.0)
        self.assertEqual(s[KEY]["state"], WORKING)

    def test_working_with_a_live_agent_survives(self) -> None:
        s = self._working()
        reconcile(s, live_panes={PANE}, live_agents={PANE}, now=100.0)
        self.assertEqual(s[KEY]["state"], WORKING)

    def test_waiting_states_are_not_decayed_by_process_absence(self) -> None:
        # A bell and a failed turn are addressed to a HUMAN. They outlive the
        # process that raised them and end by acknowledgement or TTL, never by
        # the agent having exited -- which is exactly when they matter most.
        for stale in (ATTENTION, ERROR):
            with self.subTest(stale):
                s = self._working()
                s[KEY]["state"] = stale
                reconcile(s, live_panes={PANE}, live_agents=set(), now=100.0)
                self.assertEqual(s[KEY]["state"], stale)

    def test_failed_observation_decays_nothing(self) -> None:
        # "I could not look" must never be mistaken for "it is gone". A wedged
        # zellij or a failed ps must not silently blank every tab.
        s = self._working()
        changed = reconcile(s, live_panes=None, live_agents=None, now=100.0)
        self.assertEqual(changed, [])
        self.assertEqual(s[KEY]["state"], WORKING)


if __name__ == "__main__":
    unittest.main()


class PromotionTest(unittest.TestCase):
    """Observation may CREATE state, not only correct it.

    Without this the projector can only describe panes that happened to publish
    while it was listening. An agent already running when the service started,
    or one whose CLI has no bloodbank hooks at all, would stay invisible
    forever -- which is exactly how a codex tab went dark.
    """

    def test_a_live_agent_with_no_state_is_seeded_as_present(self) -> None:
        s: dict = {}
        changed = reconcile(s, live_panes={PANE}, live_agents={PANE: "codex"}, now=100.0)
        self.assertEqual(changed, [KEY])
        # IDLE, not WORKING: a live process proves PRESENCE, not ACTIVITY. An
        # agent waiting at a prompt has a process too, and marking those
        # "working" lit every tab green at once -- a signal always on carries no
        # information. Events raise it to working.
        self.assertEqual(s[KEY]["state"], IDLE)
        self.assertEqual(s[KEY]["source"], "reconcile:agent-process-seen")
        # A promoted pane names its agent, so it is as useful as an event-derived
        # one -- consumers distinguish codex from claude.
        self.assertEqual(s[KEY]["agent"], "codex")

    def test_promotion_never_overwrites_a_known_state(self) -> None:
        for existing in (ATTENTION, ERROR, IDLE):
            with self.subTest(existing):
                s = {KEY: {"session": "Workspace", "pane": 41, "state": existing,
                           "since": 1.0, "seen": 99.0, "agent": "", "cwd": "", "source": "bus"}}
                reconcile(s, live_panes={PANE}, live_agents={PANE}, now=100.0)
                self.assertEqual(s[KEY]["state"], existing)

    def test_an_agent_in_a_pane_we_cannot_see_is_not_promoted(self) -> None:
        s: dict = {}
        reconcile(s, live_panes=set(), live_agents={PANE}, now=100.0)
        self.assertEqual(s, {})

    def test_no_observation_promotes_nothing(self) -> None:
        s: dict = {}
        reconcile(s, live_panes=None, live_agents=None, now=100.0)
        self.assertEqual(s, {})


class FocusTest(unittest.TestCase):
    def _bell(self) -> dict:
        return {KEY: {"session": "Workspace", "pane": 41, "state": ATTENTION,
                      "since": 1.0, "seen": 1.0, "agent": "", "cwd": "", "source": "bus"}}

    def test_focused_panes_resolves_through_the_tab(self) -> None:
        mapping = {("Workspace", 41): 7, ("Workspace", 42): 7, ("Workspace", 43): 9}
        self.assertEqual(
            focused_panes(mapping, {"Workspace": 7}),
            {("Workspace", 41), ("Workspace", 42)},
        )

    def test_looking_at_the_tab_clears_its_bell(self) -> None:
        s = self._bell()
        changed = clear_on_focus(s, {PANE}, now=100.0)
        self.assertEqual(changed, [KEY])
        self.assertEqual(s[KEY]["state"], IDLE)
        self.assertEqual(s[KEY]["source"], "focus:acknowledged")

    def test_an_unfocused_bell_survives(self) -> None:
        s = self._bell()
        self.assertEqual(clear_on_focus(s, set(), now=100.0), [])
        self.assertEqual(s[KEY]["state"], ATTENTION)

    def test_focus_does_not_clear_error_or_working(self) -> None:
        # A glance is not a fix. An unresolved failure outlives being looked at.
        for keep in (ERROR, WORKING):
            with self.subTest(keep):
                s = self._bell()
                s[KEY]["state"] = keep
                self.assertEqual(clear_on_focus(s, {PANE}, now=100.0), [])
                self.assertEqual(s[KEY]["state"], keep)
