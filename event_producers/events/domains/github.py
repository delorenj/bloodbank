"""
GitHub event payload definitions for Bloodbank event bus.

All events are wrapped in EventEnvelope[T] where T is your payload type.
"""

from pydantic import BaseModel, Field
from typing import Literal


# ============================================================================
# GitHub Events
# ============================================================================


class GitHubPRCreatedPayload(BaseModel):
    """
    Published when a GitHub pull request is created.

    Published when: GitHub webhook fires for PR creation, or n8n workflow detects new PR
    Consumed by: Notification service, project tracking, CI/CD automation
    Routing Key: github.pr.created

    This event follows the standard cache-based pattern where the PR details
    are stored in a cache (Redis, etc.) and referenced via cache_key.

    Generate deterministic event_id using:
        tracker.generate_event_id(
            "github.pr.created",
            unique_key=f"{repo_owner}|{repo_name}|{pr_number}"
        )
    """

    cache_key: str = Field(
        ..., description="Key to retrieve PR data from cache (e.g., 'trinote|423')"
    )
    cache_type: Literal["redis", "memory", "file"] = Field(
        default="redis", description="Type of cache storage used"
    )


ROUTING_KEYS = {
    "GitHubPRCreatedPayload": "github.pr.created",
}
