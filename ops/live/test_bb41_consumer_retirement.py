"""Isolated BB-41 fixtures: no sockets, services or production databases."""
import copy
import importlib.util
import asyncio
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
import subprocess
import threading
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import bb41_delete_consumers as delete

spec = importlib.util.spec_from_file_location("retirement", Path(__file__).with_name("bb41_consumer_retirement.py"))
assert spec is not None and spec.loader is not None
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def fixture():
    consumers = []
    workflows = {}
    for name, (wid, subject) in r.TARGETS.items():
        consumers.append({"name": name, "created": "2026-11-01T00:00:00Z",
                          "config": {"durable_name": name, "filter_subject": subject},
                          "num_pending": 12, "num_ack_pending": 0, "num_waiting": 0,
                          "delivered": {"stream_seq": 50, "last_active": "2026-11-14T00:00:00Z"},
                          "ack_floor": {"stream_seq": 50}})
        workflows[wid] = {"id": wid, "active": False, "active_version_id": None, "nodes": []}
    for wid in (r.TRIAGE, r.CHIP):
        name = "n8n-" + wid + "-node"
        consumers.append({"name": name, "created": "2026-11-01T00:00:00Z", "config": {"durable_name": name},
                          "num_pending": 0, "num_ack_pending": 0, "num_waiting": 1,
                          "delivered": {"stream_seq": 60}, "ack_floor": {"stream_seq": 60}})
        workflows[wid] = {"id": wid, "active": True, "nodes": []}
    return {"captured_at": "2026-11-17T19:00:00Z", "workflows": workflows,
            "jsz": {"now": "2026-11-17T19:00:00Z", "server_id": "fixture-broker",
                    "account_details": [{"stream_detail": [{"name": r.STREAM, "created": "2026-01-01T00:00:00Z",
                         "config": {"retention": "limits"}, "state": {"first_seq": 1, "last_seq": 100, "consumer_count": 4},
                         "consumer_detail": consumers}]}]}}


def active_orphan(push=False):
    snap = fixture()
    # Use a non-retired identity so an active outage cannot be hidden as retirement.
    c = r.stream(snap["jsz"])["consumer_detail"][0]
    c["name"] = "n8n-active-node"
    c["config"]["durable_name"] = c["name"]
    if push:
        c["config"]["deliver_subject"] = "delivery.fixture"
        c["push_bound"] = False
    snap["workflows"]["active"] = {"id": "active", "active": True}
    return snap, c


class HealthTests(unittest.TestCase):
    def status(self, snap, previous=None):
        return next(x["status"] for x in r.health(snap, previous) if x["name"] == "n8n-active-node")

    def test_real_orphan_over_24h_is_flagged_for_push_and_pull(self):
        for push in (True, False):
            with self.subTest(push=push):
                snap, c = active_orphan(push)
                previous = copy.deepcopy(snap)
                previous["jsz"]["now"] = "2026-11-16T18:59:59Z"
                self.assertEqual(self.status(snap, previous), "orphan_over_24h_observed")

    def test_bound_push_and_polling_pull_not_flagged_despite_old_last_delivery(self):
        for push in (True, False):
            snap, c = active_orphan(push)
            if push:
                c["push_bound"] = True
            else:
                c["num_waiting"] = 1
            self.assertEqual(self.status(snap), "healthy")

    def test_zero_pending_not_flagged(self):
        snap, c = active_orphan()
        c["num_pending"] = 0
        self.assertEqual(self.status(snap), "healthy")

    def test_short_outage_not_over_24h(self):
        snap, c = active_orphan(True)
        c["delivered"]["last_active"] = "2026-11-17T18:30:00Z"
        self.assertEqual(self.status(snap), "short_or_unknown_outage")

    def test_missing_push_bound_on_pull_is_not_failure(self):
        snap, c = active_orphan()
        c["num_waiting"] = 1
        self.assertNotIn("push_bound", c)
        self.assertEqual(self.status(snap), "healthy")

    def test_one_snapshot_cannot_prove_outage_duration(self):
        snap, c = active_orphan()
        self.assertEqual(self.status(snap), "orphan_candidate_over_24h_idle")

    def test_progress_prevents_confirmed_orphan(self):
        snap, c = active_orphan()
        previous = copy.deepcopy(snap)
        previous["jsz"]["now"] = "2026-11-16T18:00:00Z"
        r.consumer_map(previous)[c["name"]]["delivered"]["stream_seq"] -= 1
        self.assertNotEqual(self.status(snap, previous), "orphan_over_24h_observed")

    def test_inactive_known_retirement_separate_from_outage(self):
        results = r.health(fixture())
        self.assertEqual(sum(x["status"] == "retirement_required" for x in results), 2)


