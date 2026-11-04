"""One JSON line per log record on stderr (journald keeps it). Never message text or credentials."""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime


def log(event: str, **fields) -> None:
    record = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **fields}
    print(json.dumps(record, default=str, sort_keys=True), file=sys.stderr, flush=True)
