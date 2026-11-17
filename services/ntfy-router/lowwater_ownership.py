"""Owner-path local control, not a bus event or a second source detector."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

LOW_WATER = "bloodbank.game.concept_pool.breached"
PROJECT = "tower-of-lost-things"
POOL = "curated-plane-novel"
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://33god.dev/control/tolt/lowwater/1")
WAITING = "awaiting-owner-grant"
IMPORTED = "owner-grant-imported"


def canonical(value):
    # Forge's saved intent codec, including ASCII escaping, binds exact original bytes.
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identity(parts):
    return str(uuid.uuid5(NAMESPACE, json.dumps(parts, ensure_ascii=True, separators=(",", ":"))))


def fence_id(episode):
    return identity(["fence", PROJECT, POOL, episode])


def grant_id(grant):
    return identity(["grant", grant["barrier_id"], PROJECT, POOL, grant["episode_id"],
                     grant["event_id"], grant["destination"], grant["envelope_sha256"]])


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate control key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


class ControlUnavailable(ValueError):
    pass


class OwnershipControl:
    def __init__(self, config, ledger, validator, formatter):
        from jsonschema import Draft202012Validator
        schema = json.loads(Path(__file__).with_name("lowwater_control.schema.json").read_text())
        self.validators = {name: Draft202012Validator({**schema, "$ref": "#/$defs/" + name})
                           for name in ("barrier", "handoff")}
        self.config, self.ledger, self.validator, self.formatter = config, ledger, validator, formatter
        self.pending_cursor = 0
        with ledger.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS lowwater_barrier (
                    scope TEXT PRIMARY KEY, barrier_id TEXT NOT NULL UNIQUE,
                    body TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lowwater_grant (
                    event_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL UNIQUE,
                    episode_id TEXT NOT NULL UNIQUE, fence_id TEXT NOT NULL UNIQUE,
                    destination TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL,
                    grant_digest TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lowwater_conflict (
                    scope TEXT NOT NULL, rejected_digest TEXT NOT NULL,
                    PRIMARY KEY(scope,rejected_digest)
                );
                CREATE TABLE IF NOT EXISTS lowwater_ack (
                    ack_id TEXT PRIMARY KEY, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lowwater_import_scan (
                    source_root TEXT PRIMARY KEY, names_json TEXT NOT NULL,
                    next_index INTEGER NOT NULL
                );
            """)

    def _validate(self, name, value):
        if not self.validators[name].is_valid(value):
            raise ControlUnavailable("invalid-local-control")

    def _trusted(self, fd, directory=False):
        info = os.fstat(fd)
        if (info.st_uid != self.config.ownership_uid or info.st_mode & 0o022
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
            raise ControlUnavailable("untrusted-control-path")

    def read(self, name):
        root = self.config.ownership_dir
        if root is None or root.resolve() != root:
            raise ControlUnavailable("untrusted-control-root")
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._trusted(fd, True)
            if name.startswith("grants/"):
                child = os.open("grants", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                self._trusted(fd, True)
                name = name.removeprefix("grants/")
            if "/" in name or name in (".", ".."):
                raise ControlUnavailable("invalid-control-name")
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                self._trusted(child)
                with os.fdopen(child, "rb", closefd=False) as f:
                    raw = f.read(600001)
                if len(raw) > 600000:
                    raise ControlUnavailable("oversize-control")
                return strict_json(raw)
            finally:
                os.close(child)
        finally:
            os.close(fd)

    def barrier(self):
        value = self.read("barrier.json")
        self._validate("barrier", value)
        destination_matches = value["destination"] == self.config.destination
        body, hashed = canonical(value), digest(value)
        with self.ledger.connection(True) as db:
            prior = db.execute("SELECT * FROM lowwater_barrier WHERE scope=?", (POOL,)).fetchone()
            if prior:
                if prior["digest"] != hashed:
                    db.execute("UPDATE lowwater_barrier SET state='conflict' WHERE scope=?", (POOL,))
                    self._conflict(db, "barrier", hashed, all_grants=True)
                if prior["digest"] != hashed or prior["state"] == "conflict":
                    invalid = True
                else:
                    invalid = False
            else:
                if destination_matches:
                    db.execute("INSERT INTO lowwater_barrier VALUES (?,?,?,?,?)",
                               (POOL, value["barrier_id"], body, hashed, "active"))
                invalid = False
        if invalid:
            raise ControlUnavailable("immutable-barrier-conflict")
        if not destination_matches:
            raise ControlUnavailable("barrier-destination-mismatch")
        return value

    def _conflict(self, db, scope, hashed, all_grants=False):
        db.execute("INSERT OR IGNORE INTO lowwater_conflict VALUES (?,?)", (scope, hashed))
        if all_grants:
            db.execute("UPDATE lowwater_grant SET status='conflict',reason='immutable-barrier-conflict'")
            db.execute("UPDATE delivery SET state='conflict',reason='immutable-barrier-conflict' "
                       "WHERE event_id IN (SELECT event_id FROM lowwater_grant)")
        else:
            db.execute("UPDATE lowwater_grant SET status='conflict',reason='immutable-ownership-conflict' "
                       "WHERE event_id=?", (scope,))
            db.execute("UPDATE delivery SET state='conflict',reason='immutable-ownership-conflict' "
                       "WHERE event_id=?", (scope,))

    @staticmethod
    def source_matches(event):
        return (event["type"] == LOW_WATER and event["data"]["project_slug"] == PROJECT
                and event["data"]["pool_id"] == POOL
                and event["source"] == "urn:33god:cli:ltf-concept-alerts"
                and event.get("producer") == "ltf-concept-alerts"
                and event.get("service") == "lost-things-forge"
                and event["actor"] == {"type": "service", "agent_id": "ltf-concept-alerts"}
                and event["data"]["provenance"]["record_ref"] ==
                "repo://lost-things-forge/controller/concept-low-water/" + event["data"]["episode_id"])

    def _offer(self, db, event, request, reason=""):
        # Reuse the existing delivery digest codec; its historical rows stay immutable.
        from tolt_family import canonical as delivery_json
        destination = self.config.destination
        env_json, req_json = delivery_json(event), delivery_json(request)
        hashed = hashlib.sha256(delivery_json([event, request]).encode()).hexdigest()
        rows = db.execute("SELECT * FROM delivery WHERE event_id=?", (event["id"],)).fetchall()
        if any(r["destination"] != destination or r["digest"] != hashed for r in rows):
            self._conflict(db, event["id"], hashed)
            return "conflict"
        if rows:
            row = rows[0]
            registered = db.execute("SELECT 1 FROM lowwater_grant WHERE event_id=? AND status='accepted'",
                                    (event["id"],)).fetchone()
            if row["state"] == "pending" and row["reason"] != WAITING and not registered:
                db.execute("UPDATE delivery SET state='suppressed',reason='lowwater-legacy-intent' "
                           "WHERE event_id=? AND state='pending'", (event["id"],))
                return "suppressed"
            if row["state"] == "pending" and reason:
                db.execute("UPDATE delivery SET state='suppressed',reason=? WHERE event_id=? AND state='pending'",
                           (reason, event["id"]))
                return "suppressed"
            return row["state"]
        state = "suppressed" if reason else "pending"
        db.execute("INSERT INTO delivery "
                   "(event_id,destination,event_type,envelope_json,request_json,digest,state,reason) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (event["id"], destination, LOW_WATER, env_json, req_json, hashed, state, reason or WAITING))
        return state

    def receive(self, event, request):
        reason = ""
        if not self.source_matches(event):
            reason = "lowwater-source-not-eligible"
        elif not self.config.authorized:
            reason = "cutover-not-accepted"
        try:
            barrier = self.barrier()
            if event["data"]["episode_id"] in barrier["excluded_episode_ids"]:
                reason = "lowwater-legacy-episode"
        except (OSError, ValueError):
            pass  # Recoverable waiting, never an HTTP authorization.
        with self.ledger.connection(True) as db:
            return self._offer(db, event, request, reason)

    def _check_handoff(self, handoff, barrier):
        self._validate("handoff", handoff)
        grant, proof = handoff["grant"], handoff["publication"]
        event = strict_json(handoff["envelope_body"])
        if self.validator is None:
            raise ControlUnavailable("canonical-validator-unavailable")
        self.validator(event)
        d = event["data"]
        hashed = hashlib.sha256(handoff["envelope_body"].encode()).hexdigest()
        if (not self.source_matches(event) or canonical(event) != handoff["envelope_body"]
                or grant["barrier_id"] != barrier["barrier_id"]
                or grant["admission_barrier_id"] != barrier["barrier_id"]
                or grant["destination"] != self.config.destination
                or grant["episode_id"] in barrier["excluded_episode_ids"]
                or grant["episode_id"] != d["episode_id"] or grant["event_id"] != event["id"]
                or grant["eligible_count"] != d["eligible_count"] or grant["observed_at"] != event["time"]
                or event["time"] != d["observed_at"]
                or grant["envelope_sha256"] != hashed
                or grant["grant_id"] != grant_id(grant) or grant["fence_id"] != fence_id(d["episode_id"])
                or proof["message_id"] != event["id"] or proof["envelope_sha256"] != hashed):
            raise ControlUnavailable("ownership-binding-mismatch")
        return event

    def _ack(self, db, status, reason, hashed, grant=None, state=None):
        grant = grant or {}
        ack = {"schema": "bloodbank.tolt-lowwater-ack/1",
               "ack_id": identity(["ack", grant.get("grant_id"), hashed, status, reason]),
               "grant_id": grant.get("grant_id"), "event_id": grant.get("event_id"),
               "handoff_sha256": hashed, "status": status, "reason": reason,
               "durable_import": status in ("accepted", "duplicate"), "delivery_state": state}
        db.execute("INSERT OR IGNORE INTO lowwater_ack VALUES (?,?)", (ack["ack_id"], canonical(ack)))
        return json.loads(db.execute("SELECT body FROM lowwater_ack WHERE ack_id=?",
                                     (ack["ack_id"],)).fetchone()["body"])

    def import_file(self, name):
        # Only a trusted path is accepted, never a caller-supplied grant/event object.
        hashed = None  # Unreadable input has no known handoff digest; never invent one.
        grant = None
        loaded = False
        try:
            handoff = self.read("grants/" + name)
            loaded = True
            hashed = digest(handoff)
            barrier = self.barrier()
            event = self._check_handoff(handoff, barrier)
            grant = handoff["grant"]
            if name != grant["grant_id"] + ".json":
                raise ControlUnavailable("grant-filename-mismatch")
            request = self.formatter(event, self.config)
        except Exception:
            with self.ledger.connection(True) as db:
                candidate = handoff.get("grant", {}) if loaded and isinstance(handoff, dict) else {}
                if not isinstance(candidate, dict):
                    candidate = {}
                priors = db.execute("SELECT * FROM lowwater_grant WHERE grant_id=? OR event_id=? OR episode_id=?",
                                    (name.removesuffix(".json"), str(candidate.get("event_id", "")),
                                     str(candidate.get("episode_id", "")))).fetchall()
                if loaded and any(prior["digest"] != hashed for prior in priors):
                    for prior in priors:
                        self._conflict(db, prior["event_id"], hashed)
                    return self._ack(db, "conflict", "immutable-ownership-conflict",
                                     hashed, json.loads(priors[0]["body"])["grant"], "conflict")
                return self._ack(db, "rejected", "control-unavailable-or-invalid", hashed)
        with self.ledger.connection(True) as db:
            priors = db.execute("SELECT * FROM lowwater_grant WHERE event_id=? OR grant_id=? OR episode_id=?",
                                (event["id"], grant["grant_id"], grant["episode_id"])).fetchall()
            pinned = db.execute("SELECT * FROM lowwater_barrier WHERE scope=?", (POOL,)).fetchone()
            # Files/formatting were validated before this transaction. A competing
            # importer may since have latched a permanent barrier conflict.
            if (not pinned or pinned["state"] != "active"
                    or pinned["barrier_id"] != barrier["barrier_id"]
                    or pinned["digest"] != digest(barrier)):
                if not priors:
                    self._offer(db, event, request)
                    db.execute("INSERT INTO lowwater_grant VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (event["id"], grant["grant_id"], grant["episode_id"], grant["fence_id"],
                                self.config.destination, canonical(handoff), hashed, digest(grant),
                                "conflict", "immutable-barrier-conflict"))
                for prior in priors:
                    self._conflict(db, prior["event_id"], hashed)
                self._conflict(db, event["id"], hashed)
                return self._ack(db, "conflict", "immutable-barrier-conflict",
                                 hashed, grant, "conflict")
            if priors:
                if any(p["digest"] != hashed or p["destination"] != self.config.destination for p in priors):
                    for prior in priors:
                        self._conflict(db, prior["event_id"], hashed)
                    return self._ack(db, "conflict", "immutable-ownership-conflict", hashed, grant, "conflict")
                prior = priors[0]
                self._offer(db, event, request)
                current = db.execute("SELECT status FROM lowwater_grant WHERE event_id=?",
                                     (event["id"],)).fetchone()
                if current["status"] == "conflict":
                    return self._ack(db, "conflict", "immutable-ownership-conflict",
                                     hashed, grant, "conflict")
                status = "duplicate" if prior["status"] == "accepted" else prior["status"]
                row = db.execute("SELECT state FROM delivery WHERE event_id=? AND destination=?",
                                 (event["id"], self.config.destination)).fetchone()
                return self._ack(db, status, prior["reason"], hashed, grant, row["state"] if row else None)
            reason = "" if self.config.authorized else "cutover-not-accepted"
            state = self._offer(db, event, request, reason)
            status = ("conflict" if state == "conflict" else
                      "accepted" if state == "pending" else "rejected")
            reason = "durable-import-only" if status == "accepted" else "legacy-held-or-conflict"
            db.execute("INSERT INTO lowwater_grant VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (event["id"], grant["grant_id"], grant["episode_id"], grant["fence_id"],
                        self.config.destination, canonical(handoff), hashed, digest(grant), status, reason))
            if state == "pending":
                db.execute("UPDATE delivery SET reason=? WHERE event_id=? AND reason=? AND state='pending'",
                           (IMPORTED, event["id"], WAITING))
            return self._ack(db, status, reason, hashed, grant, state)

    def permits(self, event_id):
        with self.ledger.connection() as db:
            row = db.execute("SELECT * FROM lowwater_grant WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            # Old pending rows must not acquire the new recoverable wait marker.
            current = self.ledger.get(event_id, self.config.destination)
            if current and current["reason"] != WAITING:
                self.ledger.suppress_pending(event_id, self.config.destination, "lowwater-legacy-intent")
            elif current:
                try:
                    event = json.loads(current["envelope_json"])
                    if event["data"]["episode_id"] in self.barrier()["excluded_episode_ids"]:
                        self.ledger.suppress_pending(event_id, self.config.destination, "lowwater-legacy-episode")
                except (OSError, ValueError):
                    pass
            return False
        if row["status"] != "accepted" or row["destination"] != self.config.destination:
            return False
        try:
            handoff = self.read("grants/" + row["grant_id"] + ".json")
            if digest(handoff) != row["digest"]:
                with self.ledger.connection(True) as db:
                    self._conflict(db, event_id, digest(handoff))
                return False
            self._check_handoff(handoff, self.barrier())
        except Exception:
            return False
        return True

    def reconcile_pending(self, limit):
        with self.ledger.connection() as db:
            def page(after):
                return db.execute("SELECT rowid AS scan_id,event_id FROM delivery WHERE event_type=? "
                                  "AND destination=? AND state='pending' AND rowid>? ORDER BY rowid LIMIT ?",
                                  (LOW_WATER, self.config.destination, after, limit)).fetchall()
            rows = page(self.pending_cursor)
            if not rows:
                rows = page(0)
        for row in rows:
            self.permits(row["event_id"])  # No HTTP, including while global gates are closed.
            self.pending_cursor = row["scan_id"]

    def scan(self, limit):
        root = self.config.ownership_dir
        if root is None:
            return []
        with self.ledger.connection(True) as db:
            cursor = db.execute("SELECT * FROM lowwater_import_scan WHERE source_root=?",
                                (str(root),)).fetchone()
            names, index = (json.loads(cursor["names_json"]), cursor["next_index"]) if cursor else ([], 0)
            if (not isinstance(names, list) or not all(isinstance(n, str) for n in names)
                    or type(index) is not int or not 0 <= index <= len(names)):
                raise ControlUnavailable("corrupt-import-navigation")
            if index == len(names):
                # Freeze membership, not only a lexical upper bound: new filenames
                # may sort before any retained file and must wait for the next round.
                names = sorted(p.name for p in (root / "grants").glob("*.json"))
                index = 0
            page = names[index:index + limit]
            db.execute("INSERT INTO lowwater_import_scan VALUES (?,?,?) ON CONFLICT(source_root) "
                       "DO UPDATE SET names_json=excluded.names_json,next_index=excluded.next_index",
                       (str(root), canonical(names), index + len(page)))
        # Page reservation survives fresh CLI/restart and serializes concurrent scans.
        # A crash here defers unprocessed files to the next finite round. Every
        # selected file still passes trusted-path, schema and immutable checks.
        return [self.import_file(name) for name in page]

    def status(self, grant):
        with self.ledger.connection() as db:
            # Historical ACK and current authority/delivery share one read snapshot.
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM lowwater_grant WHERE grant_id=?", (grant,)).fetchone()
            if row is None:
                db.execute("COMMIT")
                return {"status": "not-imported", "durable_import": False}
            ack = json.loads(db.execute("SELECT body FROM lowwater_ack WHERE "
                                       "json_extract(body,'$.grant_id')=? ORDER BY rowid DESC LIMIT 1",
                                       (grant,)).fetchone()["body"])
            delivery = db.execute("SELECT state,reason,attempts,http_status,message_id FROM delivery "
                                  "WHERE event_id=? AND destination=?",
                                  (row["event_id"], row["destination"])).fetchone()
            pinned = db.execute("SELECT state,barrier_id FROM lowwater_barrier WHERE scope=?",
                                (POOL,)).fetchone()
            saved_grant = json.loads(row["body"])["grant"]
            current = (row["status"] if pinned and pinned["state"] == "active"
                       and pinned["barrier_id"] == saved_grant["barrier_id"] else "conflict")
            db.execute("COMMIT")
            return {"import_ack": ack, "ownership_status": current,
                    "delivery": dict(delivery) if delivery else None}


def main():
    parser = argparse.ArgumentParser(description="Trusted local low-water import/status; never HTTP")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--import-pending", action="store_true")
    group.add_argument("--status", metavar="GRANT_UUID")
    args = parser.parse_args()
    from tolt_family import Config, DeliveryLedger, CanonicalValidator, format_message
    config = Config.from_env()
    if config.ownership_dir is None:
        parser.error("ownership directory is not configured")
    control = OwnershipControl(config, DeliveryLedger(config.state_path),
                               CanonicalValidator(config.validator_root), format_message)
    result = control.scan(config.drain_limit) if args.import_pending else control.status(args.status)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