class GuardTests(unittest.TestCase):
    def test_plan_lists_only_exact_ids_and_is_not_authorization(self):
        result = r.plan(fixture())
        self.assertEqual({x["name"] for x in result["targets"]}, set(r.TARGETS))
        self.assertFalse(result["live_mutation_authorized"])

    def test_refuses_active_inflight_push_filter_and_retention_drift(self):
        edits = [lambda s: s["workflows"][next(iter(r.TARGETS.values()))[0]].update(active=True),
                 lambda s: r.stream(s["jsz"])["consumer_detail"][0].update(num_waiting=1),
                 lambda s: r.stream(s["jsz"])["consumer_detail"][0].update(num_ack_pending=1),
                 lambda s: r.stream(s["jsz"])["consumer_detail"][0]["config"].update(deliver_subject="push"),
                 lambda s: r.stream(s["jsz"])["consumer_detail"][0]["config"].update(filter_subject="wrong"),
                 lambda s: r.stream(s["jsz"])["config"].update(retention="workqueue"),
                 lambda s: s["workflows"][r.TRIAGE]["nodes"].append({"operation": "delegateTicket"})]
        for edit in edits:
            snap = fixture()
            edit(snap)
            with self.assertRaises(ValueError):
                r.plan(snap)

    def test_missing_replacement_blocks_plan(self):
        snap = fixture()
        r.stream(snap["jsz"])["consumer_detail"][2]["num_waiting"] = 0
        with self.assertRaises(ValueError):
            r.plan(snap)

    def test_incomplete_inventory_blocks(self):
        snap = fixture()
        r.stream(snap["jsz"])["state"]["consumer_count"] = 5
        with self.assertRaises(ValueError):
            r.plan(snap)

    def test_fresh_allows_only_counter_growth_not_legacy_progress(self):
        snap = fixture()
        fresh = copy.deepcopy(snap)
        r.stream(fresh["jsz"])["consumer_detail"][0]["num_pending"] += 1
        r.compare_before(snap, fresh)
        r.stream(fresh["jsz"])["consumer_detail"][0]["delivered"]["stream_seq"] += 1
        with self.assertRaises(ValueError):
            r.compare_before(snap, fresh)

    def test_fresh_rejects_unrelated_config_drift_and_recreation(self):
        for key, value in (("created", "2026-11-17T18:59:00Z"), ("config", {"durable_name": "changed"})):
            snap = fixture()
            fresh = copy.deepcopy(snap)
            r.stream(fresh["jsz"])["consumer_detail"][2][key] = value
            with self.assertRaises(ValueError):
                r.compare_before(snap, fresh)

    def after(self, before):
        snap = copy.deepcopy(before)
        stream = r.stream(snap["jsz"])
        stream["consumer_detail"] = [c for c in stream["consumer_detail"] if c["name"] not in r.TARGETS]
        stream["state"]["consumer_count"] -= 2
        return snap

    def test_exact_postcheck_does_not_claim_handler_success(self):
        before = fixture()
        result = r.postcheck(before, self.after(before))
        self.assertTrue(result["ok"])
        self.assertFalse(result["handler_execution_proven"])

    def test_postcheck_rejects_no_delete_extra_delete_config_drift_or_reactivation(self):
        before = fixture()
        for mutate in (lambda s: s.update(before),
                       lambda s: r.stream(s["jsz"])["consumer_detail"].pop(),
                       lambda s: r.stream(s["jsz"])["consumer_detail"][0]["config"].update(ack_wait=1),
                       lambda s: s["workflows"][next(iter(r.TARGETS.values()))[0]].update(active=True)):
            after = self.after(before)
            mutate(after)
            with self.assertRaises(ValueError):
                r.postcheck(before, after)

    def test_database_connection_is_query_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.sqlite"
            import sqlite3
            sqlite3.connect(path).close()
            with r.sqlite_ro(path) as db:
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("CREATE TABLE forbidden (id int)")


