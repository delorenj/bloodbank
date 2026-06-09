"""Load a publisher's hook-arg → (ce_type, ordering_bucket) map from the SSOT.

The authoritative map is GENERATED from ``hooks.master.json`` by ``sync.py``
into ``<service_dir>/<agent>/event_map.generated.json``. Publishers call
:func:`resolve_map` to source their mapping from that projection, MERGED over
an embedded ``_DEFAULT_MAP`` fallback.

Why merge (not replace):
  * The generated file holds the canonical per-agent bindings — it wins for any
    arg it defines, so the SSOT is authoritative for live behavior.
  * The embedded ``_DEFAULT_MAP`` may carry extra migration aliases (e.g. codex
    ``session-start`` / ``notify``) that aren't canonical native hook names;
    merging keeps them working.
  * If the generated file is missing/corrupt, the publisher still runs on its
    embedded default — a hook invoked as a standalone script never silently
    breaks.

Stdlib-only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

Pair = tuple[str, str]


def load_generated(agent_dir: Path) -> dict[str, Pair] | None:
    """Return the generated arg→(type,bucket) map, or None if unavailable."""
    path = Path(agent_dir) / "event_map.generated.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    table = raw.get("map") if isinstance(raw, dict) else None
    if not isinstance(table, dict):
        return None
    out: dict[str, Pair] = {}
    for key, val in table.items():
        if isinstance(val, (list, tuple)) and len(val) >= 2:
            out[str(key)] = (str(val[0]), str(val[1]))
    return out or None


def resolve_map(agent_dir: Path, default_map: Mapping[str, Pair]) -> dict[str, Pair]:
    """Embedded default merged-under the generated SSOT projection."""
    merged: dict[str, Pair] = {k: tuple(v) for k, v in default_map.items()}  # type: ignore[misc]
    generated = load_generated(agent_dir)
    if generated:
        merged.update(generated)
    return merged
