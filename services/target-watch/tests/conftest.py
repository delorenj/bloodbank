"""Shared fixtures: a schema tree with the watch contract, fake targets, fake bus and runners.

The schemas track (feat/watch-domain-schemas) registers the watch domain and adds
x-sender to the inbound schemas. Until it is on main, the tree used here is the
repo's own schemas overlaid with tests/fixtures (the watch schemas and x-sender
blocks copied from that branch); once it lands, the x-sender overlay switches itself
off (real_tree_has_x_sender) so the real files are what the suite checks. The validator allowlists are patched the same way.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from target_watch import envelope as env
from target_watch.paths import repo_root

FIXTURES = Path(__file__).parent / "fixtures"

# Fake handles only: this repo is public.
JANE_EMAIL = "jane.doe@example.com"
JANE_PHONE = "+12155550100"
JANE_LABEL = "Jane Doe"


def real_tree_has_x_sender() -> bool:
    """True once the schemas track has landed: the repo's own inbound schemas carry x-sender."""
    return any('"x-sender"' in p.read_text() for p in (repo_root() / "schemas" / "bloodbank").rglob("*.json"))


@pytest.fixture(scope="session")
def schema_tree(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("schemas")
    shutil.copytree(repo_root() / "schemas", root, dirs_exist_ok=True)
    for fixture in (FIXTURES / "schemas").rglob("*.json"):
        dest = root / fixture.relative_to(FIXTURES / "schemas")
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(fixture, dest)
    blocks = json.loads((FIXTURES / "x-sender.json").read_text())
    if real_tree_has_x_sender():
        blocks = {}  # the real files win and are tested as they are: no overlay can mask a missing block
    for ce_type, block in blocks.items():
        _, domain, entity, action = ce_type.split(".")
        path = root / "bloodbank" / domain / f"{entity}.{action}.json"
        schema = json.loads(path.read_text())
        if "x-sender" not in schema:
            schema["x-sender"] = block
            path.write_text(json.dumps(schema, indent=2))
    return root


@pytest.fixture(autouse=True)
def contract(schema_tree, monkeypatch):
    """Point the repo validator at the overlay tree and register the watch domain if needed."""
    monkeypatch.setenv("BLOODBANK_SCHEMAS_DIR", str(schema_tree))
    monkeypatch.setenv("TARGET_WATCH_SCHEMAS_DIR", str(schema_tree))
    validate = env._validator()
    if "watch" not in validate.ALLOWED_DOMAINS:
        monkeypatch.setattr(validate, "ALLOWED_DOMAINS", validate.ALLOWED_DOMAINS | {"watch"})
    if "target" not in validate.ALLOWED_ENTITIES:
        monkeypatch.setattr(validate, "ALLOWED_ENTITIES", validate.ALLOWED_ENTITIES | {"target"})
    validate._load_schema.cache_clear()
    validate._build_registry.cache_clear()
    yield validate
    validate._load_schema.cache_clear()
    validate._build_registry.cache_clear()


@pytest.fixture
def plane_repo(tmp_path) -> Path:
    repo = tmp_path / "client-repo"
    repo.mkdir()
    (repo / ".project.json").write_text(json.dumps({"ticket_provider": "plane"}))
    return repo


def write_target(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(body)
    return path


@pytest.fixture
def jane_file(tmp_path, plane_repo) -> Path:
    return write_target(tmp_path / "watch", "jane", f"""
[target]
id = "jane-doe"
display_name = "Jane Doe"

[target.handles]
email = ["Jane Doe <Jane.Doe@Example.com>", "jd@example.org"]
e164 = ["(215) 555-0100"]
label = ["{JANE_LABEL}", "Janie D"]
platform = ["slack:U0JANE", "telegram:4242"]

[sink]
kind = "plane"
repo = "{plane_repo}"
state = "Todo"
labels = ["submitted-by-client"]
priority = "medium"
coalesce_hours = 24
notify = ["telegram:1000"]
""")


@pytest.fixture
def registry(jane_file):
    from target_watch.targets import TargetRegistry

    return TargetRegistry([str(jane_file.parent / "*.toml")])


@pytest.fixture
def index(schema_tree):
    from target_watch.xsender import XSenderIndex

    return XSenderIndex(schema_tree, recheck_s=0)


@pytest.fixture
def state(tmp_path):
    from target_watch.state import State

    return State(tmp_path / "state" / "state.db")


class FakeJS:
    """Records publishes; answers a PubAck like JetStream (duplicate on a repeated Nats-Msg-Id)."""

    def __init__(self, fail: int = 0):
        self.calls: list[tuple[str, dict, dict]] = []
        self.ids: set[str] = set()
        self.fail = fail

    async def publish(self, subject, body, headers=None, stream=None, timeout=None):
        if self.fail:
            self.fail -= 1
            raise ConnectionError("bus down")
        event = json.loads(body)
        self.calls.append((subject, event, headers or {}))
        duplicate = headers["Nats-Msg-Id"] in self.ids
        self.ids.add(headers["Nats-Msg-Id"])
        return SimpleNamespace(stream="BLOODBANK_EVENTS", seq=len(self.calls), duplicate=duplicate)

    @property
    def events(self) -> list[dict]:
        return [event for _, event, _ in self.calls]


@pytest.fixture
def fake_js():
    return FakeJS()


@pytest.fixture
def publisher(fake_js):
    from target_watch.bus import Publisher

    return Publisher(fake_js)


class FakeRunner:
    """Stands in for subprocess: px and hermes answers keyed by argv[1]."""

    def __init__(self, ticket_start: int = 450):
        self.calls: list[tuple[list[str], str | None]] = []
        self.next_ticket = ticket_start
        self.create_output: str | None = None
        self.create_code = 0
        self.hermes_code = 0
        self.op_code = 0
        self.op_key = "fake-telnyx-key-DO-NOT-LEAK"

    @staticmethod
    def verb(argv) -> str:
        # hermes is called as `hermes -p <profile> send ...`
        return argv[3] if len(argv) > 3 and argv[1] == "-p" else argv[1]

    def __call__(self, argv, cwd=None, timeout=None):
        self.calls.append((list(argv), cwd))
        verb = self.verb(argv)
        if verb == "task":
            if self.create_output is not None:
                return self.create_code, self.create_output, ""
            ref = f"JIMB-{self.next_ticket}"
            self.next_ticket += 1
            return 0, f"created {ref} [Todo] {argv[3]}\n  https://plane.example/ws/projects/x/issues/abc\n", ""
        if verb == "comment":
            return 0, f"commented on {argv[2]}\n  {argv[2]} title\n  https://plane.example/x\n", ""
        if verb == "send":
            if self.hermes_code:
                return self.hermes_code, '{"error": "Platform not configured"}', ""
            return 0, '{"success": true, "platform": "telegram", "message_id": "1"}', ""
        if verb == "read":  # fake `op read --no-newline <ref>`
            if self.op_code:
                return self.op_code, "", "[ERROR] not signed in"
            return 0, self.op_key, ""
        raise AssertionError(f"unexpected command {argv}")

    def of(self, verb: str) -> list[list[str]]:
        return [argv for argv, _ in self.calls if self.verb(argv) == verb]


@pytest.fixture
def runner():
    return FakeRunner()


def inbound(ce_type: str, data: dict, *, event_id: str = "0b9c6a62-1111-4d0e-8f5f-000000000001", time: str = "2026-11-04T15:00:00.000Z") -> dict:
    """A source envelope shaped like the live producers (n8n)."""
    return {
        "specversion": "1.0", "id": event_id, "type": ce_type, "source": "urn:33god:integration:n8n",
        "time": time, "producer": "n8n", "service": "n8n", "domain": ce_type.split(".")[1], "kind": "event",
        "correlationid": "5a0d4c1e-2222-5d0e-9f5f-000000000002", "causationid": None,
        "actor": {"type": "service", "agent_id": "bloodbank.integration.n8n"},
        "ordering_key": "line:cell", "data": data,
    }


def rcs(sender: str = JANE_PHONE, text: str = "Can you come by Tuesday?", *, message_id: str = "rcs-1",
        received_at: str = "2026-11-04T15:00:00Z", from_name=None, event_id: str = "0b9c6a62-1111-4d0e-8f5f-000000000001",
        thread_id="thread-9") -> dict:
    return inbound("bloodbank.messaging.rcs.received", {
        "message_id": message_id, "line": "cell", "source": "android-notification", "source_app": None,
        "from": sender, "from_name": from_name, "to": "cell", "text": text, "media": [],
        "received_at": received_at, "verification_code": None, "thread_id": thread_id,
    }, event_id=event_id)


def email(sender: str = JANE_EMAIL, text: str = "Invoice question\n\nHi, quick one.", *, direction: str = "inbound",
          display_name: str = "Jane Doe", message_id: str = "gm-1", thread_id: str = "gt-1",
          event_id: str = "0b9c6a62-1111-4d0e-8f5f-000000000003") -> dict:
    sender_obj = {"handle": sender}
    if display_name:
        sender_obj["display_name"] = display_name
    return inbound("bloodbank.messaging.email.received", {
        "schema_version": 1, "direction": direction, "sender": sender_obj,
        "recipient": {"handle": "operator@example.net"}, "content": {"text": text, "format": "plain"},
        "received_at": "2026-11-04T15:01:00Z", "message_id": message_id, "thread_id": thread_id,
    }, event_id=event_id)


def call_ended(sender: str = JANE_PHONE, disposition: str = "voicemail", *, direction: str = "inbound",
               call_id: str = "call-77", event_id: str = "0b9c6a62-1111-4d0e-8f5f-000000000004") -> dict:
    return inbound("bloodbank.telephony.call.ended", {
        "call_id": call_id, "line": "+12155550199", "direction": direction, "from": sender, "to": "+12155550199",
        "disposition": disposition, "duration_seconds": 0, "hangup_cause": "timeout",
        "ended_at": "2026-11-04T15:02:00Z", "remote_name": JANE_LABEL, "forwarded_from": None,
    }, event_id=event_id)


def transcription(sender: str = JANE_PHONE, text: str = "Hi, it's Jane, call me back.", *, call_id: str = "call-77",
                  event_id: str = "0b9c6a62-1111-4d0e-8f5f-000000000005") -> dict:
    return inbound("bloodbank.telephony.transcription.completed", {
        "call_id": call_id, "recording_id": "rec-1", "line": "+12155550199", "kind": "voicemail", "from": sender,
        "text": text, "engine": "deepgram/nova-3", "language": "en-US", "completed_at": "2026-11-04T15:03:00Z",
    }, event_id=event_id)


def telegram(handle: str = "4242", text: str = "ping", *, direction: str = "inbound",
             event_id: str = "0b9c6a62-1111-4d0e-8f5f-000000000006") -> dict:
    return inbound("bloodbank.messaging.telegram.received", {
        "schema_version": 1, "direction": direction, "sender": {"handle": handle, "display_name": "JD"},
        "recipient": {"handle": "operator-bot"}, "content": {"text": text, "format": "plain"},
        "received_at": "2026-11-04T15:04:00Z", "message_id": "tg-1", "thread_id": "chat-4242",
    }, event_id=event_id)
