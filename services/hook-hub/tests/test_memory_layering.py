"""An agent's memory is the agent's; the project's facts are the project's.

These pin FLUME-18/19. Before them, `write_bank` and `recall_banks` had been
declared in the agent registry for months and were read by NOTHING -- the one
reference outside the registry was a pjangler test asserting the YAML round-trips
byte-for-byte, which proves the field survives an edit, not that anyone reads it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hindsight


def registry(text: str) -> mock._patch:
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    handle.write(text)
    handle.close()
    return mock.patch.dict(os.environ, {"HERMES_AGENTS_REGISTRY": handle.name})


REGISTRY = """
schema_version: 1
agents:
  delonet-company-reporter:
    repo: delonet-company
    profile_name: delonet-company-reporter
    hindsight:
      write_bank: delonet-company
      recall_banks: [delonet-company, exec-office]
  33god-pm:
    repo: 33god
    profile_name: 33god-pm
"""


class IdentityTests(unittest.TestCase):
    def test_hermes_agent_is_identified_by_its_profile(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/home/x/.hermes/profiles/33god-pm/"}):
            self.assertEqual(hindsight.agent_profile("hermes"), "33god-pm")

    def test_the_fleet_router_is_not_an_agent(self):
        # It presents exactly like a profile but represents every PM at once.
        for name in ("fleet-bloodbank-gateway", "fleet-bloodbank"):
            with mock.patch.dict(os.environ, {"HERMES_HOME": f"/home/x/.hermes/profiles/{name}"}):
                self.assertEqual(hindsight.agent_profile("hermes"), "", name)

    def test_a_non_hermes_cli_has_no_agent_identity(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/home/x/.hermes/profiles/33god-pm"}):
            self.assertEqual(hindsight.agent_profile("claude"), "")


class DeclaredBankTests(unittest.TestCase):
    def test_the_registry_declaration_is_finally_read(self):
        with registry(REGISTRY), mock.patch.dict(os.environ, {"HERMES_HOME": "/p/delonet-company-reporter"}):
            self.assertEqual(hindsight.declared_banks("hermes"),
                             ("delonet-company", ["delonet-company", "exec-office"]))

    def test_an_agent_with_no_declaration_gets_nothing_invented(self):
        with registry(REGISTRY), mock.patch.dict(os.environ, {"HERMES_HOME": "/p/33god-pm"}):
            self.assertEqual(hindsight.declared_banks("hermes"), ("", []))

    def test_an_unreadable_registry_never_fails_a_prompt(self):
        with mock.patch.dict(os.environ, {"HERMES_AGENTS_REGISTRY": "/nope/missing.yaml",
                                          "HERMES_HOME": "/p/33god-pm"}):
            self.assertEqual(hindsight.declared_banks("hermes"), ("", []))


class AncestryTests(unittest.TestCase):
    def test_a_submodule_also_loads_its_superproject(self):
        with mock.patch.object(hindsight, "repository", return_value=Path("/code/33GOD/flume")), \
             mock.patch.object(hindsight, "bank_is_declared", return_value=False), \
             mock.patch.object(hindsight, "bank_at", side_effect=lambda root: Path(root).name), \
             mock.patch.object(hindsight.subprocess, "run") as run:
            run.side_effect = [mock.Mock(returncode=0, stdout="/code/33GOD\n"),
                               mock.Mock(returncode=0, stdout="\n")]
            self.assertEqual(hindsight.ancestor_banks(), ["33GOD"])

    def test_an_explicit_declaration_suppresses_ancestry(self):
        # A named bank is a decision; ancestry is an inference. The decision wins.
        with mock.patch.dict(os.environ, {"HINDSIGHT_BANK": "test-bank"}), \
             mock.patch.object(hindsight, "repository", return_value=Path("/code/33GOD/flume")):
            self.assertEqual(hindsight.ancestor_banks(), [])

    def test_ancestry_is_opt_out(self):
        with mock.patch.dict(os.environ, {"HINDSIGHT_ANCESTRY": "0"}):
            self.assertEqual(hindsight.ancestor_banks(), [])

    def test_outside_a_repository_there_is_no_ancestry(self):
        with mock.patch.object(hindsight, "repository", return_value=None), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hindsight.ancestor_banks(), [])


class OrderingTests(unittest.TestCase):
    def layered(self, **environ):
        base = {"HINDSIGHT_FANOUT": "0", "HINDSIGHT_GLOBAL_BANKS": "infra",
                "HERMES_HOME": "/p/delonet-company-reporter", **environ}
        with registry(REGISTRY), mock.patch.dict(os.environ, base), \
             mock.patch.object(hindsight, "ancestor_banks", return_value=["33GOD"]):
            return hindsight.recall_banks("hermes", "flume")

    def test_personal_leads_then_project_then_ancestors_then_declared(self):
        self.assertEqual(self.layered(),
                         ["delonet-company", "flume", "33GOD", "exec-office", "general", "infra"])

    def test_the_personal_bank_is_never_dropped_by_the_cap(self):
        # An agent that cannot remember what it has done is the defect the
        # ordering exists to prevent, so it leads and the cap trims the tail.
        banks = self.layered(HINDSIGHT_RECALL_MAX_BANKS="2")
        self.assertEqual(banks, ["delonet-company", "flume"])
        self.assertIn("delonet-company", banks)

    def test_a_bank_named_twice_is_read_once(self):
        self.assertEqual(len(self.layered()), len(set(self.layered())))

    def test_an_agentless_cli_is_unchanged_but_for_ancestry(self):
        with registry(REGISTRY), mock.patch.dict(os.environ, {"HINDSIGHT_FANOUT": "0", "HINDSIGHT_GLOBAL_BANKS": "infra"}), \
             mock.patch.object(hindsight, "ancestor_banks", return_value=["33GOD"]):
            self.assertEqual(hindsight.recall_banks("claude", "flume"), ["flume", "33GOD", "general", "infra"])


if __name__ == "__main__":
    unittest.main()
