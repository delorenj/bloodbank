#!/usr/bin/env python3
"""Project artifact sidecar + vault projector adversarial test suite.

Stdlib unittest (repo constraint: no pytest in this environment). Organized
by the defect classes the parent review demanded regression cover for:

  SchemaTests          canonical schema + invariants, negative probes
  DiscoveryTests       producer shapes; narrow qualification; no binary
                       fabrication; denylist; thread-scoped mermaid identity;
                       delivered-marker only with explicit producer intent
  PathSafetyTests      traversal, symlink escapes (checked on ORIGINAL
                       components), denied fragments, allowlist
  StoreTests           strict sha refs (traversal/uppercase refused), safe
                       fd walks, bounded reads, corrupt preexisting blob
                       refused, no-clobber writes, private perms, dir fsync
  PipelineTests        publish (enqueued vs published semantics), duplicate
                       replay, stale-replay cannot revert revision, revision
                       chain, hold flow with hold_id, worktree attribution,
                       outbox retry/bound/backoff/exhaustion/retry-CLI,
                       crash-restart atomicity, idle-timer semantics
  ProjectorTests       full-schema pre-validation, deadletter on malformed,
                       durable unmapped holds (not RAM), date folders from
                       source-event time in the configured tz, deterministic
                       collision suffixes with ACTUAL path recorded, no
                       overwrite at any race, crash-after-file-before-DB
                       replay, render digest/length validation + extension
                       by media type, symlink defense in every component,
                       status nonzero on unresolved holds
  RegistryTests        deepest repo wins, planned+ok indexed, broken index
                       excluded, payload validation, slug/name consistency
  WorktreeTests        git common-dir attribution: worktree of enrolled repo
                       attributes to that repo, never the umbrella; foreign
                       nested repo is held

FakeTransportTests (same file) drive main.Runtime against an in-memory fake
JetStream, proving the ack/nak/drain/idle-retry contract end to end.

Run: python3 services/artifact-sidecar/test_sidecar.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import stat as stat_module
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]  # worktree root (services/<svc>/)
_HOOKS = HERE.parents[1] / "agent-hooks"
for _p in (str(HERE), str(_HOOKS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("BLOODBANK_SCHEMAS_DIR", str(REPO / "schemas"))

import artifact_lib
import discovery
from artifact_lib import (
    PathViolation,
    ProjectRegistry,
    RegistryError,
    StoreError,
    artifact_id_for,
    assert_readable_capture_path,
    denied_path,
    get_blob,
    put_blob,
    safe_resolve_under_root,
    sha256_bytes,
)
from sidecar import Disposition, Sidecar, SidecarState

sys.path.insert(0, str(REPO / "services" / "artifact-vault-projector"))
import main as projector_main  # noqa: E402

from core.validate import ContractViolation, validate_envelope  # noqa: E402


def registry_payload(tmp: Path, *, index_ok: bool = True) -> dict:
    return {
        "schema_version": 1,
        "projects": {
            "bb": {
                "name": "Bloodbank",
                "slug": "bb",
                "project_id": "bb",
                "repo_path": str(tmp / "repos" / "bb"),
                "status": "planned",
            },
            "other": {
                "name": "Other Project",
                "slug": "other",
                "project_id": "other",
                "repo_path": str(tmp / "repos" / "other"),
                "status": "active",
            },
            "broken": {
                "name": "Broken Index",
                "slug": "broken",
                "project_id": "broken",
                "repo_path": str(tmp / "repos" / "broken"),
                "status": "active",
            },
        },
        "__registry_status": {
            "bb": {"status": "ok"},
            "other": {"status": "ok"},
            "broken": {"status": "error", "error": "manifest unreadable"},
        } if index_ok else {},
    }


def tool_completed_envelope(
    *,
    event_id: str | None = None,
    path: str | None = None,
    content: str | None = None,
    tool: str = "Write",
    outcome: str = "success",
    working_directory: str | None = None,
    time: str = "2026-10-16T20:11:00Z",
    delivered: bool = True,
    extra_data: dict | None = None,
) -> dict:
    arguments: dict = {}
    if path is not None:
        arguments["file_path"] = path
    if content is not None:
        arguments["content"] = content
    data: dict = {
        "invocation_id": "abc",
        "tool_call_id": "t1",
        "tool_name": tool,
        "outcome": outcome,
        "arguments": arguments,
        "working_directory": working_directory,
    }
    if delivered:
        # Default: the producer marked the write user-facing. Tests that
        # exercise THE GATE pass delivered=False and assert refusal.
        data["delivered"] = True
    if extra_data:
        data.update(extra_data)
    return {
        "specversion": "1.0",
        "id": event_id or str(uuid.uuid4()),
        "source": "urn:33god:agent:claude-code",
        "type": "bloodbank.agent.tool.completed",
        "subject": "bloodbank.evt.agent.tool.completed",
        "time": time,
        "correlationid": str(uuid.uuid4()),
        "causationid": None,
        "producer": "claude-code",
        "service": "claude-code",
        "domain": "agent",
        "kind": "event",
        "schemaref": "bloodbank.agent.tool.completed.v1",
        "actor": {
            "type": "agent_cli",
            "agent_id": "bloodbank.agent.claude",
            "cli": "claude",
            "provider": "anthropic",
            "model": None,
        },
        "ordering_key": "invocation:abc",
        "data": data,
    }


def turn_completed_envelope(
    *,
    event_id: str | None = None,
    reply_text: str | None = None,
    working_directory: str | None = None,
    time: str = "2026-10-16T21:00:00Z",
    thread_id: str = "thr-1",
    turn_id: str = "turn-9",
    extra_data: dict | None = None,
) -> dict:
    data: dict = {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "outcome": "completed",
        "working_directory": working_directory,
    }
    if reply_text is not None:
        data["reply_text"] = reply_text
        data["reply_length"] = len(reply_text.encode())
    if extra_data:
        data.update(extra_data)
    return {
        "specversion": "1.0",
        "id": event_id or str(uuid.uuid4()),
        "source": "urn:33god:agent:hermes",
        "type": "bloodbank.conversation.turn.completed",
        "subject": "bloodbank.evt.conversation.turn.completed",
        "time": time,
        "correlationid": str(uuid.uuid4()),
        "causationid": None,
        "producer": "hermes-agent:test",
        "service": "hermes-hooks",
        "domain": "conversation",
        "kind": "event",
        "schemaref": "bloodbank.conversation.turn.completed.v1",
        "actor": {"type": "agent_cli", "agent_id": "bloodbank.agent.hermes", "cli": "hermes", "provider": None, "model": None},
        "ordering_key": f"thread:{thread_id}",
        "data": data,
    }


class Harness:
    """One temp world: registry, repos, sidecar state + store. Closes DBs."""

    def __init__(self):
        # /tmp, not $TMPDIR: attribution resolves repo roots via git, and a
        # TMPDIR inside some other checkout (e.g. ~/.claude/tmp) would hijack it.
        self.tmp = Path(tempfile.mkdtemp(prefix="artifact-sidecar-test-", dir="/tmp"))
        (self.tmp / "repos" / "bb" / "docs").mkdir(parents=True)
        (self.tmp / "repos" / "other").mkdir(parents=True)
        (self.tmp / "repos" / "broken").mkdir(parents=True)
        self.registry_path = self.tmp / "registry.json"
        self.registry_path.write_text(json.dumps(registry_payload(self.tmp)))
        self.registry = ProjectRegistry.from_payload(json.loads(self.registry_path.read_text()))
        self.store = artifact_lib.ensure_store_root(self.tmp / "store")
        self.state = SidecarState(self.tmp / "state.sqlite3")
        self.sidecar = Sidecar(state=self.state, store_root=self.store, registry=self.registry)
        self.published: list[dict] = []

    def drain(self) -> list[dict]:
        before = len(self.published)

        def publish(envelope: dict) -> None:
            self.published.append(envelope)

        self.sidecar.drain_outbox(publish)
        return self.published[before:]

    def close(self):
        self.state.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class SchemaTests(unittest.TestCase):
    """Schema-first: the artifact event validates against the real tree."""

    def _harness(self):
        h = Harness()
        self.addCleanup(h.close)
        return h

    def test_valid_envelope_passes_full_schema_validation(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "diagram.md"),
            content="# Plan\n\n```mermaid\ngraph TD\nA-->B\n```\n",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        [d] = h.sidecar.process(env)
        self.assertEqual(d.kind, "enqueued", d.detail)
        [event] = h.drain()
        validate_envelope(event)  # must not raise
        self.assertEqual(event["type"], "bloodbank.project.artifact.created")
        self.assertEqual(event["subject"], "bloodbank.evt.project.artifact.created")
        self.assertEqual(event["ordering_key"], "project:bb")
        self.assertEqual(event["causationid"], env["id"])
        self.assertEqual(event["data"]["origin"]["source_event"]["id"], env["id"])
        self.assertEqual(
            event["data"]["artifact"]["content"]["ref"],
            "sha256:" + event["data"]["artifact"]["sha256"],
        )

    def test_content_ref_must_match_sha(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "x.md"),
            content="hello",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        h.sidecar.process(env)
        [event] = h.drain()
        event["data"]["artifact"]["content"]["ref"] = "sha256:" + "0" * 64
        with self.assertRaises(ContractViolation):
            validate_envelope(event)

    def test_causation_must_equal_source_event_id(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "x.md"),
            content="hello",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        h.sidecar.process(env)
        [event] = h.drain()
        event["causationid"] = str(uuid.uuid4())
        with self.assertRaises(ContractViolation):
            validate_envelope(event)

    def test_absolute_path_in_payload_refused(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "x.md"),
            content="hello",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        h.sidecar.process(env)
        [event] = h.drain()
        event["data"]["artifact"]["title"] = f"seen at {h.tmp}/repos/bb/x.md"
        with self.assertRaises(ContractViolation):
            validate_envelope(event)

    def test_entity_registered_in_allowlist(self):
        from core.validate import assert_type_shape

        domain, entity, action = assert_type_shape("bloodbank.project.artifact.created")
        self.assertEqual((domain, entity, action), ("project", "artifact", "created"))

    def test_render_twin_digest_must_differ(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "x.md"),
            content="hello",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        h.sidecar.process(env)
        [event] = h.drain()
        sha = event["data"]["artifact"]["sha256"]
        event["data"]["artifact"]["render"] = {
            "media_type": "image/svg+xml",
            "sha256": sha,  # same digest: must be refused
            "bytes": 5,
            "content": {"storage": "content-addressed", "ref": f"sha256:{sha}"},
        }
        with self.assertRaises(ContractViolation):
            validate_envelope(event)


class DiscoveryTests(unittest.TestCase):
    def test_claude_write_qualifies(self):
        env = tool_completed_envelope(path="/x/docs/a.md", content="body")
        result = discovery.from_tool_completed(env)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.basis, "agent_tool_write")
        self.assertEqual(result.capture.title, "a.md")
        self.assertEqual(result.capture.media_type, "text/markdown")

    def test_hermes_write_file_underscore_tool_recognized(self):
        # Hermes names its writer `write_file`; underscore normalization is
        # what makes the real Hermes producer visible to the adapter.
        env = tool_completed_envelope(tool="write_file", path="/x/docs/a.md", content="body")
        result = discovery.from_tool_completed(env)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.basis, "agent_tool_write")

    def test_generic_readme_write_without_signal_refused(self):
        # THE GATE: path + inline content + markdown is NOT enough. An
        # ordinary README/docs write with no delivery marker and no
        # configured output tree is process, not a deliverable.
        env = tool_completed_envelope(path="/repo/README.md", content="# readme", delivered=False)
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_config_docs_write_without_signal_refused(self):
        env = tool_completed_envelope(
            path="/repo/docs/architecture.md", content="# arch", delivered=False
        )
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_markerless_bmad_evidence_write_refused(self):
        # Even inside the configured output tree, evidence/ is excluded
        # (process exhaust, not a deliverable).
        env = tool_completed_envelope(
            path="/repo/_bmad_output/evidence/closeout/summary.md",
            content="# evidence",
            working_directory="/repo",
            delivered=False,
        )
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_unsupported_extension_refused(self):
        env = tool_completed_envelope(path="/x/src/main.py", content="print(1)")
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_binary_extension_from_inline_string_refused(self):
        # A string is not a PNG. The adapter must NOT fabricate binary bytes
        # just because the filename says .png.
        env = tool_completed_envelope(path="/x/img/diagram.png", content="\x89PNG not really")
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_pdf_from_inline_string_refused(self):
        env = tool_completed_envelope(path="/x/report.pdf", content="%PDF-fake")
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_config_json_not_a_deliverable_location(self):
        env = tool_completed_envelope(path="/x/config/app.json", content="{}")
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_dotenv_not_a_deliverable_location(self):
        env = tool_completed_envelope(path="/x/.env", content="SECRET=1")
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_secrets_dir_not_a_deliverable_location(self):
        env = tool_completed_envelope(path="/x/secrets/creds.md", content="# nope")
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_scratch_not_a_deliverable_location(self):
        env = tool_completed_envelope(path="/x/scratch/notes.md", content="tmp")
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_repo_relative_denied_fragment_matches_absolute_too(self):
        # wd under the repo, file under repo config/ -> denied by rel path
        env = tool_completed_envelope(
            path="/repo/config/app.json", content="{}", working_directory="/repo"
        )
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_bmad_output_write_qualifies_via_explicit_shape(self):
        # `_bmad_output/` (the spelling THIS repo uses) qualifies through the
        # configured producer output tree even without a delivery marker:
        # planning docs are deliverables by project convention. Note
        # evidence/ is excluded (see test_markerless_bmad_evidence_write_refused).
        env = tool_completed_envelope(
            path="/repo/_bmad_output/planning/roadmap.md",
            content="# roadmap",
            working_directory="/repo",
            delivered=False,
        )
        result = discovery.from_tool_completed(env)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.basis, "agent_tool_write")
        self.assertIn("producer output tree", result.qualification_reason)

    def test_bmad_hyphen_spelling_also_qualifies(self):
        env = tool_completed_envelope(
            path="/repo/_bmad-output/planning/notes.md",
            content="# notes",
            working_directory="/repo",
            delivered=False,
        )
        result = discovery.from_tool_completed(env)
        self.assertIsNotNone(result)

    def test_output_tree_qualifies_from_project_subdirectory_cwd(self):
        # Real traffic: Claude cwd = <repo>/native, writes <repo>/_bmad-output/x.md.
        env = tool_completed_envelope(
            path="/repo/_bmad-output/implementation-artifacts/spec-1.md",
            content="# spec",
            working_directory="/repo/native/godot",
            delivered=False,
        )
        result = discovery.from_tool_completed(env)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("producer output tree", result.qualification_reason)

    def test_output_tree_subdir_cwd_still_excludes_exhaust_and_sibling_paths(self):
        for path in (
            "/repo/_bmad-output/evidence/x.md",  # excluded component
            "/repo/notes/x.md",  # not an output tree
        ):
            env = tool_completed_envelope(
                path=path, content="# x", working_directory="/repo/native", delivered=False
            )
            self.assertIsNone(discovery.from_tool_completed(env), path)

    def test_bmad_output_requires_working_directory_anchor(self):
        # No working_directory -> no project-relative comparison -> the
        # output-tree signal cannot fire (fail-closed, never ambient).
        env = tool_completed_envelope(
            path="/_bmad_output/planning/roadmap.md",
            content="# roadmap",
            working_directory=None,
            delivered=False,
        )
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_delivery_marker_beats_missing_output_tree(self):
        # Explicit producer marker qualifies a write outside any configured
        # output tree: the agent said THIS one is user-facing.
        env = tool_completed_envelope(
            path="/repo/notes/decision.md",
            content="# decision",
            working_directory="/repo",
        )  # delivered=True default
        result = discovery.from_tool_completed(env)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("producer delivery marker", result.qualification_reason)

    def test_edit_without_full_content_does_not_qualify(self):
        env = tool_completed_envelope(tool="edit", path="/x/a.md")
        env["data"]["arguments"] = {"path": "/x/a.md", "newString": "a", "oldString": "b"}
        self.assertIsNone(discovery.from_tool_completed(env))
        self.assertTrue(discovery.write_like_but_unsupported(env) is False)

    def test_opencode_write_without_content_flagged_unsupported(self):
        env = tool_completed_envelope(tool="write", path="/x/a.md", content=None)
        env["data"]["arguments"] = {"path": "/x/a.md"}
        self.assertIsNone(discovery.from_tool_completed(env))
        self.assertTrue(discovery.write_like_but_unsupported(env))

    def test_error_outcome_never_qualifies(self):
        env = tool_completed_envelope(path="/x/a.md", content="x", outcome="error")
        self.assertIsNone(discovery.from_tool_completed(env))

    def test_mermaid_block_identity_is_thread_and_content_scoped(self):
        reply = "Intro.\n```mermaid\ngraph TD\nA-->B\n```\n"
        env = turn_completed_envelope(reply_text=reply)
        [cand] = discovery.from_turn_completed(env)
        self.assertEqual(cand.basis, "conversation_reply_mermaid")
        # logical path is thread-scoped and digest-named, NOT project-wide
        self.assertIn("thr-1", cand.capture.logical_path)
        self.assertTrue(cand.capture.logical_path.startswith("inline/mermaid/thr-1/"))

    def test_same_diagram_same_thread_one_identity(self):
        reply = "```mermaid\ngraph TD\nA-->B\n```\n"
        e1 = turn_completed_envelope(reply_text=reply, thread_id="t1", turn_id="turn-1")
        e2 = turn_completed_envelope(reply_text=reply, thread_id="t1", turn_id="turn-2")
        [c1] = discovery.from_turn_completed(e1)
        [c2] = discovery.from_turn_completed(e2)
        self.assertEqual(c1.capture.logical_path, c2.capture.logical_path)

    def test_same_diagram_different_thread_different_identity(self):
        reply = "```mermaid\ngraph TD\nA-->B\n```\n"
        e1 = turn_completed_envelope(reply_text=reply, thread_id="t1", turn_id="turn-1")
        e2 = turn_completed_envelope(reply_text=reply, thread_id="t2", turn_id="turn-1")
        [c1] = discovery.from_turn_completed(e1)
        [c2] = discovery.from_turn_completed(e2)
        self.assertNotEqual(c1.capture.logical_path, c2.capture.logical_path)

    def test_two_diagrams_one_thread_different_identities(self):
        reply = (
            "```mermaid\ngraph TD\nA-->B\n```\nprose\n"
            "```mermaid\nsequenceDiagram\nA->>B: hi\n```\n"
        )
        env = turn_completed_envelope(reply_text=reply)
        cands = discovery.from_turn_completed(env)
        self.assertEqual(len(cands), 2)
        self.assertNotEqual(cands[0].capture.logical_path, cands[1].capture.logical_path)

    def test_plain_reply_produces_nothing(self):
        env = turn_completed_envelope(reply_text="Just a normal summary, no diagram.")
        self.assertEqual(discovery.from_turn_completed(env), [])

    def test_empty_reply_produces_nothing(self):
        env = turn_completed_envelope(reply_text="")
        self.assertEqual(discovery.from_turn_completed(env), [])

    def test_substantive_reply_without_marker_produces_nothing(self):
        env = turn_completed_envelope(
            reply_text="A" * 500
        )  # long, but no explicit user-facing marker
        self.assertEqual(discovery.from_turn_completed(env), [])

    def test_delivered_marker_captures_substantive_reply(self):
        env = turn_completed_envelope(
            reply_text="# Design decision\nWe chose the durable outbox because… " + "x" * 200,
            extra_data={"delivered": True},
        )
        [cand] = discovery.from_turn_completed(env)
        self.assertEqual(cand.basis, "conversation_reply_delivered")
        self.assertIn(cand.capture.data, env["data"]["reply_text"].encode())

    def test_user_facing_marker_captures_substantive_reply(self):
        env = turn_completed_envelope(
            reply_text="Design rationale " + "y" * 200,
            extra_data={"user_facing": True},
        )
        [cand] = discovery.from_turn_completed(env)
        self.assertEqual(cand.basis, "conversation_reply_delivered")

    def test_deliverables_list_marker_captures(self):
        env = turn_completed_envelope(
            reply_text="Deliverable text " + "z" * 200,
            extra_data={"deliverables": [{"title": "d"}]},
        )
        [cand] = discovery.from_turn_completed(env)
        self.assertEqual(cand.basis, "conversation_reply_delivered")

    def test_marker_on_trivial_reply_captures_nothing(self):
        env = turn_completed_envelope(reply_text="ok", extra_data={"delivered": True})
        self.assertEqual(discovery.from_turn_completed(env), [])

    def test_media_token_in_reply_without_marker_ignored(self):
        # A MEDIA: reference in ordinary reply prose is NOT a delivery: no
        # producer marker, no file read. The gate must be explicit.
        env = turn_completed_envelope(
            reply_text="I saved a chart at MEDIA:/tmp/secret.png earlier."
        )
        self.assertEqual(discovery.from_turn_completed(env), [])


def _safe_tmp_base() -> Path:
    """Temp base whose path is not denylist-refused for local file reads.

    The runtime TMPDIR may itself sit under ~/.hermes (the agent cache) —
    exactly what the capture denylist exists to refuse. Delivered-file tests
    need a base whose absolute path passes denied_path(); fall back through
    the candidates until one does.
    """
    candidates = [Path(tempfile.gettempdir()), Path("/tmp"), Path.home()]
    for base in candidates:
        try:
            probe = Path(tempfile.mkdtemp(prefix="artifact-safe-tmp-", dir=str(base)))
        except OSError:
            continue
        if denied_path(str(probe)) is None:
            return probe
        shutil.rmtree(probe, ignore_errors=True)
    raise RuntimeError("no non-denied temp base available for delivered-file tests")


class DeliveredFileTests(unittest.TestCase):
    """Explicitly delivered LOCAL file capture (MEDIA:/deliverables paths).

    The only path to genuine binary artifacts: real bytes read from disk
    under operator-allowlisted roots — never an inline string, never an
    ambient read, never the .hermes cache.
    """

    def setUp(self):
        self.tmp = _safe_tmp_base()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.deliver_root = self.tmp / "deliver"
        (self.deliver_root / "charts").mkdir(parents=True)
        self.png_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 64  # genuine-ish binary
        self.png_path = self.deliver_root / "charts" / "architecture.png"
        self.png_path.write_bytes(self.png_bytes)
        self.svg_path = self.deliver_root / "charts" / "flow.svg"
        self.svg_path.write_text("<svg>ok</svg>")
        # Operator opt-in: only this root is readable for delivered files.
        self._env_patch = unittest.mock.patch.dict(
            os.environ, {"ARTIFACT_SIDECAR_CAPTURE_ROOTS": str(self.deliver_root)}
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def _turn(self, reply: str, **extra):
        data_extra = {"delivered": True}
        data_extra.update(extra)
        return turn_completed_envelope(
            reply_text=reply,
            working_directory=str(self.tmp / "repo"),
            extra_data=data_extra,
        )

    def test_delivered_png_media_token_captured_with_real_bytes(self):
        env = self._turn(
            f"Here is the diagram.\n\nMEDIA:{self.png_path}\n\nDone."
        )
        candidates = [
            c for c in discovery.from_turn_completed(env)
            if c.capture.media_type == "image/png"
        ]
        self.assertEqual(len(candidates), 1)
        cand = candidates[0]
        self.assertEqual(cand.basis, "conversation_reply_delivered")
        # GENUINE bytes from disk — not an inline string pretending:
        self.assertEqual(cand.capture.data, self.png_bytes)
        self.assertTrue(cand.capture.logical_path.startswith("delivered/"))

    def test_delivered_svg_path_from_deliverables_list(self):
        env = self._turn(
            "Diagram attached.",
            deliverables=[{"title": "flow", "path": str(self.svg_path)}],
        )
        candidates = [
            c for c in discovery.from_turn_completed(env)
            if c.capture.media_type == "image/svg+xml"
        ]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].capture.data, b"<svg>ok</svg>")

    def test_delivered_file_outside_roots_refused(self):
        outside = self.tmp / "elsewhere" / "doc.md"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("# outside")
        env = self._turn(f"Take MEDIA:{outside}")
        candidates = [
            c for c in discovery.from_turn_completed(env)
            if c.capture.title == "doc.md"
        ]
        self.assertEqual(candidates, [])  # refused: not under any root

    def test_delivered_symlink_refused(self):
        link = self.deliver_root / "charts" / "link.png"
        link.symlink_to(self.png_path)
        env = self._turn(f"Take MEDIA:{link}")
        candidates = [
            c for c in discovery.from_turn_completed(env)
            if c.capture.title == "link.png"
        ]
        self.assertEqual(candidates, [])

    def test_relative_media_path_dropped(self):
        env = self._turn("See MEDIA:charts/architecture.png")
        self.assertEqual(
            [c for c in discovery.from_turn_completed(env) if c.capture.title.endswith(".png")],
            [],
        )

    def test_oversize_delivered_file_refused(self):
        big = self.deliver_root / "big.png"
        big.write_bytes(b"x" * 300)
        env = self._turn(f"Take MEDIA:{big}")
        real_read = discovery.read_file_bounded
        with unittest.mock.patch.object(
            discovery,
            "read_file_bounded",
            lambda path, roots: real_read(path, roots, max_bytes=64),
        ):
            candidates = discovery.from_turn_completed(env)
        self.assertEqual(
            [c for c in candidates if c.capture.title == "big.png"], []
        )

    def test_hermes_cache_denied_even_if_rooted(self):
        # .hermes is on the denylist: even an operator who roots capture
        # there cannot read delivered files out of the agent cache.
        fake_hermes = self.tmp / "agent-home" / ".hermes" / "cache"
        fake_hermes.mkdir(parents=True)
        secret = fake_hermes / "screen.png"
        secret.write_bytes(b"not for capture")
        with unittest.mock.patch.dict(
            os.environ,
            {"ARTIFACT_SIDECAR_CAPTURE_ROOTS": str(self.tmp / "agent-home")},
        ):
            env = self._turn(f"Take MEDIA:{secret}")
        self.assertEqual(
            [c for c in discovery.from_turn_completed(env) if c.capture.title == "screen.png"],
            [],
        )


class EndToEndHermesTests(unittest.TestCase):
    """Full chain: REAL Hermes adapter -> native payload -> bloodbank event
    -> sidecar -> artifact.created -> projector. No invented shapes: the
    source payload is the exact stdin wire the Hermes shell_hooks bridge
    sends (agent/shell_hooks.py _payload_fields), and the canonical
    publisher pipeline (core.publisher.run) is what builds the envelope.
    """

    def _harness(self):
        h = Harness()
        self.addCleanup(h.close)
        return h

    def _adapter(self):
        hooks_dir = REPO / "services" / "agent-hooks"
        if str(hooks_dir) not in sys.path:
            sys.path.insert(0, str(hooks_dir))
        from clients.hermes import HermesAdapter
        return HermesAdapter()

    def _publish_native(
        self,
        h,
        adapter,
        hook_name: str,
        payload: dict,
    ) -> dict:
        """Run the canonical publisher over a native Hermes hook payload.

        Captures the envelope at the NATS boundary via a patched
        core.nats_publish.publish, so the bloodbank event is exactly what
        the hook process would put on the wire. The adapter's stdin read is
        replaced with the in-memory native payload (the real wire is stdin
        JSON from the Hermes shell_hooks bridge), and hook-hub forwarding is
        disabled so the canonical path runs directly.
        """
        import core.nats_publish
        import core.publisher

        envelopes: list[dict] = []

        def fake_publish(subject, body, **kwargs):
            envelopes.append(json.loads(body.decode("utf-8")))

        def fake_read_payload(_argv):
            return payload

        # core.publisher did `from core.nats_publish import publish as
        # nats_publish` — patch BOTH the module attribute and publisher's
        # own binding so the envelope is captured at the real NATS call site.
        with unittest.mock.patch.dict(os.environ, {"BB_HOOK_HUB": "off"}):
            with unittest.mock.patch.object(
                core.nats_publish, "publish", fake_publish
            ), unittest.mock.patch.object(
                core.publisher, "nats_publish", fake_publish
            ), unittest.mock.patch.object(
                adapter, "read_payload", fake_read_payload
            ):
                core.publisher.run(adapter, ["publish.py", hook_name], report={})
        self.assertTrue(envelopes, "publisher produced no envelope")
        return envelopes[-1]

    def test_hermes_post_tool_call_write_end_to_end(self):
        h = self._harness()
        adapter = self._adapter()
        repo = h.tmp / "repos" / "bb"
        native_payload = {
            "hook_event_name": "post_tool_call",
            "tool_name": "write_file",
            "tool_input": {
                "path": str(repo / "_bmad_output" / "planning" / "roadmap.md"),
                "content": "# Real roadmap\n\nProduced by Hermes.",
            },
            "session_id": "hermes-sess-e2e",
            "cwd": str(repo),
            "profile": "rererere",
            "extra": {
                "result": "wrote 41 bytes",
                "status": "ok",
                "duration_ms": 12,
                "tool_call_id": "call-e2e-1",
                "turn_id": "turn-e2e-1",
            },
        }
        envelope = self._publish_native(h, adapter, "post_tool_call", native_payload)
        # The canonical Hermes adapter promoted the native fields, and the
        # produced agent event passes the FULL canonical validation itself:
        validate_envelope(envelope)
        data = envelope["data"]
        self.assertEqual(envelope["type"], "bloodbank.agent.tool.completed")
        self.assertEqual(data["tool_name"], "write_file")
        self.assertEqual(data["arguments"]["content"], "# Real roadmap\n\nProduced by Hermes.")
        self.assertEqual(data["working_directory"], str(repo))
        self.assertEqual(data["result"], "wrote 41 bytes")

        # Sidecar qualifies it (underscore tool + output tree signal):
        [d] = h.sidecar.process(envelope)
        self.assertEqual(d.kind, "enqueued", d.detail)
        [event] = h.drain()
        validate_envelope(event)  # full canonical schema
        self.assertEqual(event["data"]["origin"]["basis"], "agent_tool_write")
        self.assertEqual(
            event["data"]["artifact"]["sha256"],
            sha256_bytes("# Real roadmap\n\nProduced by Hermes.".encode()),
        )
        self.assertEqual(event["data"]["origin"]["session"]["cli"], "hermes")

        # Projector lands it in the vault:
        writer = projector_main.VaultWriter(h.tmp / "vault", {"bb": "Projects/Bloodbank"})
        with projector_main.ProjectorState(h.tmp / "proj-e2e.sqlite3") as pstate:
            proj = projector_main.Projector(
                state=pstate, writer=writer, store_root=h.store, tz="America/New_York"
            )
            result = proj.handle(event)
        self.assertEqual(result["kind"], "projected", result)

    def test_hermes_session_end_media_delivery_end_to_end(self):
        h = self._harness()
        adapter = self._adapter()
        repo = h.tmp / "repos" / "bb"
        # Delivered-file reads must NOT live under the (denylist-refused)
        # scratch TMPDIR: use a base whose absolute path passes denied_path.
        deliver_base = _safe_tmp_base()
        self.addCleanup(shutil.rmtree, deliver_base, True)
        deliver_root = deliver_base / "deliver"
        deliver_root.mkdir()
        png_path = deliver_root / "render.png"
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"e2e" * 20
        png_path.write_bytes(png_bytes)
        native_payload = {
            "hook_event_name": "on_session_end",
            "session_id": "hermes-sess-media",
            "cwd": str(repo),
            "profile": "rererere",
            "extra": {
                "turn_id": "turn-media-1",
                "completed": True,
                "failed": False,
                "interrupted": False,
                "last_assistant_message": (
                    "The rendered diagram is at MEDIA:%s\n" % png_path
                ),
                "delivered": True,
            },
        }
        with unittest.mock.patch.dict(
            os.environ, {"ARTIFACT_SIDECAR_CAPTURE_ROOTS": str(deliver_root)}
        ):
            envelope = self._publish_native(h, adapter, "on_session_end", native_payload)
            validate_envelope(envelope)  # the source agent event, full schema
            data = envelope["data"]
            self.assertEqual(envelope["type"], "bloodbank.conversation.turn.completed")
            self.assertEqual(data["reply_text"], native_payload["extra"]["last_assistant_message"])
            self.assertIs(data["delivered"], True)
            candidates = discovery.from_turn_completed(envelope)
            pngs = [c for c in candidates if c.capture.media_type == "image/png"]
            self.assertEqual(len(pngs), 1)
            self.assertEqual(pngs[0].capture.data, png_bytes)
            [d_png, d_reply] = h.sidecar.process(envelope)
            self.assertEqual(d_png.kind, "enqueued", d_png.detail)
            self.assertEqual(d_reply.kind, "enqueued", d_reply.detail)
            events = h.drain()
        [event] = [e for e in events if e["data"]["artifact"]["media_type"] == "image/png"]
        validate_envelope(event)
        self.assertEqual(event["data"]["origin"]["basis"], "conversation_reply_delivered")
        self.assertEqual(event["data"]["artifact"]["media_type"], "image/png")
        self.assertEqual(event["data"]["artifact"]["bytes"], len(png_bytes))
        # The digest is of the REAL bytes:
        self.assertEqual(event["data"]["artifact"]["sha256"], sha256_bytes(png_bytes))
        blob = get_blob(h.store, event["data"]["artifact"]["content"]["ref"])
        self.assertEqual(blob, png_bytes)


class RendererTests(unittest.TestCase):
    """ops/systemd/render-install.py: render + verify + dry-run contract."""

    def setUp(self):
        self.renderer = REPO / "ops" / "systemd" / "render-install.py"
        self.assertTrue(self.renderer.is_file())

    def _run(self, *args):
        proc = subprocess.run(
            [sys.executable, str(self.renderer), *args],
            capture_output=True, text=True, timeout=120,
        )
        return proc

    def test_dry_run_renders_configurable_paths(self):
        # The renderer resolves the interpreter symlink (python3 ->
        # python3.14); compare against the resolved form.
        python_resolved = str(Path(sys.executable).resolve())
        proc = self._run("--dry-run", "--repo", str(REPO), "--python", sys.executable)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"WorkingDirectory={REPO}/services/artifact-sidecar", proc.stdout)
        self.assertIn(f"ExecStart={python_resolved} ", proc.stdout)
        self.assertIn(f"Documentation=file://{REPO}/docs/project-artifact-sidecar.md", proc.stdout)
        self.assertNotIn("/usr/bin/python3", proc.stdout)
        self.assertNotIn("%h/code/33GOD/bloodbank", proc.stdout)

    def test_dry_run_writes_nothing(self):
        before = sorted(p.name for p in (REPO / "ops" / "systemd").rglob("*.service"))
        proc = self._run("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = sorted(p.name for p in (REPO / "ops" / "systemd").rglob("*.service"))
        self.assertEqual(before, after)

    def test_interpreter_without_nats_fails_loudly(self):
        # /usr/bin/python3 on this box lacks nats-py: verification must
        # refuse to render rather than emit a unit that cannot start.
        if not Path("/usr/bin/python3").exists():
            self.skipTest("no /usr/bin/python3")
        proc = self._run("--dry-run", "--python", "/usr/bin/python3")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cannot import", proc.stderr)

    def test_relative_repo_refused(self):
        proc = self._run("--dry-run", "--repo", "some/relative/path")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("absolute", proc.stderr)

    def test_install_refuses_overwrite_without_force(self):
        dest = Path(tempfile.mkdtemp(prefix="unit-dest-"))
        self.addCleanup(shutil.rmtree, dest, True)
        (dest / "bloodbank-artifact-sidecar.service").write_text("existing")
        proc = self._run(
            "--install", "--user-dir", str(dest),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refusing to overwrite", proc.stderr)
        self.assertEqual(
            (dest / "bloodbank-artifact-sidecar.service").read_text(), "existing"
        )


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="artifact-path-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "allowed"
        (self.root / "sub").mkdir(parents=True)

    def test_containment_ok(self):
        resolved = safe_resolve_under_root(self.root / "sub" / ".." / "x.md", self.root, label="t")
        self.assertEqual(resolved, (self.root / "x.md").resolve())

    def test_prefix_trap_refused(self):
        evil = self.tmp / "allowed-evil"
        evil.mkdir()
        with self.assertRaises(PathViolation):
            safe_resolve_under_root(evil / "x", self.root, label="t")

    def test_symlink_escape_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        link = self.root / "sub" / "link"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PathViolation):
            safe_resolve_under_root(link / "x.md", self.root, label="t")

    def test_within_root_symlink_refused_checked_before_resolution(self):
        # The adversarial case from review: a symlink fully INSIDE the root.
        # The old code resolved first and could not see it; the check must
        # run on the ORIGINAL components.
        real_dir = self.root / "real"
        real_dir.mkdir()
        (real_dir / "secret.md").write_text("x")
        link = self.root / "innocent"
        link.symlink_to(real_dir, target_is_directory=True)
        with self.assertRaises(PathViolation):
            safe_resolve_under_root(link / "secret.md", self.root, label="t")

    def test_denied_fragments_refused_before_allowlist(self):
        secret = self.root / ".ssh"
        secret.mkdir()
        with self.assertRaises(PathViolation):
            assert_readable_capture_path(secret / "id_rsa", (self.tmp,))

    def test_read_policy_requires_allowlisted_root(self):
        with self.assertRaises(PathViolation):
            assert_readable_capture_path(self.tmp / "elsewhere" / "f.md", (self.root,))

    def test_denied_path_helper_matches_relative_forms(self):
        self.assertIsNotNone(denied_path("config/app.json"))
        self.assertIsNotNone(denied_path(".env"))
        self.assertIsNotNone(denied_path("scratch/n.md"))
        self.assertIsNone(denied_path("docs/design.md"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="artifact-store-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = artifact_lib.ensure_store_root(self.tmp / "store")

    def test_ref_must_be_strict_lowercase_hex(self):
        put_blob(self.root, b"data")
        sha = sha256_bytes(b"data")
        ok = get_blob(self.root, f"sha256:{sha}")
        self.assertEqual(ok, b"data")
        for bad in (
            f"sha256:{sha.upper()}",
            "sha256:" + sha[:63] + "g",
            f"sha256:../../etc/passwd{sha[62:]}",
            f"sha256:{sha[:-2]}..",
            "sha256:" + sha[:32],
            sha,
            f"sha256:{sha[:-1]}/",
        ):
            with self.assertRaises(StoreError, msg=bad):
                get_blob(self.root, bad)

    def test_traversal_ref_rejected_without_filesystem_touch(self):
        # Even if a file named like the traversal exists, the ref is refused
        # before any open.
        sha = sha256_bytes(b"x")
        with self.assertRaises(StoreError):
            get_blob(self.root, f"sha256:{'..' * 64}")

    def test_blob_read_is_symlink_refused(self):
        sha = sha256_bytes(b"payload")
        fan = self.root / sha[:2]
        fan.mkdir(parents=True, exist_ok=True)
        outside = self.tmp / "outside.bin"
        outside.write_bytes(b"not the payload")
        (fan / sha).symlink_to(outside)
        with self.assertRaises(StoreError):
            get_blob(self.root, f"sha256:{sha}")

    def test_corrupt_preexisting_blob_refused_on_put(self):
        sha = sha256_bytes(b"good")
        fan = self.root / sha[:2]
        fan.mkdir(parents=True, exist_ok=True)
        (fan / sha).write_bytes(b"corrupt bytes")  # digest mismatch
        with self.assertRaises(StoreError):
            put_blob(self.root, b"good")

    def test_preexisting_matching_blob_idempotent(self):
        sha1, _ = put_blob(self.root, b"same")
        sha2, _ = put_blob(self.root, b"same")
        self.assertEqual(sha1, sha2)

    def test_blob_files_private_perms(self):
        sha, path = put_blob(self.root, b"secret-ish")
        self.assertEqual(stat_module.S_IMODE(path.stat().st_mode), 0o600)
        fan = self.root / sha[:2]
        self.assertEqual(stat_module.S_IMODE(fan.stat().st_mode), 0o700)

    def test_store_root_symlink_refused(self):
        real = self.tmp / "real-store"
        real.mkdir()
        link = self.tmp / "link-store"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(StoreError):
            artifact_lib.ensure_store_root(link)

    def test_symlinked_fanout_refused(self):
        sha = sha256_bytes(b"data")
        evil = self.tmp / "evil"
        evil.mkdir()
        (self.root / sha[:2]).symlink_to(evil, target_is_directory=True)
        with self.assertRaises(StoreError):
            put_blob(self.root, b"data")

    def test_oversize_blob_refused(self):
        with self.assertRaises(StoreError):
            put_blob(self.root, b"x" * (artifact_lib.MAX_CAPTURE_BYTES + 1))


class PipelineTests(unittest.TestCase):
    def _harness(self):
        h = Harness()
        self.addCleanup(h.close)
        return h

    def test_publish_duplicate_revision(self):
        h = self._harness()
        path = str(h.tmp / "repos" / "bb" / "docs" / "a.md")
        env = tool_completed_envelope(path=path, content="v1", working_directory=str(h.tmp / "repos" / "bb"))
        [d1] = h.sidecar.process(env)
        self.assertEqual(d1.kind, "enqueued")  # NOT published: only enqueued
        self.assertEqual(d1.revision, 1)
        [event1] = h.drain()
        self.assertEqual(event1["data"]["artifact"]["revision"], 1)

        # Exact replay of the source event: duplicate, nothing new published.
        [d2] = h.sidecar.process(env)
        self.assertEqual(d2.kind, "duplicate")
        self.assertEqual(h.drain(), [])

        # Same logical path, new bytes: revision 2.
        env2 = tool_completed_envelope(path=path, content="v2 body", working_directory=str(h.tmp / "repos" / "bb"))
        [d3] = h.sidecar.process(env2)
        self.assertEqual(d3.kind, "enqueued")
        self.assertEqual(d3.revision, 2)
        [event2] = h.drain()
        self.assertEqual(event2["data"]["artifact"]["revision"], 2)
        self.assertNotEqual(event1["data"]["artifact"]["sha256"], event2["data"]["artifact"]["sha256"])

    def test_stale_source_replay_cannot_revert_revision(self):
        # The adversarial replay: v1 event replayed AFTER v2 was captured.
        h = self._harness()
        path = str(h.tmp / "repos" / "bb" / "docs" / "a.md")
        env1 = tool_completed_envelope(path=path, content="v1", working_directory=str(h.tmp / "repos" / "bb"))
        h.sidecar.process(env1)
        h.drain()
        env2 = tool_completed_envelope(path=path, content="v2", working_directory=str(h.tmp / "repos" / "bb"))
        h.sidecar.process(env2)
        h.drain()
        # Stale replay of env1: must NOT mint revision 3 with v1 bytes.
        [d] = h.sidecar.process(env1)
        self.assertEqual(d.kind, "duplicate")
        head = h.state.artifact_head(artifact_id_for("bb", "docs/a.md"))
        self.assertEqual(head["revision"], 2)
        self.assertEqual(head["sha256"], sha256_bytes(b"v2"))
        self.assertEqual(h.drain(), [])

    def test_artifact_and_outbox_are_atomic(self):
        # Crash between the old two-step commit could strand an artifact row
        # whose event was never enqueued. Now one transaction: probe by
        # counting rows after an injected failure is impossible, so assert
        # the invariant directly — every artifact row has an outbox row (or
        # a published marker), and vice versa.
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "a.md"),
            content="atomic",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        h.sidecar.process(env)
        h.drain()
        db = sqlite3.connect(h.tmp / "state.sqlite3")
        artifacts = db.execute("SELECT artifact_id, revision FROM artifacts").fetchall()
        outbox = db.execute("SELECT artifact_id, revision FROM outbox").fetchall()
        db.close()
        self.assertTrue(artifacts)
        self.assertEqual(set(artifacts), set(outbox))

    def test_restart_replays_outbox_only(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "a.md"),
            content="durable",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        h.sidecar.process(env)
        # No drain: simulates crash before publish. Close and reopen state.
        h.state.close()
        with SidecarState(h.tmp / "state.sqlite3") as state2:
            sidecar2 = Sidecar(state=state2, store_root=h.store, registry=h.registry)
            published: list[dict] = []
            results = sidecar2.drain_outbox(published.append)
            self.assertEqual([r.kind for r in results], ["published"])
            self.assertEqual(len(published), 1)
            # Replaying the same source event after restart is a duplicate.
            [d] = sidecar2.process(env)
            self.assertEqual(d.kind, "duplicate")

    def test_outbox_publish_failure_does_not_mark_published(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "a.md"),
            content="retry me",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        h.sidecar.process(env)

        def boom(_envelope):
            raise RuntimeError("transport down")

        results = h.sidecar.drain_outbox(boom)
        self.assertEqual([r.kind for r in results], ["error"])
        pending = h.state.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 1)
        self.assertIn("transport down", pending[0]["last_error"])
        # Not marked published anywhere:
        db = sqlite3.connect(h.tmp / "state.sqlite3")
        pub = db.execute("SELECT published_at FROM outbox").fetchone()[0]
        art = db.execute("SELECT published_event_id FROM artifacts").fetchone()[0]
        db.close()
        self.assertIsNone(pub)
        self.assertIsNone(art)

    def test_outbox_retry_respects_backoff_and_bounds(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "a.md"),
            content="bounded",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        h.sidecar.process(env)

        def boom(_e):
            raise RuntimeError("still down")

        # Exhaust the retry budget (each pass bumps attempts; backoff gates
        # the NEXT pass, so reset next_attempt_at to force dueness).
        import sidecar as sidecar_mod

        for _ in range(sidecar_mod.OUTBOX_MAX_ATTEMPTS):
            h.state._db.execute("UPDATE outbox SET next_attempt_at = 0")
            h.state._db.commit()
            h.sidecar.drain_outbox(boom)
        row = h.state.pending()[0]
        self.assertEqual(row["attempts"], sidecar_mod.OUTBOX_MAX_ATTEMPTS)
        # Further drains skip it (exhausted, not due):
        self.assertEqual(h.sidecar.drain_outbox(boom), [])
        self.assertEqual(len(h.state.exhausted()), 1)
        # Operator re-arm CLI:
        re_armed = h.state.retry_exhausted()
        self.assertEqual(re_armed, 1)
        # And now a healthy transport publishes it.
        [r] = h.sidecar.drain_outbox(h.published.append)
        self.assertEqual(r.kind, "published")

    def test_outbox_backoff_delays_retry(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "a.md"),
            content="backoff",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        h.sidecar.process(env)
        calls = []

        def flaky(_e):
            calls.append(1)
            raise RuntimeError("flaky")

        h.sidecar.drain_outbox(flaky)  # attempt 1, next attempt in the future
        h.sidecar.drain_outbox(flaky)  # not due yet -> no call
        self.assertEqual(len(calls), 1)

    def test_missing_attribution_held_not_published(self):
        h = self._harness()
        env = tool_completed_envelope(path="/nowhere/a.md", content="x", working_directory=None)
        [d] = h.sidecar.process(env)
        self.assertEqual(d.kind, "held")
        self.assertEqual(h.drain(), [])
        holds = h.state.holds()
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["sha256"], artifact_lib.sha256_bytes(b"x"))

    def test_hold_identity_keeps_provenance(self):
        # Same bytes, two different source events: two holds, not one merged.
        h = self._harness()
        h.sidecar.process(tool_completed_envelope(path="/nowhere/a.md", content="same", working_directory=None))
        h.sidecar.process(tool_completed_envelope(path="/nowhere/b.md", content="same", working_directory=None))
        holds = h.state.holds()
        self.assertEqual(len(holds), 2)
        self.assertNotEqual(holds[0]["hold_id"], holds[1]["hold_id"])
        self.assertNotEqual(holds[0]["source_event"]["id"], holds[1]["source_event"]["id"])

    def test_ambiguous_wd_outside_registry_held(self):
        h = self._harness()
        env = tool_completed_envelope(
            path="/srv/stray/a.md", content="x",
            working_directory=str(h.tmp / "not-a-repo"),
        )
        [d] = h.sidecar.process(env)
        self.assertEqual(d.kind, "held")

    def test_file_path_escape_from_repo_held(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "other" / "evil.md"),  # claims bb via wd, writes other repo
            content="x",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        [d] = h.sidecar.process(env)
        self.assertEqual(d.kind, "held")

    def test_hold_resolve_and_publish(self):
        h = self._harness()
        env = tool_completed_envelope(path="/nowhere/a.md", content="held body", working_directory=None)
        [d] = h.sidecar.process(env)
        self.assertEqual(d.kind, "held")
        hold_id = h.state.holds()[0]["hold_id"]
        self.assertTrue(h.state.resolve_hold(hold_id, "bb"))
        d = h.sidecar.publish_resolution(hold_id)
        self.assertEqual(d.kind, "enqueued", d.detail)
        [event] = h.drain()
        self.assertEqual(event["data"]["origin"]["basis"], "operator_resolution")
        self.assertEqual(event["data"]["project"]["slug"], "bb")
        validate_envelope(event)

    def test_planned_project_with_ok_index_is_attributed(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "p.md"),
            content="planning doc",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        [d] = h.sidecar.process(env)
        self.assertEqual(d.kind, "enqueued")  # bb status is 'planned'

    def test_failing_index_status_excluded(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "broken" / "x.md"),
            content="x",
            working_directory=str(h.tmp / "repos" / "broken"),
        )
        [d] = h.sidecar.process(env)
        self.assertEqual(d.kind, "held")  # broken index -> no attribution

    def test_bytes_durable_before_event(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "b.md"),
            content="bytes first",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        h.sidecar.process(env)
        sha = h.state.artifact_head(artifact_id_for("bb", "docs/b.md"))["sha256"]
        blob = get_blob(h.store, f"sha256:{sha}")
        self.assertEqual(blob, b"bytes first")

    def test_oversize_capture_skipped(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "big.md"),
            content="x" * (artifact_lib.MAX_CAPTURE_BYTES + 1),
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        [d] = h.sidecar.process(env)
        self.assertEqual(d.kind, "skipped")

    def test_unsupported_write_family_reported(self):
        h = self._harness()
        env = tool_completed_envelope(tool="write", path=None, content=None)
        env["data"]["arguments"] = {"path": "/x/a.md"}
        env["data"]["working_directory"] = str(h.tmp / "repos" / "bb")
        [d] = h.sidecar.process(env)
        self.assertEqual(d.kind, "unsupported")

    def test_inline_denied_location_not_persisted(self):
        # .env content arriving via an inline write is never persisted.
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / ".env"),
            content="SECRET=1",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        [d] = h.sidecar.process(env)
        self.assertEqual(d.kind, "skipped")
        self.assertEqual(h.state.holds(), [])
        self.assertEqual(h.state.pending(), [])
        # And the bytes never entered the store:
        with self.assertRaises(StoreError):
            get_blob(h.store, "sha256:" + sha256_bytes(b"SECRET=1"))

    def test_inline_config_json_not_persisted(self):
        h = self._harness()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "config" / "app.json"),
            content='{"a": 1}',
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        [d] = h.sidecar.process(env)
        self.assertEqual(d.kind, "skipped")

    def test_candidate_exception_contained(self):
        # One candidate blowing up must not kill processing of siblings.
        h = self._harness()
        reply = "```mermaid\ngraph TD\nA-->B\n```"
        env = turn_completed_envelope(reply_text=reply, working_directory=str(h.tmp / "repos" / "bb"))
        original = discovery.from_turn_completed

        def poisoned(envelope, **kw):
            raise RuntimeError("adapter exploded")

        discovery.from_turn_completed = poisoned
        try:
            ds = h.sidecar.process(env)
        finally:
            discovery.from_turn_completed = original
        self.assertEqual(len(ds), 1)
        self.assertEqual(ds[0].kind, "error")
        self.assertIn("adapter exploded", ds[0].detail)


class WorktreeTests(unittest.TestCase):
    """Git-identity attribution: worktrees and nested repos."""

    def _world(self):
        tmp = Path(tempfile.mkdtemp(prefix="artifact-wt-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        # umbrella repo (enrolled as 'umb') with a real git dir
        umb = tmp / "code" / "umb"
        (umb / "docs").mkdir(parents=True)
        subprocess.run(["git", "-C", str(umb), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(umb), "commit", "--allow-empty", "-m", "init", "-q"], check=True)
        # enrolled child repo bb, a linked WORKTREE of another repo 'bbmain'
        bbmain = tmp / "code" / "bbmain"
        bbmain.mkdir(parents=True)
        subprocess.run(["git", "-C", str(bbmain), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(bbmain), "commit", "--allow-empty", "-m", "init", "-q"], check=True)
        wt = tmp / "code" / "umb" / "bb-worktree"  # physically INSIDE umbrella
        subprocess.run(
            ["git", "-C", str(bbmain), "worktree", "add", "--detach", str(wt)], check=True
        )
        payload = {
            "schema_version": 1,
            "projects": {
                "umb": {"name": "Umbrella", "slug": "umb", "repo_path": str(umb)},
                "bb": {"name": "Bloodbank", "slug": "bb", "repo_path": str(bbmain)},
            },
            "__registry_status": {"umb": {"status": "ok"}, "bb": {"status": "ok"}},
        }
        registry = ProjectRegistry.from_payload(payload)
        store = artifact_lib.ensure_store_root(tmp / "store")
        return tmp, registry, store, umb, wt, bbmain

    def test_worktree_attributes_to_owning_repo_not_umbrella(self):
        tmp, registry, store, umb, wt, bbmain = self._world()
        with SidecarState(tmp / "state.sqlite3") as state:
            sidecar = Sidecar(state=state, store_root=store, registry=registry)
            env = tool_completed_envelope(
                path=str(wt / "docs" / "d.md"),
                content="worktree doc",
                working_directory=str(wt),
            )
            [d] = sidecar.process(env)
            self.assertEqual(d.kind, "enqueued", d.detail)
            head = state.artifact_head(artifact_id_for("bb", "docs/d.md"))
            self.assertIsNotNone(head)  # attributed to bb, NOT umb

    def test_foreign_nested_repo_held_not_umbrella(self):
        tmp, registry, store, umb, wt, bbmain = self._world()
        # A repo inside the umbrella that belongs to NO enrolled project.
        stray = umb / "stray"
        stray.mkdir()
        subprocess.run(["git", "-C", str(stray), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(stray), "commit", "--allow-empty", "-m", "i", "-q"], check=True)
        with SidecarState(tmp / "state.sqlite3") as state:
            sidecar = Sidecar(state=state, store_root=store, registry=registry)
            env = tool_completed_envelope(
                path=str(stray / "notes.md"),
                content="stray content",
                working_directory=str(stray),
            )
            [d] = sidecar.process(env)
            self.assertEqual(d.kind, "held")  # never attributed to the umbrella

    def test_umbrella_docs_still_attributed_to_umbrella(self):
        tmp, registry, store, umb, wt, bbmain = self._world()
        with SidecarState(tmp / "state.sqlite3") as state:
            sidecar = Sidecar(state=state, store_root=store, registry=registry)
            env = tool_completed_envelope(
                path=str(umb / "docs" / "top.md"),
                content="umbrella doc",
                working_directory=str(umb),
            )
            [d] = sidecar.process(env)
            self.assertEqual(d.kind, "enqueued")
            head = state.artifact_head(artifact_id_for("umb", "docs/top.md"))
            self.assertIsNotNone(head)


class ProjectorTests(unittest.TestCase):
    def _world(self):
        tmp = Path(tempfile.mkdtemp(prefix="artifact-proj-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "repos" / "bb" / "docs").mkdir(parents=True)
        registry = ProjectRegistry.from_payload(registry_payload(tmp))
        store = artifact_lib.ensure_store_root(tmp / "store")
        state = SidecarState(tmp / "sidecar.sqlite3")
        self.addCleanup(state.close)
        sidecar = Sidecar(state=state, store_root=store, registry=registry)

        class H:
            tmp: Path
            registry: ProjectRegistry
            store: Path
            state: SidecarState
            sidecar: Sidecar

        h = H()
        h.tmp = tmp
        h.registry = registry
        h.store = store
        h.state = state
        h.sidecar = sidecar
        return h

    def _projector(self, harness, mapping: dict[str, str], vault: Path):
        writer = projector_main.VaultWriter(vault, mapping)
        state = projector_main.ProjectorState(harness.tmp / "proj.sqlite3")
        self.addCleanup(state.close)
        proj = projector_main.Projector(
            state=state, writer=writer, store_root=harness.store, tz="America/New_York"
        )
        return proj

    def _artifact_event(self, harness, *, content="the artifact", time="2026-10-17T02:30:00Z", path=None):
        env = tool_completed_envelope(
            path=path or str(harness.tmp / "repos" / "bb" / "docs" / "note.md"),
            content=content,
            working_directory=str(harness.tmp / "repos" / "bb"),
            time=time,
        )
        harness.sidecar.process(env)
        published: list[dict] = []
        harness.sidecar.drain_outbox(published.append)
        [event] = published
        return event

    def test_projection_writes_under_explicit_mapping_and_local_date(self):
        h = self._world()
        vault = h.tmp / "vault"
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        event = self._artifact_event(h)  # 02:30Z -> 2026-10-16 in NY
        result = proj.handle(event)
        self.assertEqual(result["kind"], "projected", result)
        target = vault / "Projects" / "Bloodbank" / "2026-10-16" / "note.md"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"the artifact")

    def test_late_night_event_rolls_to_next_day(self):
        h = self._world()
        vault = h.tmp / "vault"
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        event = self._artifact_event(h, time="2026-10-17T04:30:00Z")  # 00:30 NY on the 17th
        result = proj.handle(event)
        self.assertEqual(result["kind"], "projected")
        self.assertTrue((vault / "Projects" / "Bloodbank" / "2026-10-17" / "note.md").exists())

    def test_unmapped_slug_is_durable_hold_not_lost(self):
        h = self._world()
        vault = h.tmp / "vault"
        proj = self._projector(h, {"other": "Projects/Other"}, vault)
        event = self._artifact_event(h)  # bb event
        result = proj.handle(event)
        self.assertEqual(result["kind"], "held")
        date_dir = vault / "Projects" / "Bloodbank" / "2026-10-16"
        self.assertFalse(date_dir.exists())  # no projection anywhere
        # DURABLE (not a RAM set): visible in state, resolvable, replayable.
        holds = proj.state.holds()
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["kind"], "unmapped")
        self.assertIn("no explicit vault mapping", holds[0]["reason"])
        # The envelope is preserved for later projection:
        env = proj.state.hold_envelope(holds[0]["hold_id"])
        self.assertEqual(env["id"], event["id"])
        # Replay after mapping is added projects it:
        proj.writer.mapping["bb"] = "Projects/Bloodbank"
        proj.state.resolve_hold(holds[0]["hold_id"], {"mapped_to": "Projects/Bloodbank"})
        result2 = proj.handle(env)
        self.assertEqual(result2["kind"], "projected", result2)
        self.assertTrue((vault / "Projects" / "Bloodbank" / "2026-10-16" / "note.md").exists())

    def test_malformed_envelope_deadlettered_before_writes(self):
        h = self._world()
        vault = h.tmp / "vault"
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        event = self._artifact_event(h)
        # Corrupt the envelope in a way the schema catches (ref != sha):
        event["data"]["artifact"]["content"]["ref"] = "sha256:" + "0" * 64
        result = proj.handle(event)
        self.assertEqual(result["kind"], "deadletter")
        holds = proj.state.holds()
        self.assertEqual(holds[0]["kind"], "malformed")
        # Nothing was written to the vault:
        self.assertFalse(list(vault.rglob("*")) if vault.exists() else [])

    def test_missing_source_blob_is_retryable_error_not_projected(self):
        h = self._world()
        vault = h.tmp / "vault"
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        event = self._artifact_event(h)
        # Make the blob vanish (store root swap):
        empty_store = artifact_lib.ensure_store_root(h.tmp / "empty-store")
        proj.store_root = empty_store
        result = proj.handle(event)
        self.assertEqual(result["kind"], "error")
        self.assertIn("retry", result["detail"])
        # NOT recorded as projected:
        self.assertEqual(proj.state.counts(), {})
        # Replay with the store back works:
        proj.store_root = h.store
        result2 = proj.handle(event)
        self.assertEqual(result2["kind"], "projected", result2)

    def test_no_overwrite_conflict_gets_deterministic_suffix(self):
        h = self._world()
        vault = h.tmp / "vault"
        date_dir = vault / "Projects" / "Bloodbank" / "2026-10-16"
        date_dir.mkdir(parents=True)
        (date_dir / "note.md").write_bytes(b"operator's own note")
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        event = self._artifact_event(h)
        result = proj.handle(event)
        self.assertEqual(result["kind"], "projected")
        self.assertEqual((date_dir / "note.md").read_bytes(), b"operator's own note")
        sha = event["data"]["artifact"]["sha256"]
        suffixed = date_dir / f"note-{sha[:8]}.md"
        self.assertTrue(suffixed.exists())
        self.assertEqual(suffixed.read_bytes(), b"the artifact")
        # The ACTUAL path (suffixed) is recorded, not the original:
        row = proj.state._db.execute("SELECT vault_path FROM projections").fetchone()
        self.assertEqual(row["vault_path"], str(suffixed))

    def test_identical_bytes_idempotent(self):
        h = self._world()
        vault = h.tmp / "vault"
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        event = self._artifact_event(h)
        self.assertEqual(proj.handle(event)["kind"], "projected")
        dup = proj.handle(dict(event))  # replay of the same bus message
        self.assertEqual(dup["kind"], "duplicate")
        files = list((vault / "Projects" / "Bloodbank" / "2026-10-16").iterdir())
        self.assertEqual(len(files), 1)

    def test_crash_after_file_before_db_replay_no_duplicate(self):
        h = self._world()
        vault = h.tmp / "vault"
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        event = self._artifact_event(h)
        # Simulate crash: write happens, DB record does not.
        sha = event["data"]["artifact"]["sha256"]
        base = vault / "Projects" / "Bloodbank" / "2026-10-16" / "note.md"
        proj.writer.write(path=base, data=b"the artifact", sha=sha)
        # Now the projector processes the same event fresh (new state db
        # simulates the lost DB record):
        proj2 = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        result = proj2.handle(event)
        self.assertEqual(result["kind"], "projected")
        files = list((vault / "Projects" / "Bloodbank" / "2026-10-16").iterdir())
        self.assertEqual(len(files), 1)  # identical bytes found, no duplicate

    def test_render_pair_written_with_own_extension(self):
        h = self._world()
        vault = h.tmp / "vault"
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        event = self._artifact_event(h)
        render_bytes = b"<svg>rendered</svg>"
        sha, _ = artifact_lib.put_blob(h.store, render_bytes)
        event["data"]["artifact"]["render"] = {
            "media_type": "image/svg+xml",
            "sha256": sha,
            "bytes": len(render_bytes),
            "content": {"storage": "content-addressed", "ref": f"sha256:{sha}"},
        }
        result = proj.handle(event)
        self.assertEqual(result["kind"], "projected", result)
        date_dir = vault / "Projects" / "Bloodbank" / "2026-10-16"
        self.assertTrue((date_dir / "note.md").exists())
        # Render extension from ITS OWN media type (svg), not source's (.md):
        self.assertTrue((date_dir / "note.render.svg").exists())
        self.assertEqual((date_dir / "note.render.svg").read_bytes(), render_bytes)

    def test_render_for_mermaid_source_gets_mmd_extension(self):
        h = self._world()
        vault = h.tmp / "vault"
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        reply = "```mermaid\ngraph TD\nA-->B\n```"
        env = turn_completed_envelope(
            reply_text=reply, working_directory=str(h.tmp / "repos" / "bb")
        )
        h.sidecar.process(env)
        published: list[dict] = []
        h.sidecar.drain_outbox(published.append)
        [event] = published
        render_bytes = b"<svg>mermaid render</svg>"
        sha, _ = artifact_lib.put_blob(h.store, render_bytes)
        event["data"]["artifact"]["render"] = {
            "media_type": "image/svg+xml",
            "sha256": sha,
            "bytes": len(render_bytes),
            "content": {"storage": "content-addressed", "ref": f"sha256:{sha}"},
        }
        result = proj.handle(event)
        self.assertEqual(result["kind"], "projected", result)
        files = sorted(p.name for p in (vault / "Projects" / "Bloodbank" / "2026-10-16").iterdir())
        # diagram title is digest-named with .mmd source ext; render twin gets
        # ITS OWN media type's extension (.svg):
        diagrams = [p for p in files if "diagram" in p]
        self.assertEqual(len(diagrams), 2)  # source .mmd + render .render.svg
        self.assertTrue(any(f.endswith(".mmd") for f in diagrams))
        self.assertTrue(any(f.endswith(".render.svg") for f in diagrams))

    def test_missing_render_blob_is_retryable_not_projected(self):
        h = self._world()
        vault = h.tmp / "vault"
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        event = self._artifact_event(h)
        render_bytes = b"<svg>gone</svg>"
        sha, _ = artifact_lib.put_blob(h.store, render_bytes)
        event["data"]["artifact"]["render"] = {
            "media_type": "image/svg+xml",
            "sha256": sha,
            "bytes": len(render_bytes),
            "content": {"storage": "content-addressed", "ref": f"sha256:{sha}"},
        }
        # Remove the render blob (crash between sidecar and projector, or GC):
        (h.store / sha[:2] / sha).unlink()
        result = proj.handle(event)
        self.assertEqual(result["kind"], "error")
        self.assertIn("render", result["detail"])
        # Nothing half-written: no source file either (validated pre-write).
        self.assertFalse(list((vault / "Projects" / "Bloodbank").rglob("note.md")))

    def test_declared_bytes_mismatch_rejected(self):
        h = self._world()
        vault = h.tmp / "vault"
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        event = self._artifact_event(h)
        event["data"]["artifact"]["bytes"] = 999999
        result = proj.handle(event)
        self.assertEqual(result["kind"], "error")
        self.assertIn("declared bytes", result["detail"])

    def test_symlinked_date_dir_refused(self):
        h = self._world()
        vault = h.tmp / "vault"
        target = h.tmp / "evil-target"
        target.mkdir()
        (target / "stolen.md").write_text("pwn")
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        event = self._artifact_event(h)
        # Attacker pre-plants a symlinked DATE dir inside the project dir:
        proj_dir = vault / "Projects" / "Bloodbank"
        proj_dir.mkdir(parents=True)
        (proj_dir / "2026-10-16").symlink_to(target, target_is_directory=True)
        result = proj.handle(event)
        self.assertEqual(result["kind"], "error", result)
        self.assertFalse((target / "note.md").exists())

    def test_symlinked_final_file_not_overwritten_or_followed(self):
        h = self._world()
        vault = h.tmp / "vault"
        outside = h.tmp / "outside.bin"
        outside.write_bytes(b"precious")
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        event = self._artifact_event(h)
        date_dir = vault / "Projects" / "Bloodbank" / "2026-10-16"
        date_dir.mkdir(parents=True)
        (date_dir / "note.md").symlink_to(outside)
        result = proj.handle(event)
        # Symlink at destination is refused (never followed, never replaced):
        self.assertEqual(result["kind"], "error", result)
        self.assertEqual(outside.read_bytes(), b"precious")
        self.assertTrue((date_dir / "note.md").is_symlink())

    def test_projector_never_ingests_own_projection_events(self):
        h = self._world()
        vault = h.tmp / "vault"
        proj = self._projector(h, {"bb": "Projects/Bloodbank"}, vault)
        foreign = {"type": "bloodbank.agent.tool.completed", "id": str(uuid.uuid4()), "data": {}}
        result = proj.handle(foreign)
        self.assertEqual(result["kind"], "ignored")

    def test_sidecar_ignores_artifact_events(self):
        h = self._world()
        event = {"type": "bloodbank.project.artifact.created", "id": str(uuid.uuid4()), "data": {}}
        self.assertEqual(h.sidecar.process(event), [])

    def test_mapping_validation(self):
        tmp = Path(tempfile.mkdtemp(prefix="mapping-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        good = tmp / "m.json"
        good.write_text(json.dumps({"bb": "Projects/Bloodbank"}))
        self.assertEqual(projector_main.load_mapping(str(good)), {"bb": "Projects/Bloodbank"})
        bad = tmp / "bad.json"
        bad.write_text(json.dumps({"bb": "/etc/passwd"}))
        with self.assertRaises(projector_main.MappingError):
            projector_main.load_mapping(str(bad))
        trav = tmp / "t.json"
        trav.write_text(json.dumps({"bb": "../outside"}))
        with self.assertRaises(projector_main.MappingError):
            projector_main.load_mapping(str(trav))
        coll = tmp / "c.json"
        coll.write_text(json.dumps({"bb": "Projects/Bloodbank", "BB": "projects/bloodbank"}))
        with self.assertRaises(projector_main.MappingError):
            projector_main.load_mapping(str(coll))
        hidden = tmp / "h.json"
        hidden.write_text(json.dumps({"bb": ".hidden/dir"}))
        with self.assertRaises(projector_main.MappingError):
            projector_main.load_mapping(str(hidden))

    def test_state_db_is_private(self):
        h = self._world()
        state = projector_main.ProjectorState(h.tmp / "perms.sqlite3")
        self.addCleanup(state.close)
        self.assertEqual(
            stat_module.S_IMODE((h.tmp / "perms.sqlite3").stat().st_mode), 0o600
        )


class RegistryTests(unittest.TestCase):
    def test_deepest_repo_wins(self):
        payload = {
            "schema_version": 1,
            "projects": {
                "outer": {"name": "Outer", "slug": "outer", "repo_path": "/repos", "project_id": "outer"},
                "inner": {"name": "Inner", "slug": "inner", "repo_path": "/repos/inner", "project_id": "inner"},
            },
        }
        reg = ProjectRegistry.from_payload(payload)
        ref = reg.resolve_by_repo_path("/repos/inner/sub")
        self.assertIsNotNone(ref)
        self.assertEqual(ref.slug, "inner")

    def test_bad_schema_version_refused(self):
        with self.assertRaises(RegistryError):
            ProjectRegistry.from_payload({"schema_version": 2, "projects": {}})

    def test_empty_projects_refused(self):
        with self.assertRaises(RegistryError):
            ProjectRegistry.from_payload({"schema_version": 1, "projects": {}})

    def test_invalid_slug_refused(self):
        with self.assertRaises(RegistryError):
            ProjectRegistry.from_payload(
                {"schema_version": 1, "projects": {"x": {"slug": "Bad Slug!", "repo_path": "/x"}}}
            )

    def test_validate_event_project(self):
        payload = registry_payload(Path("/tmp/x"))
        reg = ProjectRegistry.from_payload(payload)
        reg.validate_event_project({"slug": "bb", "name": "Bloodbank"})
        with self.assertRaises(RegistryError):
            reg.validate_event_project({"slug": "bb", "name": "Not Bloodbank"})
        with self.assertRaises(RegistryError):
            reg.validate_event_project({"slug": "ghost", "name": "Ghost"})


class FakeMsg:
    """Minimal stand-in for a nats.aio.msg.Msg."""

    def __init__(self, subject: str, envelope: dict):
        self.subject = subject
        self.data = json.dumps(envelope).encode()
        self._acked = False
        self._naked = False
        self.nak_delay = None

    async def ack(self):
        self._acked = True

    async def nak(self, delay=0):
        self._naked = True
        self.nak_delay = delay


class FakeJS:
    """In-memory JetStream: records publishes, can fail N first attempts."""

    def __init__(self, fail_first=0):
        self.published: list[tuple[str, bytes, dict]] = []
        self.fail_first = fail_first

    async def publish(self, subject, payload, timeout=None, headers=None):
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("fake transport outage")
        self.published.append((subject, payload, headers or {}))


class FakeTransportTests(unittest.TestCase):
    """Runtime ack/nak/drain contract against a fake transport."""

    def _world(self):
        import importlib.util

        h = Harness()
        self.addCleanup(h.close)
        # Load the SIDECAR main.py under its own module name (both services
        # have a main.py; a bare import_module("main") would pick whichever
        # dir landed on sys.path first).
        spec = importlib.util.spec_from_file_location(
            "artifact_sidecar_main", HERE / "main.py"
        )
        assert spec is not None and spec.loader is not None
        runtime_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime_mod)
        return h, runtime_mod

    def test_handle_acks_only_after_durable_terminal_dispositions(self):
        h, mod = self._world()
        runtime = mod.Runtime(h.sidecar)
        fake_js = FakeJS()
        runtime._js = fake_js
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "rt.md"),
            content="runtime",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        msg = FakeMsg("bloodbank.evt.agent.tool.completed", env)
        asyncio.run(runtime._handle(msg))
        self.assertTrue(msg._acked)
        # Published through the REAL awaited transport path with Nats-Msg-Id:
        self.assertEqual(len(fake_js.published), 1)
        subject, payload, headers = fake_js.published[0]
        self.assertEqual(subject, "bloodbank.evt.project.artifact.created")
        published_env = json.loads(payload)
        # Nats-Msg-Id is the STABLE artifact event id (dedupes outbox replays):
        self.assertEqual(headers.get("Nats-Msg-Id"), published_env["id"])
        self.assertEqual(published_env["causationid"], env["id"])

    def test_transport_outage_naks_and_outbox_retries_then_publishes(self):
        h, mod = self._world()
        runtime = mod.Runtime(h.sidecar)
        fake_js = FakeJS(fail_first=1)
        runtime._js = fake_js
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "rt2.md"),
            content="retry story",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        msg = FakeMsg("bloodbank.evt.agent.tool.completed", env)
        asyncio.run(runtime._handle(msg))
        # Capture was durably enqueued and the message was acked; the outbox
        # row recorded the failure and is still pending:
        self.assertTrue(msg._acked)
        pending = h.state.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 1)
        # Idle timer path: drain retries (after backoff elapses) and publishes:
        db = h.state._db
        db.execute("UPDATE outbox SET next_attempt_at = 0")
        db.commit()
        published = asyncio.run(runtime._drain_outbox())
        self.assertEqual(published, 1)
        self.assertEqual(len(fake_js.published), 1)

    def test_subject_type_mismatch_acked(self):
        h, mod = self._world()
        runtime = mod.Runtime(h.sidecar)
        runtime._js = FakeJS()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "x.md"),
            content="x",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        env["type"] = "bloodbank.conversation.turn.completed"  # mismatched
        msg = FakeMsg("bloodbank.evt.agent.tool.completed", env)
        asyncio.run(runtime._handle(msg))
        self.assertTrue(msg._acked)
        self.assertEqual(len(runtime._js.published), 0)

    def test_malformed_json_acked(self):
        h, mod = self._world()
        runtime = mod.Runtime(h.sidecar)
        runtime._js = FakeJS()
        msg = FakeMsg("bloodbank.evt.agent.tool.completed", {})
        msg.data = b"not json{"
        asyncio.run(runtime._handle(msg))
        self.assertTrue(msg._acked)

    def test_processing_crash_naks(self):
        h, mod = self._world()
        runtime = mod.Runtime(h.sidecar)
        runtime._js = FakeJS()
        env = tool_completed_envelope(
            path=str(h.tmp / "repos" / "bb" / "docs" / "y.md"),
            content="y",
            working_directory=str(h.tmp / "repos" / "bb"),
        )
        original = h.sidecar.process

        def crash(_e):
            raise RuntimeError("processing exploded")

        h.sidecar.process = crash
        try:
            msg = FakeMsg("bloodbank.evt.agent.tool.completed", env)
            asyncio.run(runtime._handle(msg))
        finally:
            h.sidecar.process = original
        self.assertTrue(msg._naked)

    def test_status_health_signal(self):
        # --status semantics live in amain; test the state contract here.
        h, _ = self._world()
        self.assertEqual(h.state.exhausted(), [])
        self.assertEqual(h.state.holds(), [])


class BackfillSinceTests(unittest.TestCase):
    """ARTIFACT_SIDECAR_BACKFILL_SINCE: bounded first-start backfill."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sidecar_main_backfill", HERE / "main.py"
        )
        assert spec and spec.loader
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_parse_accepts_z_and_offset(self):
        z = self.mod.parse_backfill_since("2026-10-16T18:17:21Z")
        off = self.mod.parse_backfill_since("2026-10-16T14:17:21-04:00")
        self.assertEqual(z, off)
        self.assertEqual(z.utcoffset().total_seconds(), 0)

    def test_parse_empty_is_none(self):
        self.assertIsNone(self.mod.parse_backfill_since(""))
        self.assertIsNone(self.mod.parse_backfill_since("   "))

    def test_parse_rejects_naive_and_garbage(self):
        with self.assertRaises(ValueError):
            self.mod.parse_backfill_since("2026-10-16T18:17:21")
        with self.assertRaises(ValueError):
            self.mod.parse_backfill_since("yesterday")

    def test_config_precedence_since_over_all_over_new(self):
        from nats.js.api import DeliverPolicy
        since = self.mod.parse_backfill_since("2026-10-16T18:17:21Z")
        c = self.mod.build_consumer_config("d", backfill=True, backfill_since=since)
        self.assertEqual(c.deliver_policy, DeliverPolicy.BY_START_TIME)
        self.assertEqual(c.opt_start_time, "2026-10-16T18:17:21.000000Z")
        self.assertEqual(
            self.mod.build_consumer_config("d", backfill=True).deliver_policy,
            DeliverPolicy.ALL,
        )
        c = self.mod.build_consumer_config("d")
        self.assertEqual(c.deliver_policy, DeliverPolicy.NEW)
        self.assertIsNone(c.opt_start_time)
        self.assertEqual(c.ack_policy.value, "explicit")


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="error")
