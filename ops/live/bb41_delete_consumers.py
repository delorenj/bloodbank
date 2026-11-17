#!/usr/bin/env python3
"""BB-41 operator-only live step. Never run before independent review/authorization."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import os

import bb41_consumer_retirement as guard


def save_json(path, value):
    with path.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


async def delete_exact(request):
    """Only two hardcoded consumer-delete subjects; injected request for offline tests."""
    results = []
    for name in guard.TARGETS:
        subject = "$JS.API.CONSUMER.DELETE." + guard.STREAM + "." + name
        response = await request(subject, b"", timeout=10)
        body = json.loads(response.data)
        guard.require(body.get("success") is True and not body.get("error"),
                      f"delete failed for {name}; stop, preserve partial evidence, do not recreate or retry blindly")
        results.append({"name": name, "success": True})
    return results


def verify_review(args):
    guard.require(args.execute == "DELETE-ONLY-BB-41-RETIRED-DURABLES-NO-REPLAY", "explicit execution phrase required")
    guard.require(bool(args.review_ref.strip()), "independent review reference required")
    guard.require(bool(args.authorization_ref.strip()), "separate live authorization reference required")
    guard.require(hashlib.sha256(args.reviewed.read_bytes()).hexdigest() == args.reviewed_sha256,
                  "reviewed snapshot digest mismatch")
    guard.require(hashlib.sha256(args.handler_evidence.read_bytes()).hexdigest() == args.handler_sha256,
                  "reviewed handler evidence digest mismatch")
    guard.require(not args.evidence_dir.exists(), "output directory exists; use a new private evidence directory")


async def run(args):
    verify_review(args)  # No network access before explicit gates.
    reviewed = json.loads(args.reviewed.read_text())
    before = guard.capture(args.monitor, args.n8n_db)
    guard.require(0 <= (guard.utc(before["captured_at"]) - guard.utc(before["jsz"]["now"])).total_seconds() <= 30,
                  "broker timestamp differs from fresh capture")
    guard.compare_before(reviewed, before)
    # The current broker is fixed to localhost:4222, paired with local monitoring.
    guard.require(args.monitor.rstrip("/") == "http://127.0.0.1:8222", "refuse alternate broker monitor")
    import nats
    nc = await nats.connect("nats://127.0.0.1:4222", name="bb41-authorized-consumer-retirement")
    try:
        # Pair monitor identity to the connected server, and re-read each target using INFO only.
        guard.require(nc.connected_server_version is not None, "NATS connection not established")
        info = nc._server_info  # nats-py exposes server_id here; refuse ambiguous monitor/client pairing.
        guard.require(info.get("server_id") == before["jsz"]["server_id"], "NATS/monitor broker identity mismatch")
        current = guard.consumer_map(before)
        for name in guard.TARGETS:
            response = await nc.request("$JS.API.CONSUMER.INFO." + guard.STREAM + "." + name, b"", timeout=10)
            c = json.loads(response.data)
            guard.require(not c.get("error"), "target INFO failed")
            guard.require(all(c.get(k) == current[name].get(k) for k in ("name", "created", "config", "delivered", "ack_floor")),
                          "target changed after fresh snapshot")
            guard.require(c.get("num_waiting") == 0 and c.get("num_ack_pending") == 0, "target acquired work")
        args.evidence_dir.mkdir(mode=0o700, parents=False)
        record = {"review_ref": args.review_ref, "authorization_ref": args.authorization_ref,
                  "reviewed_sha256": args.reviewed_sha256, "handler_sha256": args.handler_sha256,
                  "disposition": guard.plan(before), "results": [], "complete": False}
        save_json(args.evidence_dir / "before.json", before)
        journal = args.evidence_dir / "operation.json"
        save_json(journal, record)
        async def recorded_request(subject, data, timeout):
            record["attempting"] = subject
            save_json(journal, record)  # Persist intent before request; timeout outcome may be unknown.
            response = await nc.request(subject, data, timeout=timeout)
            record["results"].append({"subject": subject, "response": json.loads(response.data)})
            record["attempting"] = None
            save_json(journal, record)
            return response
        await delete_exact(recorded_request)
        after = guard.capture(args.monitor, args.n8n_db)
        save_json(args.evidence_dir / "after.json", after)
        record["postcheck"] = guard.postcheck(before, after)
        record["complete"] = True
        save_json(journal, record)
        print(json.dumps({"ok": True, "evidence_dir": str(args.evidence_dir),
                          "remaining": "Later no-recreation snapshot and actual fresh replacement handler evidence required."}))
    finally:
        await nc.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reviewed", required=True, type=Path)
    p.add_argument("--reviewed-sha256", required=True)
    p.add_argument("--handler-evidence", required=True, type=Path)
    p.add_argument("--handler-sha256", required=True)
    p.add_argument("--review-ref", required=True)
    p.add_argument("--authorization-ref", required=True)
    p.add_argument("--execute", required=True)
    p.add_argument("--evidence-dir", required=True, type=Path)
    p.add_argument("--monitor", default="http://127.0.0.1:8222")
    p.add_argument("--n8n-db", default="~/.n8n/database.sqlite")
    args = p.parse_args()
    os.umask(0o077)
    try:
        asyncio.run(run(args))
    except Exception as exc:
        # No auto-retry or rollback: restoring an obsolete durable could replay history.
        p.exit(1, f"STOP: {type(exc).__name__}: {exc}\nInspect operation.json for partial results. Do not recreate/replay.\n")


if __name__ == "__main__":
    main()
