"""Ephemeral-context detection for hook publishers.

An event is ephemeral when its origin context is expected to die: currently
that means the publisher ran inside a git worktree (fleet policy puts them at
``repo/.worktrees/<branch>`` and deletes them on merge). The envelope
``ephemeral`` extension carries the session's identifying data so consumers
(Candystore, Holocene) can keep breadcrumbs after the worktree — and the CLI
session history keyed to its path — is gone.

Detection asks git itself: in a worktree ``--git-dir`` differs from
``--git-common-dir``. That is independent of any path convention, so a
worktree created anywhere (sibling dir, central hub) is still marked.

Fail-open like everything else on the hook path: the git helper swallows
errors and any non-worktree answer degrades to "no extension", never to a
dropped event.
"""
from __future__ import annotations

import os
from typing import Any

from .session import _git


def worktree_context(cwd: str | None = None) -> dict[str, Any] | None:
    """Worktree details when *cwd* sits inside a git worktree, else None."""
    git_dir = _git("rev-parse", "--path-format=absolute", "--git-dir", cwd=cwd)
    common_dir = _git(
        "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd
    )
    if not git_dir or not common_dir or git_dir == common_dir:
        return None
    main_checkout = os.path.dirname(common_dir)
    toplevel = _git("rev-parse", "--show-toplevel", cwd=cwd)
    if not toplevel and cwd:
        toplevel = os.path.abspath(cwd)
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd) or "unknown"
    return {
        "path": toplevel,
        "branch": branch,
        "repo": os.path.basename(main_checkout),
        "main_checkout": main_checkout,
    }


def ephemeral_context(
    *,
    cwd: str,
    harness: str,
    turn_number: int,
    payload: Any,
) -> dict[str, Any] | None:
    """The envelope ``ephemeral`` extension for this publish, or None.

    Harness-native session ids and transcript paths are lifted out of the raw
    hook payload here — this is the only place they survive downstream, since
    adapters deliberately substitute the publisher-internal session id for
    correlation.
    """
    worktree = worktree_context(cwd)
    if worktree is None:
        return None
    session: dict[str, Any] = {"harness": harness, "turn_number": turn_number}
    if isinstance(payload, dict):
        harness_id = payload.get("session_id") or payload.get("thread_id")
        if harness_id:
            session["harness_session_id"] = harness_id
        transcript = payload.get("transcript_path")
        if transcript:
            session["transcript_path"] = transcript
    return {"worktree": worktree, "session": session}
