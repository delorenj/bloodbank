"""Pin the discoverability surface: `bb contract` and `bb emit --check`.

The migration these guard against: nine event families were invented, shipped
and never received, because the only way to learn a token was illegal was for
the event to silently not arrive. The allowlists lived in core/validate.py and
nothing printed them; bb-emit counted tokens and published without ever asking
the validator. These tests fail if either regression returns -- if the
vocabulary stops being printable/parseable, or if a contract-invalid envelope
stops exiting non-zero.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BB = ROOT / "cli" / "bb.py"
BB_EMIT = ROOT / "bin" / "bb-emit"

# The three families that are live, protected, and publish today.
LIVE = (
    "bloodbank.repo.decision.recorded",
    "bloodbank.repo.intake.triaged",
    "bloodbank.repo.task.created",
)
# Deleted because nothing published or consumed them. Each must stay refused.
PHANTOM_EVENTS = (
    "bloodbank.evt.repo.code.committed",
    "bloodbank.evt.repo.pr.opened",
    "bloodbank.evt.repo.review.completed",
    "bloodbank.evt.repo.pr.merged",
)
PHANTOM_COMMANDS = ("bloodbank.cmd.agent.task.assign",)


def run(*argv, stdin=""):
    return subprocess.run(
        [sys.executable, *[str(a) for a in argv]],
        input=stdin, text=True, capture_output=True, check=False,
    )


class TestBbContract(unittest.TestCase):
    def test_text_output_lists_all_four_vocabularies(self):
        r = run(BB, "contract")
        self.assertEqual(r.returncode, 0, r.stderr)
        for heading in ("DOMAINS", "ENTITIES", "EVENT ACTIONS", "COMMAND ACTIONS"):
            self.assertIn(heading, r.stdout)

    def test_json_is_parseable_and_matches_validate_py(self):
        r = run(BB, "contract", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)

        sys.path.insert(0, str(ROOT / "services" / "agent-hooks"))
        from core import validate

        # The CLI is a window onto validate.py, not a second copy of it.
        self.assertEqual(set(payload["domains"]), set(validate.ALLOWED_DOMAINS))
        self.assertEqual(set(payload["entities"]), set(validate.ALLOWED_ENTITIES))
        self.assertEqual(set(payload["event_actions"]), set(validate.EVENT_ACTIONS))
        self.assertEqual(set(payload["command_actions"]), set(validate.COMMAND_ACTIONS))
        self.assertEqual(set(payload["banned_tokens"]), set(validate.BANNED_TOKENS))

    def test_json_carries_the_grammar_so_generators_need_no_literals(self):
        payload = json.loads(run(BB, "contract", "--json").stdout)
        g = payload["grammar"]
        self.assertEqual(g["type"], "bloodbank.<domain>.<entity>.<action>")
        self.assertEqual(g["subject"], "bloodbank.<kind>.<domain>.<entity>.<action>")
        self.assertEqual(g["kind_markers"], {"event": "evt", "command": "cmd", "reply": "rpy"})


class TestEmitCheck(unittest.TestCase):
    def test_live_families_pass_and_print_the_computed_binding(self):
        for t in LIVE:
            with self.subTest(type=t):
                r = run(BB, "emit", "--check", "--type", t, "--data", "{}")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(f"subject     bloodbank.evt.{t[len('bloodbank.'):]}", r.stdout)
                self.assertIn(f"schemaref   {t}.v1", r.stdout)
                self.assertIn(f"dataschema  apicurio://holyfields/{t}/versions/1", r.stdout)

    def test_subject_form_is_accepted_and_resolves_kind(self):
        r = run(BB, "emit", "--check", "--type", "bloodbank.cmd.agent.invocation.start",
                "--data", "{}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kind        command", r.stdout)
        self.assertIn("type        bloodbank.agent.invocation.start", r.stdout)
        self.assertIn("subject     bloodbank.cmd.agent.invocation.start", r.stdout)

    def test_phantom_families_are_refused_with_the_allowlist_reason(self):
        for t in PHANTOM_EVENTS + PHANTOM_COMMANDS:
            with self.subTest(type=t):
                r = run(BB, "emit", "--check", "--type", t, "--data", "{}")
                self.assertEqual(r.returncode, 1, f"{t} was not refused: {r.stdout}{r.stderr}")
                self.assertIn("allowlist", r.stderr)

    def test_check_never_publishes(self):
        # Port 1 is unbindable; a --check that reached the bus would error here.
        r = run(BB, "emit", "--check", "--type", LIVE[0], "--data", "{}", "--port", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing published", r.stderr)


class TestBbEmitFailsClosedOnCallerErrors(unittest.TestCase):
    """A contract violation is the caller's bug: rc=1 regardless of --strict."""

    def test_contract_violation_is_rc1_without_strict(self):
        r = run(BB_EMIT, "--type", "bloodbank.cmd.agent.task.assign", "--data", "{}")
        self.assertEqual(r.returncode, 1)
        self.assertIn("allowlist", r.stderr)

    def test_retired_version_shape_still_rejected(self):
        r = run(BB_EMIT, "--check", "--type",
                "bloodbank.v1.audio.transcription.completed", "--data", "{}")
        self.assertEqual(r.returncode, 1)

    def test_bus_failure_still_fails_open(self):
        # The distinction that must survive: a dead bus must not take the
        # caller down, even though a bad name now does.
        r = run(BB_EMIT, "--type", LIVE[0], "--data", "{}", "--port", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("failing open", r.stderr)

    def test_bus_failure_is_rc1_under_strict(self):
        r = run(BB_EMIT, "--type", LIVE[0], "--data", "{}", "--port", "1", "--strict")
        self.assertEqual(r.returncode, 1)

    def test_emitted_envelope_carries_an_actor(self):
        r = run(BB, "emit", "--check", "--type", LIVE[0], "--data", "{}")
        self.assertIn('"agent_id":', r.stdout)
        self.assertIn("validated   yes", r.stdout)


class TestVerifyEnvelopeAdvertisesTheLiveGrammar(unittest.TestCase):
    def test_help_does_not_promise_the_retired_versioned_grammar(self):
        r = run(BB, "verify-envelope", "--help")
        self.assertNotIn("Bloodbank v1 envelope", r.stdout)
        self.assertIn("bloodbank.<domain>.<entity>.<action>", r.stdout)


if __name__ == "__main__":
    unittest.main()
