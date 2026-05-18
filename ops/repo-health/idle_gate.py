#!/usr/bin/env python3
"""Decide whether Hermes pilot loop should run full repo-health evidence capture.

Reads a repo-health JSON snapshot and recent evidence artifacts, then emits a
small JSON decision payload suitable for cron orchestration.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


TS_FMT = "%Y%m%dT%H%M%SZ"


@dataclass
class Decision:
    idle_state: bool
    should_capture_full: bool
    reason: str
    interval_minutes: int
    minutes_since_last_artifact: float | None
    latest_artifact: str | None


def _parse_ts_from_name(path: Path) -> datetime | None:
    stem = path.stem  # repo-health-<timestamp>
    if not stem.startswith("repo-health-"):
        return None
    raw = stem.removeprefix("repo-health-")
    try:
        return datetime.strptime(raw, TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _latest_artifact(evidence_dir: Path) -> tuple[Path | None, datetime | None]:
    if not evidence_dir.exists():
        return None, None
    latest_path: Path | None = None
    latest_ts: datetime | None = None
    for p in sorted(evidence_dir.glob("repo-health-*.json")):
        ts = _parse_ts_from_name(p)
        if ts is None:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_path = p
            latest_ts = ts
    return latest_path, latest_ts


def _is_idle(snapshot: dict[str, object]) -> bool:
    git_status = str(snapshot.get("git_status") or "")
    worktree_dirty = bool(snapshot.get("worktree_dirty"))
    issues = snapshot.get("issues_open") or []
    prs = snapshot.get("prs_open") or []

    synced_main = "...origin/main" in git_status
    if synced_main and "[ahead" not in git_status and "[behind" not in git_status:
        pass
    else:
        return False

    return (not worktree_dirty) and (len(issues) == 0) and (len(prs) == 0)


def decide(
    snapshot: dict[str, object],
    evidence_dir: Path,
    interval_minutes: int,
    now: datetime,
) -> Decision:
    idle = _is_idle(snapshot)
    latest_path, latest_ts = _latest_artifact(evidence_dir)

    if not idle:
        return Decision(
            idle_state=False,
            should_capture_full=True,
            reason="non-idle-state",
            interval_minutes=interval_minutes,
            minutes_since_last_artifact=None,
            latest_artifact=str(latest_path) if latest_path else None,
        )

    if latest_ts is None:
        return Decision(
            idle_state=True,
            should_capture_full=True,
            reason="idle-no-prior-artifact",
            interval_minutes=interval_minutes,
            minutes_since_last_artifact=None,
            latest_artifact=None,
        )

    age_min = (now - latest_ts).total_seconds() / 60.0
    if age_min >= interval_minutes:
        return Decision(
            idle_state=True,
            should_capture_full=True,
            reason="idle-interval-elapsed",
            interval_minutes=interval_minutes,
            minutes_since_last_artifact=round(age_min, 2),
            latest_artifact=str(latest_path),
        )

    return Decision(
        idle_state=True,
        should_capture_full=False,
        reason="idle-throttled",
        interval_minutes=interval_minutes,
        minutes_since_last_artifact=round(age_min, 2),
        latest_artifact=str(latest_path),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Decide repo-health idle throttle")
    parser.add_argument("--snapshot", required=True, help="Path to repo-health JSON snapshot")
    parser.add_argument(
        "--evidence-dir",
        default="_bmad_output/evidence",
        help="Evidence directory (default: _bmad_output/evidence)",
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=60,
        help="Minimum minutes between full captures while idle (default: 60)",
    )
    parser.add_argument(
        "--now-utc",
        help="Override now timestamp in UTC format YYYYMMDDTHHMMSSZ for deterministic tests",
    )
    args = parser.parse_args()

    if args.interval_minutes < 1:
        raise SystemExit("--interval-minutes must be >= 1")

    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    now = (
        datetime.strptime(args.now_utc, TS_FMT).replace(tzinfo=timezone.utc)
        if args.now_utc
        else datetime.now(tz=timezone.utc)
    )

    decision = decide(
        snapshot=snapshot,
        evidence_dir=Path(args.evidence_dir),
        interval_minutes=args.interval_minutes,
        now=now,
    )

    print(
        json.dumps(
            {
                "idle_state": decision.idle_state,
                "should_capture_full": decision.should_capture_full,
                "reason": decision.reason,
                "interval_minutes": decision.interval_minutes,
                "minutes_since_last_artifact": decision.minutes_since_last_artifact,
                "latest_artifact": decision.latest_artifact,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