class CaptureBoundaryTests(unittest.TestCase):
    def test_http_sqlite_snapshots_postcheck_and_workflow_reactivation_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "n8n.sqlite"
            with sqlite3.connect(database) as db:
                db.execute('''CREATE TABLE workflow_entity (
                    id TEXT, name TEXT, active INTEGER, nodes TEXT, connections TEXT,
                    settings TEXT, updatedAt TEXT, activeVersionId TEXT)''')
                for wid in (*[entry[0] for entry in r.TARGETS.values()], r.TRIAGE, r.CHIP, "unrelated"):
                    db.execute('INSERT INTO workflow_entity VALUES (?,?,?,?,?,?,?,?)',
                               (wid, wid, int(wid in (r.TRIAGE, r.CHIP)), "[]", "{}", "{}",
                                "2026-11-17T19:00:00Z", None))
            state = {"jsz": copy.deepcopy(fixture()["jsz"]), "paths": []}

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    state["paths"].append(self.path)
                    if self.path != "/jsz?streams=true&consumers=true&config=true":
                        self.send_error(404)
                        return
                    body = json.dumps(state["jsz"]).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, format, *args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            command = [sys.executable, "-B", str(Path(__file__).with_name("bb41_consumer_retirement.py"))]

            def run(*arguments):
                return subprocess.run([*command, *map(str, arguments)], capture_output=True, text=True, timeout=15)

            def snapshot(name):
                path = root / name
                proc = run("snapshot", "--monitor", f"http://127.0.0.1:{server.server_port}",
                           "--n8n-db", database, "--out", path)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return path, json.loads(path.read_text())

            try:
                before_path, before = snapshot("before.json")
                self.assertEqual(set(before["workflows"]), {r.TRIAGE, r.CHIP, *[v[0] for v in r.TARGETS.values()]})
                self.assertEqual(run("plan", before_path).returncode, 0)
                stream = r.stream(state["jsz"])
                stream["consumer_detail"] = [c for c in stream["consumer_detail"] if c["name"] not in r.TARGETS]
                stream["state"]["consumer_count"] = len(stream["consumer_detail"])
                after_path, after = snapshot("after.json")
                self.assertEqual(set(r.consumer_map(before)) - set(r.consumer_map(after)), set(r.TARGETS))
                for wid, _ in r.TARGETS.values():
                    self.assertIn(wid, after["workflows"])
                    self.assertFalse(after["workflows"][wid]["active"])
                checked = run("postcheck", before_path, after_path)
                self.assertEqual(checked.returncode, 0, checked.stderr)
                self.assertTrue(json.loads(checked.stdout)["only_intended_consumers_removed"])

                with sqlite3.connect(database) as db:
                    db.execute("UPDATE workflow_entity SET active=1 WHERE id=?", (next(iter(r.TARGETS.values()))[0],))
                reactivated_path, reactivated = snapshot("reactivated.json")
                self.assertTrue(reactivated["workflows"][next(iter(r.TARGETS.values()))[0]]["active"])
                denied = run("postcheck", before_path, reactivated_path)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn("workflow changed", denied.stderr)
                self.assertEqual(state["paths"], ["/jsz?streams=true&consumers=true&config=true"] * 3)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class DeleteTests(unittest.TestCase):
    def test_requests_only_exact_two_deletes(self):
        request = AsyncMock(return_value=SimpleNamespace(data=b'{"success":true}'))
        result = asyncio.run(delete.delete_exact(request))
        self.assertEqual(len(result), 2)
        self.assertEqual([c.args[0] for c in request.call_args_list],
                         ["$JS.API.CONSUMER.DELETE." + r.STREAM + "." + n for n in r.TARGETS])
        self.assertTrue(all(c.args[1] == b"" for c in request.call_args_list))

    def test_first_failure_stops_before_second_delete(self):
        request = AsyncMock(return_value=SimpleNamespace(data=b'{"error":{"code":500}}'))
        with self.assertRaises(ValueError):
            asyncio.run(delete.delete_exact(request))
        self.assertEqual(request.await_count, 1)

    def test_no_network_before_review_and_authorization_gates(self):
        args = SimpleNamespace(execute="wrong")
        with patch.object(delete.guard, "capture") as capture:
            with self.assertRaises(ValueError):
                asyncio.run(delete.run(args))
            capture.assert_not_called()

    def test_full_live_runner_offline_including_snapshot_journal_and_postcheck(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed = fixture()
            snap = root / "before.json"
            snap.write_text(json.dumps(reviewed))
            handler = root / "handler.json"
            handler.write_text('{"offline_fixture":true}')
            args = SimpleNamespace(execute="DELETE-ONLY-BB-41-RETIRED-DURABLES-NO-REPLAY",
                reviewed=snap, reviewed_sha256=hashlib.sha256(snap.read_bytes()).hexdigest(),
                handler_evidence=handler, handler_sha256=hashlib.sha256(handler.read_bytes()).hexdigest(),
                review_ref="fixture-independent-review", authorization_ref="fixture-authorization",
                evidence_dir=root / "operation", monitor="http://127.0.0.1:8222", n8n_db="offline-fixture")
            before = copy.deepcopy(reviewed)
            # Time in the offline fixture is fixed; matching broker/capture times are intentional.
            after = GuardTests().after(before)
            calls = []
            async def request(subject, data, timeout):
                calls.append(subject)
                if ".CONSUMER.INFO." in subject:
                    name = subject.split(".CONSUMER.INFO." + r.STREAM + ".")[1]
                    return SimpleNamespace(data=json.dumps(r.consumer_map(before)[name]).encode())
                # Snapshots and the operation record MUST exist before any delete.
                self.assertTrue((args.evidence_dir / "before.json").exists())
                self.assertFalse(json.loads((args.evidence_dir / "operation.json").read_text())["complete"])
                return SimpleNamespace(data=b'{"success":true}')
            nc = SimpleNamespace(connected_server_version="fixture", _server_info={"server_id":"fixture-broker"},
                                 request=request, close=AsyncMock())
            fake_nats = SimpleNamespace(connect=AsyncMock(return_value=nc))
            with patch.dict(sys.modules, {"nats": fake_nats}), patch.object(delete.guard, "capture", side_effect=[before, after]):
                asyncio.run(delete.run(args))
            operation = json.loads((args.evidence_dir / "operation.json").read_text())
            self.assertTrue(operation["complete"])
            self.assertEqual(len(operation["results"]), 2)
            self.assertEqual(len(calls), 4)
            self.assertTrue(operation["postcheck"]["ok"])
            nc.close.assert_awaited_once()

    def test_digest_review_authorization_and_output_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snap = root / "before.json"
            evidence = root / "handler.json"
            snap.write_text(json.dumps(fixture()))
            evidence.write_text('{"fixture":true}')
            args = SimpleNamespace(execute="DELETE-ONLY-BB-41-RETIRED-DURABLES-NO-REPLAY",
                reviewed=snap, reviewed_sha256=hashlib.sha256(snap.read_bytes()).hexdigest(),
                handler_evidence=evidence, handler_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
                review_ref="fixture-review", authorization_ref="fixture-authorization", evidence_dir=root / "new")
            delete.verify_review(args)
            for key, value in (("reviewed_sha256", "wrong"), ("handler_sha256", "wrong"),
                               ("review_ref", " "), ("authorization_ref", ""), ("evidence_dir", root)):
                bad = copy.copy(args)
                setattr(bad, key, value)
                with self.assertRaises(ValueError):
                    delete.verify_review(bad)


if __name__ == "__main__":
    unittest.main()
