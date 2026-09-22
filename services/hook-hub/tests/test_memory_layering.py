"""An agent's memory is the agent's; the project's facts are the project's.

These pin FLUME-18/19. Before them, `write_bank` and `recall_banks` had been
declared in the agent registry for months and were read by NOTHING -- the one
reference outside the registry was a pjangler test asserting the YAML round-trips
byte-for-byte, which proves the field survives an edit, not that anyone reads it.
"""
from __future__ import annotations

import json
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


class EndToEndRetainTests(unittest.TestCase):
    """`end()` really issues one retain per bank."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.calls = self.root / "calls.jsonl"
        fake = self.root / "hindsight"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json,os,sys\n"
            "from pathlib import Path\n"
            "with Path(os.environ['CALL_LOG']).open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
            # argv[0] is the script; argv[1:] is what hook-hub passed, so the
            # bank -- `memory retain <BANK> ...` -- lands at argv[3].
            "bank = sys.argv[3] if len(sys.argv) > 3 else ''\n"
            "if bank and bank == os.environ.get('FAIL_BANK',''): raise SystemExit(1)\n"
            "print(json.dumps({'success':True,'document_id':'accepted'}))\n")
        fake.chmod(0o700)
        self.environment = mock.patch.dict(os.environ, {
            "HINDSIGHT_BIN": str(fake), "HINDSIGHT_BANK": "flume",
            "HS_JOURNAL_DIR": str(self.root / "journal"), "CALL_LOG": str(self.calls),
            "HERMES_HOME": "/p/delonet-company-reporter", "BB_HOOK_INVOCATION_ID": "t"}, clear=False)
        self.environment.start()
        self.bank = mock.patch.object(hindsight, "bank", return_value="flume")
        self.bank.start()

    def tearDown(self):
        self.bank.stop(); self.environment.stop(); self.temporary.cleanup()

    def payload(self):
        return {"session_id": "route", "last_assistant_message":
                "I built the org chart renderer and marked inferred edges so they stay distinguishable."}

    def banks_written(self):
        return [json.loads(line)[2] for line in self.calls.read_text().splitlines()
                if json.loads(line)[:2] == ["memory", "retain"]]

    def test_one_session_is_retained_into_both_banks(self):
        with registry(REGISTRY):
            outcome = hindsight.end(self.payload(), "hermes")
        self.assertEqual(outcome["_hook_hub"]["reason"], "session_summary_retained")
        self.assertEqual(sorted(self.banks_written()), ["delonet-company", "flume"])

    def test_both_copies_share_one_document_id(self):
        with registry(REGISTRY):
            hindsight.end(self.payload(), "hermes")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        doc_ids = {call[call.index("--doc-id") + 1] for call in calls if "--doc-id" in call}
        self.assertEqual(len(doc_ids), 1, "a retry must replace the same document in each bank")

    def test_a_partial_write_succeeds_with_a_named_gap_and_retries_only_the_gap(self):
        # Reporting a partial write as failed would invite a retry that
        # re-retains the bank which already accepted.
        with registry(REGISTRY), mock.patch.dict(os.environ, {"FAIL_BANK": "flume"}):
            first = hindsight.end(self.payload(), "hermes")
        self.assertEqual(first["_hook_hub"]["reason"], "session_summary_retained_partially")
        self.assertEqual(first["_hook_hub"]["status"], "succeeded")
        self.calls.write_text("")
        with registry(REGISTRY):
            second = hindsight.end(self.payload(), "hermes")
        self.assertEqual(self.banks_written(), ["flume"], "only the bank that failed is retried")
        self.assertEqual(second["_hook_hub"]["reason"], "session_summary_retained")

    def test_a_fully_retained_session_is_not_written_again(self):
        with registry(REGISTRY):
            hindsight.end(self.payload(), "hermes")
            self.calls.write_text("")
            again = hindsight.end(self.payload(), "hermes")
        self.assertEqual(again["_hook_hub"]["reason"], "session_summary_already_retained")
        self.assertEqual(self.calls.read_text(), "")


if __name__ == "__main__":
    unittest.main()
