#!/usr/bin/env python3
"""plane-webhook-bridge — turn self-hosted Plane webhooks into repo-scoped
Bloodbank events so each PM reacts to changes on ITS board in real time.

Flow:  Plane (self-hosted) --webhook--> this bridge --NATS--> the PM's consumer
       (subscribes bloodbank.evt.v1.repo.<repo>.>) --> inbox --> gateway turn.

Routing (the ground-truth contract): a repo-scoped event's CloudEvents `type`
is `bloodbank.v1.repo.<repo>.ticket_<action>` (the repo name is the ENTITY
token), so the NATS subject `bloodbank.evt.v1.repo.<repo>.ticket_<action>`
matches exactly what each PM's bloodbank-consumer already listens on. The Plane
`project` id -> repo mapping comes from ~/.hermes/agents-registry.yaml
(agents.<id>.plane.project_id / .repo).

One fleet-wide service (systemd user unit). Configure ONE Plane workspace
webhook pointing at it; it fans every project's events to the right PM.

Env: HERMES_PLANE_BRIDGE_PORT (default 8477), PLANE_WEBHOOK_SECRET (optional
HMAC verify), HERMES_FLEET_REGISTRY_FILE, BLOODBANK_HOME.

Run:      plane-webhook-bridge.py            # serve
Test:     plane-webhook-bridge.py --selftest # synthetic payload, no NATS/Plane
"""
from __future__ import annotations
import hashlib, hmac, json, os, sys, uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("HERMES_PLANE_BRIDGE_HOST", "0.0.0.0")  # 0.0.0.0 so Plane's
#   docker containers reach it via host.docker.internal:PORT. HMAC (below) is the
#   guard; set to 127.0.0.1 to restrict to same-host.
PORT = int(os.environ.get("HERMES_PLANE_BRIDGE_PORT", "8477"))
SECRET = os.environ.get("PLANE_WEBHOOK_SECRET", "")
REGISTRY = Path(os.environ.get("HERMES_FLEET_REGISTRY_FILE", os.path.expanduser("~/.hermes/agents-registry.yaml")))
KIND_MARKER = "evt"

# Plane event -> (whether we care, the entity-action verb we tack on the type).
# Only issue/comment changes drive PM reactivity; cycles/modules/pages ignored.
CARE = {"issue", "issue_comment"}


def load_project_map() -> dict[str, str]:
    """plane project_id -> repo, from the agents registry (best-effort, reloaded per request)."""
    try:
        import yaml
        reg = yaml.safe_load(REGISTRY.read_text())
    except Exception:
        return {}
    agents = reg.get("agents", reg) if isinstance(reg, dict) else {}
    out = {}
    for _id, a in (agents.items() if isinstance(agents, dict) else []):
        if not isinstance(a, dict):
            continue
        p = a.get("plane") or {}
        pid, repo = p.get("project_id"), a.get("repo")
        if pid and repo:
            out[str(pid)] = str(repo)
    return out


def build_envelope(repo: str, action: str, data: dict) -> tuple[str, dict]:
    """(subject, envelope). type=bloodbank.v1.repo.<repo>.ticket_<action> (5 tokens)."""
    ce_type = f"bloodbank.v1.repo.{repo}.ticket_{action}"
    subject = f"bloodbank.{KIND_MARKER}.v1.repo.{repo}.ticket_{action}"
    env = {
        "specversion": "1.0", "type": ce_type, "id": str(uuid.uuid4()),
        "source": "plane://webhook-bridge", "subject": subject,
        "time": datetime.now(timezone.utc).isoformat(),
        "datacontenttype": "application/json", "kind": "event", "domain": "repo",
        "producer": "service:plane-webhook-bridge", "service": "plane-webhook-bridge",
        "actor": {"type": "service", "agent_id": "plane-webhook-bridge"},
        "ordering_key": f"repo:{repo}",
        "correlationid": str(uuid.uuid4()), "causationid": str(uuid.uuid4()),
        "data": data,
    }
    return subject, env


def resolve(payload: dict, pmap: dict[str, str]):
    """-> (repo, action, data) or None if not a routable ticket event."""
    event = payload.get("event")
    if event not in CARE:
        return None
    action = payload.get("action", "updated")
    data = payload.get("data") or {}
    # issue_comment carries the issue under data.issue; issue carries it directly.
    issue = data if event == "issue" else (data.get("issue") or data)
    pid = str(data.get("project") or issue.get("project") or "")
    repo = pmap.get(pid)
    if not repo:
        return None
    slim = {
        "repo": repo, "event": event, "action": action, "project_id": pid,
        "ticket_id": issue.get("id"), "sequence_id": issue.get("sequence_id"),
        "name": issue.get("name"), "state": issue.get("state"),
        "updated_by": data.get("updated_by") or data.get("actor"),
    }
    return repo, ("commented" if event == "issue_comment" else action), slim


def publish(subject: str, env: dict) -> str:
    bb = os.environ.get("BLOODBANK_HOME", os.path.expanduser("~/code/33GOD/bloodbank"))
    core = os.path.join(bb, "services", "agent-hooks", "core")
    sys.path.insert(0, core)
    from nats_publish import publish as nats_publish  # type: ignore
    nats_publish(subject, json.dumps(env).encode(), client_name="plane-webhook-bridge")
    return subject


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):  # quiet default logging
        pass

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "projects": len(load_project_map())})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if SECRET:
            sig = self.headers.get("X-Plane-Signature", "")
            good = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, good):
                self._send(401, {"error": "bad signature"}); return
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            self._send(400, {"error": "bad json"}); return
        r = resolve(payload, load_project_map())
        if not r:
            self._send(200, {"ok": True, "routed": False}); return  # ack, ignore
        repo, action, data = r
        subject, env = build_envelope(repo, action, data)
        try:
            publish(subject, env)
        except Exception as e:
            print(f"[bridge] publish failed for {subject}: {e}", file=sys.stderr)
            self._send(502, {"error": "publish failed", "subject": subject}); return
        print(f"[bridge] {payload.get('event')}/{action} -> {subject} (ticket {data.get('sequence_id')})")
        self._send(200, {"ok": True, "routed": True, "subject": subject})


def selftest():
    pmap = load_project_map()
    sample = next(iter(pmap.items()), (None, None))
    pid, repo = sample
    print(f"registry projects: {len(pmap)}; sample {pid} -> {repo}")
    payload = {"event": "issue", "action": "updated",
               "data": {"project": pid, "id": "abc123", "sequence_id": 42,
                        "name": "Fix the thing", "state": "started"}}
    r = resolve(payload, pmap)
    if not r:
        print("SELFTEST: no route (registry empty or no plane project mapping)"); return 1
    repo, action, data = r
    subject, env = build_envelope(repo, action, data)
    print(f"SELFTEST: issue.updated on project {pid} -> repo {repo}")
    print(f"  subject: {subject}")
    print(f"  consumer listens: bloodbank.evt.v1.repo.{repo}.>  -> MATCH: "
          f"{subject.startswith(f'bloodbank.evt.v1.repo.{repo}.')}")
    print(f"  type:    {env['type']}  (5 tokens: {len(env['type'].split('.')) == 5})")
    print(f"  data:    {json.dumps(data)}")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[bridge] listening on {HOST}:{PORT} ; {len(load_project_map())} plane projects mapped"
          f"{' ; HMAC on' if SECRET else ' ; HMAC OFF (set PLANE_WEBHOOK_SECRET)'}")
    srv.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
