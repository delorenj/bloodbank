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
             mock.patch.dict(os.environ, {"HINDSIGHT_ANCESTRY": "1"}), \
             mock.patch.object(hindsight.subprocess, "run") as run:
            run.side_effect = [mock.Mock(returncode=0, stdout="/code/33GOD\n"),
                               mock.Mock(returncode=0, stdout="\n")]
            self.assertEqual(hindsight.ancestor_banks(), ["33GOD"])

    def test_an_explicit_declaration_suppresses_ancestry(self):
        # A named bank is a decision; ancestry is an inference. The decision wins.
        with mock.patch.dict(os.environ, {"HINDSIGHT_BANK": "test-bank", "HINDSIGHT_ANCESTRY": "1"}), \
             mock.patch.object(hindsight, "repository", return_value=Path("/code/33GOD/flume")):
            self.assertEqual(hindsight.ancestor_banks(), [])

    def test_ancestry_is_opt_in(self):
        # Off by default since 2026-09-26: each extra bank is another rerank the
        # prompt waits on. Unset and "0" both mean off.
        for value in (None, "0"):
            environ = {} if value is None else {"HINDSIGHT_ANCESTRY": value}
            with mock.patch.dict(os.environ, environ), \
                 mock.patch.object(hindsight, "repository", return_value=Path("/code/33GOD/flume")), \
                 mock.patch.object(hindsight.subprocess, "run") as run:
                if value is None:
                    os.environ.pop("HINDSIGHT_ANCESTRY", None)
                self.assertEqual(hindsight.ancestor_banks(), [])
                run.assert_not_called()

    def test_outside_a_repository_there_is_no_ancestry(self):
        with mock.patch.object(hindsight, "repository", return_value=None), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hindsight.ancestor_banks(), [])


class OrderingTests(unittest.TestCase):
    """Every opt-in switched on, so the relative order is visible."""

    def layered(self, **environ):
        base = {"HINDSIGHT_FANOUT": "0", "HINDSIGHT_GLOBAL_BANKS": "infra", "HINDSIGHT_RECALL_GENERAL": "1",
                "HERMES_HOME": "/p/delonet-company-reporter", **environ}
        with registry(REGISTRY), mock.patch.dict(os.environ, base), \
             mock.patch.object(hindsight, "ancestor_banks", return_value=["33GOD"]), \
             mock.patch.object(hindsight, "repo_recall_banks", return_value=[]):
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

    def test_an_agentless_cli_gets_the_same_opt_ins_minus_the_registry(self):
        with registry(REGISTRY), mock.patch.dict(os.environ, {"HINDSIGHT_FANOUT": "0", "HINDSIGHT_GLOBAL_BANKS": "infra",
                                                              "HINDSIGHT_RECALL_GENERAL": "1"}), \
             mock.patch.object(hindsight, "ancestor_banks", return_value=["33GOD"]), \
             mock.patch.object(hindsight, "repo_recall_banks", return_value=[]):
            self.assertEqual(hindsight.recall_banks("claude", "flume"), ["flume", "33GOD", "general", "infra"])


class RetainRoutingTests(unittest.TestCase):
    """EXPERIENCE to the person, WORLD to the project.

    The API exposes no type on write -- no `--type` on retain, no type field on
    the item schema -- so a bank's MISSION is the router. Verified with
    dry-run-extract: one identical summary yields "Agent built buildOrgChart ...
    | Involving: agent" in the identity bank and "Built an org chart renderer in
    tree.ts that reconciles ..." in the project bank. Episodic and semantic from
    the same bytes.
    """

    def test_a_declared_agent_writes_to_the_person_then_the_project(self):
        with registry(REGISTRY), mock.patch.dict(os.environ, {"HERMES_HOME": "/p/delonet-company-reporter"}):
            self.assertEqual(hindsight.retain_targets("hermes", "flume"),
                             ["delonet-company", "flume"])

    def test_the_person_leads_so_a_partial_failure_keeps_the_irreplaceable_half(self):
        # The project's copy can be rebuilt from the repo by a later session.
        # The agent's memory of having been there cannot be rebuilt by anything.
        with registry(REGISTRY), mock.patch.dict(os.environ, {"HERMES_HOME": "/p/delonet-company-reporter"}):
            self.assertEqual(hindsight.retain_targets("hermes", "flume")[0], "delonet-company")

    def test_an_undeclared_agent_writes_only_to_the_project(self):
        with registry(REGISTRY), mock.patch.dict(os.environ, {"HERMES_HOME": "/p/33god-pm"}):
            self.assertEqual(hindsight.retain_targets("hermes", "flume"), ["flume"])

    def test_a_non_agent_cli_is_unchanged(self):
        with registry(REGISTRY), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_HOME", None)
            self.assertEqual(hindsight.retain_targets("claude", "flume"), ["flume"])

    def test_a_personal_bank_equal_to_the_project_is_written_once(self):
        with registry(REGISTRY.replace("write_bank: delonet-company", "write_bank: flume")), \
             mock.patch.dict(os.environ, {"HERMES_HOME": "/p/delonet-company-reporter"}):
            self.assertEqual(hindsight.retain_targets("hermes", "flume"), ["flume"])


if __name__ == "__main__":
    unittest.main()
