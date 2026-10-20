"""audio.memo.* is the voice memo journey: five stage facts plus failed.

uploaded -> received -> transcribed -> dispatched -> replied, or failed at any
stage. Every fact must pass both gates every producer answers to: the stdlib
contract (type shape, allowlists, tense) and the JSON Schema.
"""
from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import validate  # noqa: E402

KEY = "inbox/holocene-pm/2026/10/15/20261015T201500Z.ogg"
MEMO_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "s3://holocene-auto/" + KEY))

BASE = {"memo_id": MEMO_ID, "s3_key": KEY, "target_agent_id": "holocene-pm"}
PAYLOADS = {
    "uploaded": {**BASE, "bytes": 18432, "content_type": "audio/ogg", "client_user_agent": "HoloceneAuto/0.2.7", "upload_ms": 41.2},
    "received": {**BASE, "size_bytes": 18432, "content_type": "audio/ogg",
                 "uploaded_at": "2026-10-15T20:15:00Z", "detected_at": "2026-10-15T20:15:09Z", "detect_lag_ms": 9000},
    "transcribed": {**BASE, "transcript_text": "summarize recent work", "transcript_chars": 21, "word_count": 3,
                    "audio_duration_seconds": 1.5, "processing_seconds": 42.7, "model": "large-v3",
                    "backend": "faster-whisper", "language": "en"},
    "dispatched": {**BASE, "command_id": str(uuid.uuid4()), "idempotency_key": str(uuid.uuid4()),
                   "command_subject": "bloodbank.cmd.agent.invocation.start", "thread_id": MEMO_ID,
                   "prompt_chars": 21, "dispatch_ms": 120},
    "replied": {**BASE, "reply_s3_key": "replies/2026/10/15/201612Z-holocene-pm-abcd1234.ogg",
                "reply_text": "Done.", "reply_chars": 5, "tts_seconds": 3.2, "journey_seconds": 61.0},
    "played": {"memo_id": MEMO_ID, "target_agent_id": "holocene-pm", "phase": "started", "played_seconds": None,
               "reply_s3_key": "replies/2026/10/15/201612Z-holocene-pm-" + MEMO_ID + ".ogg", "reply_age_ms": 4100},
    "failed": {**BASE, "stage": "agent_reply", "error_code": "timeout",
               "error_message": "no reply after 600s", "retryable": False, "attempt": 1},
    # the watcher spoke a progress update for the car while the agent still works
    "narrated": {**BASE, "command_id": str(uuid.uuid4()), "seq": 3, "narration": 1,
                 "progress_s3_key": f"progress/2026/10/15/{MEMO_ID}-3.ogg",
                 "spoken_text": "Holocene PM: checking the board.", "elapsed_seconds": 41.5,
                 "tts_seconds": 2.1, "tts_engine": "voxcpm", "voice": "rick", "audio_bytes": 18031},
    # the first sentence-chunk of a long reply went up before the full file
    "voiced": {**BASE, "command_id": str(uuid.uuid4()), "chunk": 1, "chunks": 3,
               "reply_s3_key": "replies/2026/10/15/201612Z-holocene-pm-" + MEMO_ID + ".ogg",
               "chunk_s3_key": "replies/2026/10/15/201612Z-holocene-pm-" + MEMO_ID + "-c01of03.opus",
               "spoken_text": "Holocene PM. The board has three tickets.", "spoken_chars": 41, "tts_seconds": 2.7,
               "tts_engine": "voxcpm", "voice": "rick", "audio_bytes": 15145, "since_reply_seconds": 3.1,
               "since_agent_done_seconds": 3.4, "late": False},
    # the car's speech arbiter started the instant acknowledgement
    "notified": {"memo_id": MEMO_ID, "s3_key": KEY, "target_agent_id": "holocene-pm", "utterance": "ack",
                 "decision": "started", "reason": None, "utterance_id": "u-1", "device_at": "2026-10-15T20:15:00.4Z",
                 "since_stop_ms": 420, "recording": False, "client_user_agent": "HoloceneAuto/0.9.0"},
}


