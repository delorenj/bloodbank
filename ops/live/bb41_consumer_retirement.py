#!/usr/bin/env python3
"""BB-41 read-only snapshot, retirement preflight and postcheck. No mutation API."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
import urllib.request

STREAM = "BLOODBANK_EVENTS"
TARGETS = {
    "n8n-l0J53w0TsS5PwETk-8b302a2c-5f47-5294-88de-9fbd52b2822b": (
        "l0J53w0TsS5PwETk", "bloodbank.evt.agent.invocation.completed"),
    "n8n-8mmqdMwQYA28ZwUj-d1000000-0000-4000-8000-000000000001": (
        "8mmqdMwQYA28ZwUj", "bloodbank.evt.repo.task.updated"),
}
TRIAGE = "6wAGA5pdrmHLyhs2"
CHIP = "wWXgCZiiIBWaRRzE"


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def stream(jsz):
    rows = [s for a in jsz["account_details"] for s in a["stream_detail"] if s["name"] == STREAM]
    require(len(rows) == 1, "expected exactly one BLOODBANK_EVENTS stream")
    row = rows[0]
    require(len(row["consumer_detail"]) == row["state"]["consumer_count"], "incomplete consumer inventory")
    return row


def consumer_map(snapshot):
    rows = stream(snapshot["jsz"])["consumer_detail"]
    require(len({c["name"] for c in rows}) == len(rows), "duplicate consumer names")
    return {c["name"]: c for c in rows}


def sqlite_ro(path):
    connection = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def capture(monitor, database):
    # Deliberately excludes credentials, code bodies, webhook headers and event payloads.
    with urllib.request.urlopen(monitor.rstrip("/") + "/jsz?streams=true&consumers=true&config=true", timeout=10) as response:
        jsz = json.load(response)
    consumers = stream(jsz)["consumer_detail"]
    with sqlite_ro(database) as db:
        workflows = {}
        for row in db.execute('SELECT id,name,active,nodes,connections,settings,updatedAt,activeVersionId FROM workflow_entity'):
            nodes = json.loads(row["nodes"])
            workflows[row["id"]] = {
                "definition_sha256": hashlib.sha256(json.dumps(
                    [row["nodes"], row["connections"], row["settings"]], separators=(",", ":")
                ).encode()).hexdigest(),
                "id": row["id"], "name": row["name"], "active": bool(row["active"]),
                "updated_at": row["updatedAt"], "active_version_id": row["activeVersionId"],
                "nodes": [{"id": n["id"], "name": n["name"], "type": n["type"],
                           "operation": n.get("parameters", {}).get("operation"),
                           "phase_guard": n.get("parameters", {}).get("phaseGuard"),
                           "events": n.get("parameters", {}).get("events")} for n in nodes],
            }
        # Workflow-off evidence must survive deletion of the durables that once named their owners.
        required_workflows = {TRIAGE, CHIP, *(wid for wid, _ in TARGETS.values())}
        owners = {wid: wf for wid, wf in workflows.items()
                  if wid in required_workflows or any(c["name"].startswith("n8n-" + wid + "-") for c in consumers)}
    return {"captured_at": datetime.now(timezone.utc).isoformat(), "jsz": jsz, "workflows": owners}


def health(snapshot, previous=None):
    """A snapshot proves polling now, NOT the duration of a polling outage.

    Report >24h as confirmed only with an earlier unhealthy observation and no
    delivery/ack progress. Otherwise report a candidate, never invent outage time.
    """
    now = utc(snapshot["jsz"]["now"])
    old = consumer_map(previous) if previous else {}
    results = []
    for name, c in sorted(consumer_map(snapshot).items()):
        if not name.startswith("n8n-"):
            continue
        cfg = c["config"]
        owners = [w for wid, w in snapshot["workflows"].items() if name.startswith("n8n-" + wid + "-")]
        wf = owners[0] if len(owners) == 1 else None
        push = bool(cfg.get("deliver_subject"))
        polling = bool(c.get("push_bound")) if push else c.get("num_waiting", 0) > 0
        last = c.get("delivered", {}).get("last_active") or c["created"]
        stale = (now - utc(last)).total_seconds() > 86400
        pending = c["num_pending"] > 0
        status = "healthy"
        if pending and not polling:
            status = "short_or_unknown_outage"
            if stale:
                status = "orphan_candidate_over_24h_idle"
                p = old.get(name)
                if p and previous:
                    older = (now - utc(previous["jsz"]["now"])).total_seconds() > 86400
                    was_idle = not (bool(p.get("push_bound")) if push else p.get("num_waiting", 0) > 0)
                    unchanged = all(p.get(k) == c.get(k) for k in ("created", "config", "delivered", "ack_floor"))
                    if older and was_idle and p.get("num_pending", 0) > 0 and unchanged:
                        status = "orphan_over_24h_observed"
            if name in TARGETS and wf and wf["active"] is False:
                status = "retirement_required"  # Not an active-lane outage, not silently healthy.
        results.append({"name": name, "mode": "push" if push else "pull", "polling_or_bound": polling,
                        "workflow_id": wf["id"] if wf else None, "workflow_active": wf["active"] if wf else None,
                        "pending": c["num_pending"], "unacked": c["num_ack_pending"], "last_active": last,
                        "status": status})
    return results


def validate_before(snapshot):
    current = consumer_map(snapshot)
    require(stream(snapshot["jsz"])["config"]["retention"] == "limits", "refuse non-limits retention")
    for name, (wid, subject) in TARGETS.items():
        require(name in current, f"target missing: {name}")
        c = current[name]
        require(c["config"]["durable_name"] == name, "durable identity mismatch")
        require(not c["config"].get("deliver_subject"), "target is no longer pull")
        require(c["config"].get("filter_subject") == subject and not c["config"].get("filter_subjects"), "filter drift")
        require(c["num_ack_pending"] == 0 and c["num_waiting"] == 0, "target has in-flight work or waiting pulls")
        require(snapshot["workflows"][wid]["active"] is False, "legacy workflow active")
        require(snapshot["workflows"][wid]["active_version_id"] is None, "legacy workflow published")
    for wid in (TRIAGE, CHIP):
        require(snapshot["workflows"][wid]["active"] is True, "replacement workflow inactive")
        cs = [c for n, c in current.items() if n.startswith("n8n-" + wid + "-")]
        require(len(cs) == 1 and cs[0]["num_waiting"] > 0 and cs[0]["num_pending"] == 0,
                "replacement consumer not polling or has backlog")
    require(not any(n["operation"] == "delegateTicket" for n in snapshot["workflows"][TRIAGE]["nodes"]),
            "triage includes legacy dispatch")


def plan(snapshot):
    validate_before(snapshot)
    state = stream(snapshot["jsz"])["state"]
    current = consumer_map(snapshot)
    return {
        "ticket": "BB-41", "live_mutation_authorized": False,
        "snapshot_at": snapshot["captured_at"], "stream_state": state,
        "disposition": "Skip obsolete delegation backlog; no replay. Delete consumer state only; retain stream data under its existing limits/age policy.",
        "targets": [{"name": name, "workflow_id": TARGETS[name][0],
                     "pending": current[name]["num_pending"], "unacked": current[name]["num_ack_pending"],
                     "retained_candidate_sequence_bounds_not_exact_filtered_range": [
                         max(state["first_seq"], current[name]["delivered"]["stream_seq"] + 1), state["last_seq"]],
                     "delete_api_subject": "$JS.API.CONSUMER.DELETE." + STREAM + "." + name}
                    for name in TARGETS],
        "health": health(snapshot),
    }


def compare_before(reviewed, fresh):
    validate_before(reviewed)
    validate_before(fresh)
    require(reviewed["jsz"]["server_id"] == fresh["jsz"]["server_id"], "broker identity changed")
    require(stream(reviewed["jsz"])["created"] == stream(fresh["jsz"])["created"], "stream recreated")
    require(stream(reviewed["jsz"])["config"] == stream(fresh["jsz"])["config"], "stream config changed")
    old, new = consumer_map(reviewed), consumer_map(fresh)
    require(set(old) == set(new), "consumer inventory changed; refresh review")
    for name in old:
        require(old[name]["config"] == new[name]["config"] and old[name]["created"] == new[name]["created"],
                f"consumer config/identity drift: {name}")
    for name in TARGETS:
        for key in ("delivered", "ack_floor"):
            require(old[name][key] == new[name][key], "legacy consumer progressed; investigate")
    for wid in (TRIAGE, CHIP, *(v[0] for v in TARGETS.values())):
        require(reviewed["workflows"][wid] == fresh["workflows"][wid], "workflow drift; refresh review")


def postcheck(before, after):
    old, new = consumer_map(before), consumer_map(after)
    require(set(old) - set(new) == set(TARGETS) and not set(new) - set(old), "not exactly the two intended deletions")
    require(before["jsz"]["server_id"] == after["jsz"]["server_id"], "broker identity changed")
    for key in ("created", "config"):
        require(stream(before["jsz"])[key] == stream(after["jsz"])[key], "stream changed")
    for name in new:
        require(old[name]["config"] == new[name]["config"] and old[name]["created"] == new[name]["created"],
                f"unrelated consumer changed: {name}")
    for wid in (TRIAGE, CHIP, *(v[0] for v in TARGETS.values())):
        require(before["workflows"][wid] == after["workflows"][wid], "workflow changed")
    require(all(after["workflows"][v[0]]["active"] is False for v in TARGETS.values()), "legacy reactivated")
    for wid in (TRIAGE, CHIP):
        rows = [c for name, c in new.items() if name.startswith("n8n-" + wid + "-")]
        require(len(rows) == 1 and rows[0]["num_waiting"] > 0 and rows[0]["num_pending"] == 0, "replacement not polling")
    return {"ok": True, "only_intended_consumers_removed": True, "handler_execution_proven": False,
            "remaining": "Attach actual fresh triage/Krebs handler evidence and a later no-recreation observation."}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("snapshot")
    cap.add_argument("--monitor", default="http://127.0.0.1:8222")
    cap.add_argument("--n8n-db", default="~/.n8n/database.sqlite")
    cap.add_argument("--out", type=Path, required=True)
    for name in ("plan", "audit", "validate-fresh", "postcheck"):
        cmd = sub.add_parser(name)
        cmd.add_argument("snapshot", type=Path)
        if name in ("validate-fresh", "postcheck"):
            cmd.add_argument("other", type=Path)
        if name == "audit":
            cmd.add_argument("--previous", type=Path)
    args = p.parse_args()
    try:
        if args.command == "snapshot":
            result = capture(args.monitor, args.n8n_db)
            # Refuse overwrite of evidence. Raw broker configuration contains no credentials here.
            with args.out.open("x") as f:
                json.dump(result, f, indent=2)
            print(json.dumps({"path": str(args.out), "sha256": hashlib.sha256(args.out.read_bytes()).hexdigest()}))
            return
        snap = json.loads(args.snapshot.read_text())
        if args.command == "plan":
            result = plan(snap)
        elif args.command == "audit":
            result = health(snap, json.loads(args.previous.read_text()) if args.previous else None)
        elif args.command == "validate-fresh":
            require(0 <= (utc(json.loads(args.other.read_text())["captured_at"]) - utc(json.loads(args.other.read_text())["jsz"]["now"])).total_seconds() <= 30,
                    "broker timestamp differs from capture time")
            require(0 <= (datetime.now(timezone.utc) - utc(json.loads(args.other.read_text())["captured_at"])).total_seconds() <= 300,
                    "fresh snapshot must be <=5 minutes old")
            compare_before(snap, json.loads(args.other.read_text()))
            result = {"ok": True, "live_mutation_authorized": False}
        else:
            result = postcheck(snap, json.loads(args.other.read_text()))
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        p.exit(1, f"BLOCKED: {exc}\n")


if __name__ == "__main__":
    main()
