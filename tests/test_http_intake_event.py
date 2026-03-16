from event_producers.http import _extract_plane_intake_issue_created


def _sample_issue_body(action: str = "create", project_id: str = "f7572d0c-ae54-4d25-ae6c-d0d0fa1cc11f"):
    return {
        "event": "issue",
        "action": action,
        "workspace_id": "ws-123",
        "data": {
            "id": "issue-1",
            "identifier": "INBOX",
            "sequence_id": 12,
            "name": "Triage me",
            "url": "https://plane.example/issues/issue-1",
            "created_at": "2026-03-11T03:22:00Z",
            "state_detail": {"name": "Backlog"},
            "project": project_id,
        },
    }


def test_extract_intake_issue_created_matches_target_project():
    body = _sample_issue_body()
    result = _extract_plane_intake_issue_created(body, "f7572d0c-ae54-4d25-ae6c-d0d0fa1cc11f")

    assert result is not None
    assert result["issue_id"] == "issue-1"
    assert result["issue_ref"] == "INBOX"
    assert result["project_id"] == "f7572d0c-ae54-4d25-ae6c-d0d0fa1cc11f"


def test_extract_intake_issue_created_rejects_non_create():
    body = _sample_issue_body(action="update")
    result = _extract_plane_intake_issue_created(body, "f7572d0c-ae54-4d25-ae6c-d0d0fa1cc11f")
    assert result is None


def test_extract_intake_issue_created_rejects_wrong_project():
    body = _sample_issue_body(project_id="other-project")
    result = _extract_plane_intake_issue_created(body, "f7572d0c-ae54-4d25-ae6c-d0d0fa1cc11f")
    assert result is None