def envelope(action, **data_overrides):
    t = f"bloodbank.audio.memo.{action}"
    data = dict(PAYLOADS[action])
    data.update(data_overrides)
    return {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": "urn:33god:service:inbox-watcher",
        "type": t,
        "subject": f"bloodbank.evt.audio.memo.{action}",
        "time": "2026-10-15T20:15:09Z",
        "datacontenttype": "application/json",
        "dataschema": f"apicurio://holyfields/{t}/versions/1",
        "correlationid": MEMO_ID,
        "causationid": None,
        "producer": "inbox-watcher",
        "service": "inbox-watcher",
        "domain": "audio",
        "schemaref": f"{t}.v1",
        "kind": "event",
        "actor": {"type": "service", "agent_id": "bloodbank.service.inbox-watcher"},
        "ordering_key": f"memo:{MEMO_ID}",
        "data": data,
    }


class MemoContract(unittest.TestCase):
    def test_every_stage_passes_the_contract(self):
        for action in PAYLOADS:
            with self.subTest(action=action):
                validate.assert_contract(envelope(action))

    def test_every_stage_passes_the_schema(self):
        for action in PAYLOADS:
            with self.subTest(action=action):
                try:
                    validate.validate_envelope(envelope(action))
                except validate.ValidationUnavailable:
                    self.skipTest("jsonschema is not installed")

    def test_unknown_action_fails(self):
        bad = envelope("received")
        bad["type"] = "bloodbank.audio.memo.exploded"
        bad["subject"] = "bloodbank.evt.audio.memo.exploded"
        with self.assertRaises(validate.ContractViolation):
            validate.assert_contract(bad)

    def test_memo_is_not_an_entity_of_other_domains_by_accident(self):
        self.assertIn("memo", validate.ALLOWED_ENTITIES)
        for action in ("uploaded", "transcribed", "dispatched", "replied", "played"):
            self.assertIn(action, validate.EVENT_ACTIONS)
            self.assertNotIn(action, validate.COMMAND_ACTIONS)

    def test_schema_rejects_missing_memo_id(self):
        bad = envelope("received")
        del bad["data"]["memo_id"]
        try:
            with self.assertRaises(validate.EnvelopeInvalid):
                validate.validate_envelope(bad)
        except validate.ValidationUnavailable:
            self.skipTest("jsonschema is not installed")

    def test_schema_rejects_unknown_failed_stage(self):
        try:
            with self.assertRaises(validate.EnvelopeInvalid):
                validate.validate_envelope(envelope("failed", stage="vibes"))
        except validate.ValidationUnavailable:
            self.skipTest("jsonschema is not installed")

    def test_playback_failure_is_a_legal_failed_stage(self):
        try:
            validate.validate_envelope(envelope("failed", stage="playback", retryable=True))
        except validate.ValidationUnavailable:
            self.skipTest("jsonschema is not installed")

    def test_device_side_record_and_upload_failures_are_legal_stages(self):
        for stage in ("record", "upload"):
            try:
                validate.validate_envelope(envelope("failed", stage=stage, retryable=True))
            except validate.ValidationUnavailable:
                self.skipTest("jsonschema is not installed")

    def test_a_deliberate_cancel_is_a_legal_failed_stage(self):
        for after in (False, True):
            try:
                validate.validate_envelope(envelope("failed", stage="cancelled", error_code="cancelled",
                                                    retryable=False, after_dispatch=after))
            except validate.ValidationUnavailable:
                self.skipTest("jsonschema is not installed")

    def test_after_dispatch_must_be_a_boolean(self):
        try:
            with self.assertRaises(validate.EnvelopeInvalid):
                validate.validate_envelope(envelope("failed", stage="cancelled", retryable=False, after_dispatch="yes"))
        except validate.ValidationUnavailable:
            self.skipTest("jsonschema is not installed")

    def test_played_needs_a_phase(self):
        bad = envelope("played")
        del bad["data"]["phase"]
        try:
            with self.assertRaises(validate.EnvelopeInvalid):
                validate.validate_envelope(bad)
        except validate.ValidationUnavailable:
            self.skipTest("jsonschema is not installed")

    def test_memo_id_is_deterministic_per_key(self):
        again = str(uuid.uuid5(uuid.NAMESPACE_URL, "s3://holocene-auto/" + KEY))
        self.assertEqual(MEMO_ID, again)


if __name__ == "__main__":
    unittest.main()
