"""agent.invocation.skipped is a contract-valid, schema-valid event.

The n8n 33GOD Agent Fleet node publishes it whenever it decides NOT to invoke
an agent for a ticket event. It must pass the same gates every producer does:
the stdlib contract (type shape, allowlists, tense) and the JSON Schema.
"""
from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import validate  # noqa: E402

TYPE = "bloodbank.agent.invocation.skipped"


def envelope(**data_overrides):
    event_id = str(uuid.uuid4())
    data = {
        "reason": "james-brennan-pm is registry-defined but bloodbank.enabled is false",
        "skip_code": "ineligible",
        "operation": "groomTicket",
        "target_agent_id": "james-brennan-pm",
        "matched_by": "board",
        "context": {
            "reason": "ticket-grooming",
            "repo": "james-brennan",
            "ticket_key": "JIMB-273",
            "ticket_id": "5082ee4f-5e93-4fd5-8ee9-62ea4109b7fd",
            "board_id": "a8a12be1-b3ab-44f4-ab24-abe8829aeb72",
            "workspace": "automaticai",
            "title": "Wire the delegation lane",
            "phase": None,
            "provider_event_type": "plane.ticket.created",
        },
    }
    data.update(data_overrides)
    return {
        "specversion": "1.0",
        "id": event_id,
        "source": "urn:33god:integration:n8n:agent-fleet",
        "type": TYPE,
        "subject": "bloodbank.evt.agent.invocation.skipped",
        "time": "2026-09-22T12:00:00Z",
        "datacontenttype": "application/json",
        "dataschema": f"apicurio://holyfields/{TYPE}/versions/1",
        "correlationid": str(uuid.uuid4()),
        "causationid": str(uuid.uuid4()),
        "producer": "n8n",
        "service": "n8n-ticket-grooming",
        "domain": "agent",
        "schemaref": f"{TYPE}.v1",
        "traceparent": "00-00000000000000000000000000000000-0000000000000000-00",
        "kind": "event",
        "actor": {"type": "service", "agent_id": "bloodbank.integration.n8n"},
        "ordering_key": "task:james-brennan:5082ee4f-5e93-4fd5-8ee9-62ea4109b7fd",
        "data": data,
    }


class InvocationSkippedContract(unittest.TestCase):
    def test_skipped_is_an_allowlisted_past_tense_event_action(self):
        self.assertIn("skipped", validate.EVENT_ACTIONS)
        self.assertNotIn("skipped", validate.COMMAND_ACTIONS)

    def test_contract_accepts_the_event(self):
        validate.assert_contract(envelope())

    def test_contract_rejects_it_as_a_command(self):
        bad = envelope()
        bad["kind"] = "command"
        bad["subject"] = "bloodbank.cmd.agent.invocation.skipped"
        with self.assertRaises(validate.ContractViolation):
            validate.assert_contract(bad)

    def test_schema_accepts_an_unresolved_target(self):
        try:
            validate.validate_envelope(envelope(target_agent_id=None, matched_by="none", skip_code="no_route"))
        except validate.ValidationUnavailable:
            self.skipTest("jsonschema is not installed")

    def test_schema_rejects_an_unknown_skip_code(self):
        try:
            with self.assertRaises(validate.EnvelopeInvalid):
                validate.validate_envelope(envelope(skip_code="because"))
        except validate.ValidationUnavailable:
            self.skipTest("jsonschema is not installed")


if __name__ == "__main__":
    unittest.main()
