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

    def test_bus_failure_message_matches_the_exit_code_in_both_modes(self):
        """The log line must describe what THIS invocation did, not the default.

        --strict used to print "failing open" and then exit 1. Anyone reading
        logs during a bus outage would conclude the event was dropped harmlessly
        at the exact moment the caller had in fact died -- the message asserted
        the opposite of the exit code.
        """
        strict = run(BB_EMIT, "--type", LIVE[0], "--data", "{}", "--port", "1", "--strict")
        self.assertEqual(strict.returncode, 1)
        self.assertIn("--strict", strict.stderr)
        self.assertIn("exiting 1", strict.stderr)
        self.assertNotIn("failing open", strict.stderr)

        openmode = run(BB_EMIT, "--type", LIVE[0], "--data", "{}", "--port", "1")
        self.assertEqual(openmode.returncode, 0)
        self.assertIn("failing open", openmode.stderr)
        self.assertIn("exiting 0", openmode.stderr)

        # Both modes must say the event did not make it -- that part is common.
        for r in (strict, openmode):
            self.assertIn("NOT published", r.stderr)


class TestBannedTokenLessonSurvivesTheAllowlist(unittest.TestCase):
    """A banned token in an allowlisted position must still teach §9.

    The allowlist checks necessarily run before assert_banned_tokens, so
    `bloodbank.evt.repo.claude.created` was refused with only "entity 'claude'
    not in allowlist (§7)". Correct, and the least useful half: it invites the
    producer to try a different entity name when the real rule is that identity
    is never a token at all. That lesson is the whole point of the migration.
    """

    BANNED_IN_ENTITY = "bloodbank.evt.repo.claude.created"
    BANNED_IN_DOMAIN = "bloodbank.evt.claude.session.started"
    BANNED_IN_ACTION = "bloodbank.evt.repo.decision.claude"

    def _assert_teaches_rule_9(self, r, token):
        self.assertEqual(r.returncode, 1, f"not refused: {r.stdout}{r.stderr}")
        self.assertIn("§9", r.stderr)
        self.assertIn(token, r.stderr)
        self.assertIn("actor", r.stderr)

    def test_entity_position_reports_the_banned_token_rule(self):
        r = run(BB, "emit", "--check", "--type", self.BANNED_IN_ENTITY, "--data", "{}")
        self._assert_teaches_rule_9(r, "claude")
        # The positional reason stays -- §9 is added to it, not swapped for it.
        self.assertIn("(§7)", r.stderr)
        self.assertIn("BANNED TOKEN", r.stderr)

    def test_domain_position_reports_the_banned_token_rule(self):
        r = run(BB, "emit", "--check", "--type", self.BANNED_IN_DOMAIN, "--data", "{}")
        self._assert_teaches_rule_9(r, "claude")
        self.assertIn("(§6)", r.stderr)

    def test_action_position_already_reported_it_and_still_does(self):
        r = run(BB, "emit", "--check", "--type", self.BANNED_IN_ACTION, "--data", "{}")
        self._assert_teaches_rule_9(r, "claude")

    def test_every_banned_token_teaches_rule_9_from_an_entity_position(self):
        sys.path.insert(0, str(ROOT / "services" / "agent-hooks"))
        from core import validate  # noqa: PLC0415
        for token in sorted(validate.BANNED_TOKENS):
            with self.subTest(token=token):
                r = run(BB, "emit", "--check", "--type",
                        f"bloodbank.evt.repo.{token}.created", "--data", "{}")
                self._assert_teaches_rule_9(r, token)

    def test_an_ordinary_unknown_entity_is_not_mislabelled_as_banned(self):
        r = run(BB, "emit", "--check", "--type",
                "bloodbank.evt.repo.sprocket.created", "--data", "{}")
        self.assertEqual(r.returncode, 1)
        self.assertIn("(§7)", r.stderr)
        self.assertNotIn("BANNED TOKEN", r.stderr)

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
