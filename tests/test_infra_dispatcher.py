import importlib.util
import sys
from pathlib import Path

module_path = Path(__file__).resolve().parents[1] / "event_producers" / "infra_dispatcher.py"
spec = importlib.util.spec_from_file_location("infra_dispatcher", module_path)
assert spec and spec.loader
infra_dispatcher = importlib.util.module_from_spec(spec)
sys.modules["infra_dispatcher"] = infra_dispatcher
spec.loader.exec_module(infra_dispatcher)

_extract_labels = infra_dispatcher._extract_labels
_extract_state_slug = infra_dispatcher._extract_state_slug
_unwrap_plane_body = infra_dispatcher._unwrap_plane_body
evaluate_ready_issue = infra_dispatcher.evaluate_ready_issue
build_dispatch_message = infra_dispatcher.build_dispatch_message


def _plane_body(**overrides):
    issue = {
        "id": "issue-123",
        "identifier": "PERTH-99",
        "name": "Hook up infra routing",
        "updated_at": "2026-02-07T13:00:00Z",
        "state_detail": {"name": "Unstarted"},
        "labels": [{"name": "ready"}, {"name": "comp:bloodbank"}],
        "url": "https://plane.example/issues/PERTH-99",
    }
    issue.update(overrides)
    return {
        "event": "issue",
        "action": "update",
        "workspace_id": "ws-1",
        "data": issue,
    }


def test_unwrap_plane_body_from_bloodbank_payload():
    body = _plane_body()
    payload = {
        "provider": "plane",
        "event": "issue",
        "body": body,
    }
    assert _unwrap_plane_body(payload) == body


def test_extract_state_slug_prefers_state_detail_name():
    issue = {"state_detail": {"name": "Unstarted"}}
    assert _extract_state_slug(issue) == "unstarted"


def test_extract_labels_normalizes_mixed_label_shapes():
    issue = {
        "labels": [
            {"name": "Ready"},
            {"slug": "comp:bloodbank"},
            "automation:go",
        ]
    }
    labels = _extract_labels(issue)
    assert "ready" in labels
    assert "comp-bloodbank" in labels
    assert "automation-go" in labels


def test_evaluate_ready_issue_accepts_ready_unstarted():
    ticket = evaluate_ready_issue(
        _plane_body(),
        ready_states=("unstarted",),
        ready_labels=("ready", "automation-go"),
        component_prefix="comp:",
    )
    assert ticket is not None
    assert ticket["ticket_ref"] == "PERTH-99"
    assert ticket["component"] == "bloodbank"


def test_evaluate_ready_issue_rejects_without_ready_label():
    body = _plane_body(labels=[{"name": "comp:bloodbank"}])
    ticket = evaluate_ready_issue(
        body,
        ready_states=("unstarted",),
        ready_labels=("ready",),
        component_prefix="comp:",
    )
    assert ticket is None


def test_build_dispatch_message_mentions_missing_component_route():
    ticket = {
        "issue_id": "issue-1",
        "ticket_ref": "PERTH-1",
        "title": "Test",
        "state": "unstarted",
        "labels": ["ready"],
        "component": None,
        "url": "",
    }
    message = build_dispatch_message(ticket)
    assert "Component route: UNKNOWN" in message
    assert "comp:<component>" in message
