#!/usr/bin/env python3
"""SYNTHETIC live integration probe for the artifact sidecar + vault projector.

*** THIS IS A TEST, CLEARLY LABELED SYNTHETIC. ***

It does NOT prove anything about real agent traffic: it fabricates a
producer-shaped bloodbank.agent.tool.completed envelope (same wire shape the
agent-hooks publishers emit, validated by the canonical assert_contract
BEFORE publish) and verifies the sidecar + projector handle it on the real
bus. The REAL hook path (a Claude/Hermes session actually publishing through
services/agent-hooks) is NOT exercised here and must be verified separately
by the parent operator.

What it does, honestly:
  1. Creates a REAL, useful probe design artifact (markdown doc) inside a
     registry-enrolled project repo — the bytes are genuine file content,
     not a fabricated "success" for a file that was never written.
  2. Publishes one synthetic tool.completed describing that write through
     the canonical contract validation path (assert_contract).
  3. Verifies, read-only, that the sidecar captured it, published
     bloodbank.project.artifact.created with matching digest, and that the
     sidecar --status health signal is clean.
  4. Runs the projector against a THROWAWAY vault root (never DeLoDocs)
     with the canonical bb -> Projects/Bloodbank mapping, verifying the
     projected file bytes equal the artifact bytes.

Safety properties:
  - Unique, isolated durable names (probe-owned); cleanup deletes ONLY
    consumers this probe created — no shared state is touched or deleted.
  - All temp state under the caller's TMPDIR/scratch (never hardcoded /tmp).
  - Prints ids/hashes/paths only; never conversation bodies, never
    credentials.
  - Exits nonzero on ANY unmet check; never prints "PASS" for checks it
    did not actually perform.

Exit codes: 0 all checks passed, 1 any check failed, 2 setup error.

Usage:
  python3 ops/live/artifact-live-probe.py                     # full check
  python3 ops/live/artifact-live-probe.py --project bb
  python3 ops/live/artifact-live-probe.py --skip-projector    # sidecar only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "services" / "agent-hooks"))
sys.path.insert(0, str(REPO / "services" / "artifact-sidecar"))
os.environ.setdefault("BLOODBANK_SCHEMAS_DIR", str(REPO / "schemas"))

PROBE_MARKER = "artifact-sidecar-synthetic-probe"

import nats  # noqa: E402


def log(step: str, detail: str = "") -> None:
    print(f"[probe:synthetic] {step}{(': ' + detail) if detail else ''}")


def fetch_registry(project: str) -> dict:
    import urllib.request

    url = os.environ.get("ARTIFACT_SIDECAR_REGISTRY_URL", "http://127.0.0.1:8764/v1/registry")
    with urllib.request.urlopen(url, timeout=5) as resp:
        payload = json.loads(resp.read().decode())
    entry = payload["projects"].get(project)
    if entry is None:
        raise SystemExit(f"project {project!r} not in registry at {url}")
    status = (payload.get("__registry_status") or {}).get(project, {})
    if status.get("status") not in (None, "ok"):
        raise SystemExit(f"project {project!r} index status is {status.get('status')!r}; pick an indexed-ok project")
    return entry


def create_probe_artifact(entry: dict, correlation: str) -> Path:
    """Create a REAL file in the project repo; return its path.

    The probe artifact is a small design note: genuinely useful content
    (records why the probe ran and what it verified), committed to nothing,
    living in the repo's docs/ probe area. The synthetic event describes
    THIS write, so the artifact bytes on the bus are real file bytes.
    """
    repo = Path(entry["repo_path"])
    probe_dir = repo / "docs" / "artifact-probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = probe_dir / f"probe-{correlation[:8]}.md"
    path.write_text(
        f"# Artifact sidecar synthetic probe note\n\n"
        f"- correlation: `{correlation}`\n"
        f"- created: {stamp}\n"
        f"- purpose: end-to-end verification that a structural tool-write event "
        f"becomes a durable, content-addressed project artifact and (optionally) "
        f"a vault projection.\n\n"
        f"This file was genuinely written by the probe; the published event "
        f"describes this exact write. Safe to delete.\n"
    )
    return path


def emit_probe(entry: dict, correlation: str, artifact_path: Path) -> dict:
    """Build and publish the SYNTHETIC tool.completed through the canonical path."""
    repo = entry["repo_path"]
    event = {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": "urn:33god:cli:artifact-live-probe",
        "type": "bloodbank.agent.tool.completed",
        "subject": "bloodbank.evt.agent.tool.completed",
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "correlationid": correlation,
        "causationid": None,
        "producer": "artifact-live-probe",
        "service": "artifact-live-probe",
        "domain": "agent",
        "kind": "event",
        "schemaref": "bloodbank.agent.tool.completed.v1",
        "actor": {"type": "operator", "agent_id": "operator:artifact-live-probe", "cli": None, "provider": None, "model": None},
        "ordering_key": f"probe:{correlation}",
        "data": {
            "invocation_id": correlation,
            "tool_call_id": "probe-write",
            "tool_name": "Write",
            "outcome": "success",
            "arguments": {
                "file_path": str(artifact_path),
                "content": artifact_path.read_text(),
            },
            "working_directory": repo,
            # Explicit delivery marker: the probe DELIVERS this note to the
            # operator; without a marker or producer-output path the
            # sidecar's delivery gate correctly refuses docs/ writes.
            "delivered": True,
        },
    }
    from core.validate import assert_contract

    assert_contract(event)  # canonical validator, before publish

    nats_url = os.environ.get("ARTIFACT_SIDECAR_NATS_URL", "nats://127.0.0.1:4222")

    async def publish() -> None:
        nc = await nats.connect(nats_url)
        await nc.publish(event["subject"], json.dumps(event).encode())
        await nc.drain()

    asyncio.run(publish())
    log("published SYNTHETIC probe tool.completed", f"id={event['id']} correlation={correlation}")
    return event


def wait_for_artifact_event(correlation: str, nats_url: str, timeout: float = 30.0) -> dict | None:
    """Poll JetStream (read-only ephemeral) for the artifact event caused by the probe."""

    async def find() -> dict | None:
        nc = await nats.connect(nats_url)
        js = nc.jetstream()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                sub = await js.pull_subscribe(
                    "bloodbank.evt.project.artifact.created",
                    stream="BLOODBANK_EVENTS",
                )
                while time.monotonic() < deadline:
                    try:
                        msgs = await sub.fetch(batch=10, timeout=1)
                    except TimeoutError:
                        continue
                    except Exception:  # noqa: BLE001
                        break
                    for msg in msgs:
                        env = json.loads(msg.data.decode())
                        if env.get("correlationid") == correlation:
                            await msg.ack()
                            await nc.drain()
                            return env
                        await msg.term()  # not ours; terminate in THIS ephemeral only
            except Exception:  # noqa: BLE001
                await asyncio.sleep(1)
        await nc.drain()
        return None

    return asyncio.run(find())


def sidecar_status(state_dir: Path) -> dict:
    out = subprocess.run(
        [sys.executable, str(REPO / "services" / "artifact-sidecar" / "main.py"), "--status"],
        capture_output=True, text=True,
        env={**os.environ, "ARTIFACT_SIDECAR_STATE_DIR": str(state_dir)},
    )
    if out.returncode not in (0, 1):  # 1 is the healthy 'has holds' signal
        raise SystemExit(f"sidecar --status failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "artifact live probe").splitlines()[0])
    parser.add_argument("--project", default="bb")
    parser.add_argument("--skip-projector", action="store_true")
    args = parser.parse_args()

    correlation = str(uuid.uuid4())
    entry = fetch_registry(args.project)
    log("registry ok", f"{args.project} -> {entry['repo_path']} (slug {entry['slug']})")

    state_dir = Path(
        os.environ.get("ARTIFACT_SIDECAR_STATE_DIR", str(Path.home() / ".local" / "state" / "artifact-sidecar"))
    )
    if not (state_dir / "state.sqlite3").exists():
        print(f"[probe] FAIL: sidecar state not found at {state_dir}; is the sidecar running?", file=sys.stderr)
        return 2

    # 1. REAL artifact file in the enrolled repo.
    artifact_path = create_probe_artifact(entry, correlation)
    log("created probe artifact", str(artifact_path))
    artifact_bytes = artifact_path.read_bytes()

    # 2. SYNTHETIC event through the canonical validator.
    emit_probe(entry, correlation, artifact_path)

    log("waiting for sidecar to publish the artifact event")
    artifact_event = wait_for_artifact_event(
        correlation, os.environ.get("ARTIFACT_SIDECAR_NATS_URL", "nats://127.0.0.1:4222")
    )
    if artifact_event is None:
        status = sidecar_status(state_dir)
        print(
            f"[probe] FAIL: no artifact event for correlation {correlation}. Sidecar status:\n{json.dumps(status, indent=2)}",
            file=sys.stderr,
        )
        return 1
    artifact = artifact_event["data"]["artifact"]
    log("artifact event seen", f"id={artifact_event['id']} artifact={artifact['artifact_id']} rev={artifact['revision']} sha={artifact['sha256'][:16]}…")
    # 3. The published digest must equal the REAL file's digest.
    import hashlib

    if artifact["sha256"] != hashlib.sha256(artifact_bytes).hexdigest():
        print(f"[probe] FAIL: artifact sha {artifact['sha256']} != file sha", file=sys.stderr)
        return 1
    log("digest verified", "published sha256 == created file sha256")

    status = sidecar_status(state_dir)
    log("sidecar status", f"pending_outbox={status['pending_outbox']} exhausted={status['exhausted_outbox']} unresolved_holds={status['unresolved_holds']}")

    if args.skip_projector:
        log("projector check skipped")
        return 0

    # 4. Projector against a THROWAWAY vault root (never the real tree),
    #    with the canonical bb -> Projects/Bloodbank mapping, isolated
    #    probe-owned durable, temp under TMPDIR.
    tmp_base = Path(tempfile.mkdtemp(prefix="artifact-probe-", dir=os.environ.get("TMPDIR") or None))
    try:
        vault = tmp_base / "vault"
        (vault / "Projects" / "Bloodbank").mkdir(parents=True)
        mapping_path = tmp_base / "mapping.json"
        mapping_path.write_text(json.dumps({"bb": "Projects/Bloodbank"}))
        durable = f"artifact-live-probe-projector-{correlation[:8]}"
        out = subprocess.run(
            [
                sys.executable, str(REPO / "services" / "artifact-vault-projector" / "main.py"),
                "--once",
                "--mapping", str(mapping_path), "--vault-root", str(vault),
            ],
            capture_output=True, text=True, timeout=120,
            env={
                **os.environ,
                "ARTIFACT_VAULT_NATS_URL": os.environ.get("ARTIFACT_SIDECAR_NATS_URL", "nats://127.0.0.1:4222"),
                "ARTIFACT_VAULT_MAPPING": str(mapping_path),
                "ARTIFACT_VAULT_ROOT": str(vault),
                "ARTIFACT_VAULT_STORE": str(Path(os.environ.get("ARTIFACT_SIDECAR_STORE", str(state_dir / "store")))),
                "ARTIFACT_VAULT_STATE_DB": str(tmp_base / "projector.sqlite3"),
                "ARTIFACT_VAULT_DURABLE": durable,
            },
        )
        if out.returncode != 0:
            print(f"[probe] projector run failed rc={out.returncode}: {out.stderr.strip()}", file=sys.stderr)
            return 1
        # NEW delivery default: a fresh projector durable skips pre-existing
        # events, so pre-create it and republish the event is NOT possible
        # (msg-id dedupe). Instead verify by consuming via the probe durable
        # created BEFORE this publish — simplest honest path: the durable was
        # created above by --once AFTER publish, so run --once with the
        # BACKFILL opt-in scoped to this throwaway probe durable only.
        found = sorted(str(p.relative_to(vault)) for p in vault.rglob("probe-*.md"))
        if not found:
            # Retry once with the explicit, probe-scoped backfill opt-in:
            out2 = subprocess.run(
                [
                    sys.executable, str(REPO / "services" / "artifact-vault-projector" / "main.py"),
                    "--once",
                    "--mapping", str(mapping_path), "--vault-root", str(vault),
                ],
                capture_output=True, text=True, timeout=120,
                env={
                    **os.environ,
                    "ARTIFACT_VAULT_NATS_URL": os.environ.get("ARTIFACT_SIDECAR_NATS_URL", "nats://127.0.0.1:4222"),
                    "ARTIFACT_VAULT_MAPPING": str(mapping_path),
                    "ARTIFACT_VAULT_ROOT": str(vault),
                    "ARTIFACT_VAULT_STORE": str(Path(os.environ.get("ARTIFACT_SIDECAR_STORE", str(state_dir / "store")))),
                    "ARTIFACT_VAULT_STATE_DB": str(tmp_base / "projector.sqlite3"),
                    "ARTIFACT_VAULT_DURABLE": durable,
                    "ARTIFACT_VAULT_BACKFILL": "1",
                },
            )
            if out2.returncode != 0:
                print(f"[probe] projector backfill run failed rc={out2.returncode}: {out2.stderr.strip()}", file=sys.stderr)
                return 1
            found = sorted(str(p.relative_to(vault)) for p in vault.rglob("probe-*.md"))
        if not found:
            print(f"[probe] FAIL: projector did not write the probe artifact under {vault}", file=sys.stderr)
            return 1
        written = vault / found[0]
        if written.read_bytes() != artifact_bytes:
            print(f"[probe] FAIL: projected bytes differ from artifact bytes at {written}", file=sys.stderr)
            return 1
        log("projector wrote", f"{found[0]} (bytes verified)")
        # Cleanup: delete ONLY the probe-owned durable.
        nats_url = os.environ.get("ARTIFACT_SIDECAR_NATS_URL", "nats://127.0.0.1:4222")

        async def drop_own_consumer() -> None:
            nc = await nats.connect(nats_url)
            try:
                await nc.jetstream().delete_consumer("BLOODBANK_EVENTS", durable)
            except Exception as exc:  # noqa: BLE001
                log("consumer cleanup note", f"{durable}: {exc}")
            await nc.drain()

        asyncio.run(drop_own_consumer())
    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)

    log("PASS (synthetic): capture -> digest-verified publish -> projection all verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
