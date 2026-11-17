"""Unit tests for the turn-enricher: signal gate boundary, p_null acceptance,
envelope shape (causationid = source id), noise/subagent/empty-reply skips,
and extractor failure fallbacks.

    python3 -m pytest test_main.py -v
    uv run --with pytest python3 -m pytest test_main.py -v

Mirrors services/ntfy-router/test_main.py style (fakes, no network).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import unittest

import main


def run(coro):
    return asyncio.run(coro)


class FakeGate:
    """Scripted Jev gate: replies are (status, body, retry_after)."""

    def __init__(self, replies=None, error: Exception | None = None) -> None:
        self.replies = list(replies or [])
        self.error = error
        self.calls: list[str] = []

    async def __call__(self, reply_text: str):
        self.calls.append(reply_text)
        if self.error:
            raise self.error
        return self.replies.pop(0) if self.replies else (200, {"answers": {"signal": {"noul": 0.9}}}, None)


class FakeExtract:
    """Scripted Clairic extractor: replies are (status, body, retry_after).

    The default body mirrors the real wire shape: result.answers.<field>, with
    p_null only on null-proposition fields (text), not on lists."""

    def __init__(self, replies=None, error: Exception | None = None) -> None:
        self.replies = list(replies or [])
        self.error = error
        self.calls: list[str] = []

    async def __call__(self, reply_text: str):
        self.calls.append(reply_text)
        if self.error:
            raise self.error
        body = {"result": {"answers": {
            "action": {"value": "Fixed flakey test", "p_null": 0.1},
            "artifacts": {"value": ["HOLOC-12", "services/ntfy-router/main.py"]},
            "blocker": {"value": None, "p_null": 0.9},
        }}}
        return self.replies.pop(0) if self.replies else (200, body, None)


class FakePublish:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def __call__(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, json.loads(payload.decode("utf-8"))))


SOURCE_ID = "11111111-2222-3333-4444-555555555555"
CORRELATION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def source_envelope(**data) -> dict:
    base = {
        "thread_id": "thr_123",
        "turn_id": "turn_456",
        "outcome": "completed",
        "reply_text": "Fixed the flakey cancel test in HOLOC-12; mise run validate:schemas green.",
        "working_directory": "/home/delorenj/code/33GOD/bloodbank",
    }
    base.update(data)
    return {
        "id": SOURCE_ID,
        "type": "bloodbank.conversation.turn.completed",
        "correlationid": CORRELATION_ID,
        "actor": {"type": "agent", "agent_id": "hermes-33god-pm", "cli": "hermes"},
        "data": base,
    }


def make(gate=None, extract=None) -> tuple[main.TurnEnricher, FakeGate, FakeExtract, FakePublish]:
    gate = gate or FakeGate()
    extract = extract or FakeExtract()
    publish = FakePublish()
    enricher = main.TurnEnricher(gate=gate, extract=extract, publish=publish)
    return enricher, gate, extract, publish


class TestSkips(unittest.TestCase):
    def test_subagent_skipped(self):
        enricher, gate, extract, publish = make()
        result = run(enricher.handle(source_envelope(platform="subagent")))
        self.assertEqual(result, "skipped_subagent")
        self.assertEqual(gate.calls, [])
        self.assertEqual(publish.published, [])
        self.assertEqual(enricher.stats["skipped_subagent"], 1)

    def test_empty_reply_skipped(self):
        enricher, gate, _, publish = make()
        result = run(enricher.handle(source_envelope(reply_text="   ")))
        self.assertEqual(result, "skipped_no_reply")
        self.assertEqual(gate.calls, [])
        self.assertEqual(publish.published, [])

    def test_missing_reply_skipped(self):
        enricher, _, _, publish = make()
        event = source_envelope()
        del event["data"]["reply_text"]
        self.assertEqual(run(enricher.handle(event)), "skipped_no_reply")
        self.assertEqual(publish.published, [])


class TestSignalGate(unittest.TestCase):
    def test_noise_below_threshold_skipped(self):
        gate = FakeGate([(200, {"answers": {"signal": {"noul": 0.49}}}, None)])
        extract = FakeExtract()
        enricher, _, _, publish = make(gate=gate, extract=extract)
        result = run(enricher.handle(source_envelope()))
        self.assertEqual(result, "noise")
        self.assertEqual(extract.calls, [])  # clairic never runs on noise
        self.assertEqual(publish.published, [])

    def test_at_threshold_is_signal(self):
        gate = FakeGate([(200, {"answers": {"signal": {"noul": 0.5}}}, None)])
        enricher, _, extract, publish = make(gate=gate)
        result = run(enricher.handle(source_envelope()))
        self.assertEqual(result, "annotated")
        self.assertEqual(len(extract.calls), 1)
        self.assertEqual(len(publish.published), 1)

    def test_gate_failure_runs_clairic_anyway(self):
        gate = FakeGate(error=RuntimeError("jev down"))
        enricher, _, extract, publish = make(gate=gate)
        result = run(enricher.handle(source_envelope()))
        self.assertEqual(result, "annotated")
        self.assertEqual(len(extract.calls), 1)
        subject, event = publish.published[0]
        self.assertIsNone(event["data"]["signal"])

    def test_gate_non2xx_runs_clairic_anyway(self):
        gate = FakeGate([(400, None, None)])
        enricher, _, extract, publish = make(gate=gate)
        self.assertEqual(run(enricher.handle(source_envelope())), "annotated")
        self.assertEqual(len(extract.calls), 1)
        self.assertEqual(len(publish.published), 1)


class TestClairicFacts(unittest.TestCase):
    def test_p_null_boundary(self):
        answers = {"answers": {
            "action": {"value": "Did a thing", "p_null": 0.49},
            "artifacts": {"value": ["a"], "p_null": 0.5},   # at threshold: rejected
            "blocker": {"value": "db locked", "p_null": 0.99},
        }}
        facts = main.clairic_facts(answers)
        self.assertEqual(facts["action"], "Did a thing")
        self.assertEqual(facts["artifacts"], [])
        self.assertIsNone(facts["blocker"])

    def test_malformed_body_yields_empty_facts(self):
        for body in (None, {}, {"answers": "nope"}, {"answers": {"action": {"value": 42, "p_null": 0.1}}}):
            facts = main.clairic_facts(body)
            self.assertEqual(facts, {"action": None, "artifacts": [], "blocker": None})

    def test_artifacts_capped_at_four(self):
        body = {"answers": {"artifacts": {"value": ["a", "b", "c", "d", "e", "f"], "p_null": 0.1}}}
        self.assertEqual(main.clairic_facts(body)["artifacts"], ["a", "b", "c", "d"])

    def test_clairic_failure_publishes_without_facts(self):
        extract = FakeExtract(error=RuntimeError("cloudflare down"))
        enricher, _, _, publish = make(extract=extract)
        result = run(enricher.handle(source_envelope()))
        self.assertEqual(result, "annotated")
        _, event = publish.published[0]
        self.assertIsNone(event["data"]["action"])
        self.assertEqual(event["data"]["artifacts"], [])
        self.assertIsNone(event["data"]["blocker"])

    def test_clairic_non2xx_publishes_without_facts(self):
        extract = FakeExtract([(503, None, None)])
        enricher, _, _, publish = make(extract=extract)
        self.assertEqual(run(enricher.handle(source_envelope())), "annotated")
        self.assertIsNone(publish.published[0][1]["data"]["action"])


class TestPushback(unittest.TestCase):
    def test_429_honors_retry_after_and_pauses(self):
        gate = FakeGate([(429, None, "30")])
        enricher, _, _, publish = make(gate=gate)
        self.assertEqual(run(enricher.handle(source_envelope())), "skipped_paused")
        self.assertTrue(enricher.paused())
        # While paused, the next turn is counted, not processed.
        self.assertEqual(run(enricher.handle(source_envelope())), "skipped_paused")
        self.assertEqual(enricher.stats["skipped_paused"], 2)
        self.assertEqual(publish.published, [])

    def test_429_without_retry_after_pauses_60s(self):
        gate = FakeGate([(429, None, None)])
        enricher, _, _, _ = make(gate=gate)
        run(enricher.handle(source_envelope()))
        self.assertAlmostEqual(enricher.paused_until - enricher.clock(), main.BACKOFF_SECONDS, delta=1.0)

    def test_transport_error_pauses(self):
        gate = FakeGate([(0, None, None)])
        enricher, _, _, _ = make(gate=gate)
        self.assertEqual(run(enricher.handle(source_envelope())), "skipped_paused")
        self.assertTrue(enricher.paused())

    def test_litellm_rpm_429_pauses_for_its_retry_after(self):
        # LiteLLM's ProxyRateLimitError: 429 + retry-after 60 (window size).
        gate = FakeGate([(429, None, "60")])
        enricher, _, extract, publish = make(gate=gate)
        self.assertEqual(run(enricher.handle(source_envelope())), "skipped_paused")
        self.assertAlmostEqual(enricher.paused_until - enricher.clock(), 60.0, delta=1.0)
        self.assertEqual(extract.calls, [])
        self.assertEqual(publish.published, [])
        self.assertEqual(enricher.stats["http_429"], 1)

    def test_litellm_budget_exceeded_422_does_not_pause_and_publishes_without_signal(self):
        # LiteLLM budget_exceeded answers 422 (litellm.budget_exceeded_status_code):
        # not pushback, so the turn is still published, signal null.
        gate = FakeGate([(422, None, None)])
        enricher, _, _, publish = make(gate=gate)
        self.assertEqual(run(enricher.handle(source_envelope())), "annotated")
        self.assertFalse(enricher.paused())
        self.assertIsNone(publish.published[0][1]["data"]["signal"])

    def test_bad_key_401_does_not_pause(self):
        gate = FakeGate([(401, None, None)])
        enricher, _, _, publish = make(gate=gate)
        self.assertEqual(run(enricher.handle(source_envelope())), "annotated")
        self.assertFalse(enricher.paused())
        self.assertIsNone(publish.published[0][1]["data"]["signal"])


class TestEnvelope(unittest.TestCase):
    def test_annotated_envelope_shape(self):
        enricher, _, _, publish = make()
        run(enricher.handle(source_envelope()))
        subject, event = publish.published[0]
        self.assertEqual(subject, main.PUBLISH_SUBJECT)
        self.assertEqual(event["type"], "bloodbank.conversation.turn.annotated")
        self.assertEqual(event["kind"], "event")
        self.assertEqual(event["domain"], "conversation")
        self.assertEqual(event["causationid"], SOURCE_ID)
        self.assertEqual(event["correlationid"], CORRELATION_ID)
        self.assertEqual(event["ordering_key"], f"{SOURCE_ID}.annotated")
        self.assertEqual(event["source"], "hermes://service/turn-enricher")
        self.assertEqual(event["producer"], "turn-enricher")
        self.assertEqual(event["service"], "turn-enricher")
        self.assertEqual(event["actor"], {"type": "agent", "agent_id": "hermes-33god-pm", "cli": "hermes"})

    def test_annotated_data_shape(self):
        enricher, _, _, publish = make()
        run(enricher.handle(source_envelope()))
        data = publish.published[0][1]["data"]
        self.assertEqual(data["target_event_id"], SOURCE_ID)
        self.assertEqual(data["thread_id"], "thr_123")
        self.assertEqual(data["turn_id"], "turn_456")
        self.assertEqual(data["outcome"], "completed")
        self.assertEqual(data["signal"], 0.9)
        self.assertEqual(data["action"], "Fixed flakey test")
        self.assertEqual(data["artifacts"], ["HOLOC-12", "services/ntfy-router/main.py"])
        self.assertIsNone(data["blocker"])  # p_null 0.9 rejected
        self.assertEqual(data["extractors"], {"jev": "jev-latest", "clairic": "@cf/cloudflare/clairic"})
        self.assertEqual(data["working_directory"], "/home/delorenj/code/33GOD/bloodbank")


class TestPayloads(unittest.TestCase):
    def test_jev_payload_shape(self):
        payload = main.jev_payload("did the thing")
        self.assertEqual(payload["model"], "jev-latest")
        question = payload["questions"]["signal"]
        self.assertEqual(question["type"], "noul")
        self.assertIn("substantive work output", question["instructions"])
        self.assertIn("true", question["criteria"])
        self.assertIn("false", question["criteria"])
        self.assertEqual(payload["state"], "Agent turn reply:\ndid the thing")

    def test_clairic_url_uses_account_id(self):
        url = main.clairic_url("acct123")
        self.assertEqual(
            url,
            "https://api.cloudflare.com/client/v4/accounts/acct123/ai/run/@cf/cloudflare/clairic",
        )

    def test_jev_signal_parsing(self):
        self.assertEqual(main.jev_signal({"answers": {"signal": {"noul": 0.42}}}), 0.42)
        self.assertIsNone(main.jev_signal({}))
        self.assertIsNone(main.jev_signal({"answers": {"signal": {"noul": "x"}}}))

    def test_retry_after_parsing(self):
        self.assertEqual(main.retry_after_seconds("12"), 12.0)
        self.assertIsNone(main.retry_after_seconds(None))
        self.assertIsNone(main.retry_after_seconds("not a date"))


def make_jev_only(gate=None) -> tuple[main.TurnEnricher, FakeGate, FakePublish]:
    gate = gate or FakeGate()
    publish = FakePublish()
    return main.TurnEnricher(gate=gate, extract=None, publish=publish), gate, publish


class TestClairicDisabled(unittest.TestCase):
    """CLAIRIC_ENABLED=false: Jev-only annotation, no Cloudflare account needed."""

    def test_annotates_with_signal_only_and_names_only_jev(self):
        enricher, gate, publish = make_jev_only()
        self.assertEqual(run(enricher.handle(source_envelope())), "annotated")
        self.assertEqual(len(gate.calls), 1)
        data = publish.published[0][1]["data"]
        self.assertEqual(data["signal"], 0.9)
        self.assertEqual(data["extractors"], {"jev": "jev-latest"})
        self.assertIsNone(data["action"])
        self.assertEqual(data["artifacts"], [])
        self.assertIsNone(data["blocker"])
        self.assertEqual(data["target_event_id"], SOURCE_ID)

    def test_noise_still_dropped(self):
        gate = FakeGate([(200, {"answers": {"signal": {"noul": 0.1}}}, None)])
        enricher, _, publish = make_jev_only(gate)
        self.assertEqual(run(enricher.handle(source_envelope())), "noise")
        self.assertEqual(publish.published, [])

    def test_gate_429_pauses_and_publishes_nothing(self):
        enricher, _, publish = make_jev_only(FakeGate([(429, None, "60")]))
        self.assertEqual(run(enricher.handle(source_envelope())), "skipped_paused")
        self.assertTrue(enricher.paused())
        self.assertEqual(publish.published, [])

    def test_gate_failure_publishes_with_null_signal(self):
        enricher, _, publish = make_jev_only(FakeGate([(422, None, None)]))
        self.assertEqual(run(enricher.handle(source_envelope())), "annotated")
        data = publish.published[0][1]["data"]
        self.assertIsNone(data["signal"])
        self.assertEqual(data["extractors"], {"jev": "jev-latest"})

    def test_explicit_extractors_override(self):
        publish = FakePublish()
        enricher = main.TurnEnricher(gate=FakeGate(), extract=FakeExtract(), publish=publish,
                                     extractors={"jev": "x"})
        run(enricher.handle(source_envelope()))
        self.assertEqual(publish.published[0][1]["data"]["extractors"], {"jev": "x"})


class TestConfig(unittest.TestCase):
    def _import_with_env(self, **env) -> dict:
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("JEV_", "CLAIRIC_", "CF_"))}
        clean.update(env)
        out = subprocess.run(
            [sys.executable, "-c",
             "import json, main; print(json.dumps({'url': main.JEV_URL, 'model': main.JEV_MODEL,"
             " 'clairic': main.CLAIRIC_ENABLED}))"],
            cwd=os.path.dirname(os.path.abspath(__file__)), env=clean,
            capture_output=True, text=True, check=True,
        )
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_defaults_point_at_the_jev_ingress_not_openrouter(self):
        cfg = self._import_with_env()
        self.assertEqual(cfg["url"], "https://jev.delo.sh/typesafe/v1/systemone")
        self.assertEqual(cfg["model"], "jev-latest")
        self.assertTrue(cfg["clairic"])

    def test_clairic_flag_parsing(self):
        for raw in ("false", "FALSE", "0", "no", "off"):
            self.assertFalse(self._import_with_env(CLAIRIC_ENABLED=raw)["clairic"], raw)
        for raw in ("true", "1", "yes", "", "  "):
            self.assertTrue(self._import_with_env(CLAIRIC_ENABLED=raw)["clairic"], raw)


if __name__ == "__main__":
    unittest.main()
