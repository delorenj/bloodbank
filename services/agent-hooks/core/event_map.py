"""Load publisher and Deckard-alert hook maps generated from the SSOT.

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


def _load_table(agent_dir: Path, key: str) -> dict | None:
    path = Path(agent_dir) / "event_map.generated.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    table = raw.get(key) if isinstance(raw, dict) else None
    return table if isinstance(table, dict) else None


def load_generated(agent_dir: Path) -> dict[str, Pair] | None:
    """Return the generated arg→(type,bucket) map, or None if unavailable."""
    table = _load_table(agent_dir, "map")
    if table is None:
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


def resolve_alert_kind_map(agent_dir: Path) -> dict[str, str]:
    """Return the generated hook-arg -> attention KIND map ("bell" | "gate").

    A sibling of the alert map, not a refinement of it: `alerts` still says
    WHETHER something wants attention (and Deckard's fanout keys on that literal
    value), while this says WHAT KIND -- whether arriving at the pane answers it.

    A bell is answered by being seen. A gate is not: an agent sitting on a
    permission prompt stays blocked until a key is pressed, so a surface must
    never clear it.

    Defaults to bell at the call site when an entry is missing, deliberately.
    Wrongly acknowledging a bell costs a repaint; wrongly holding a gate open
    would freeze an agent in `awaiting_human` forever.
    """
    table = _load_table(agent_dir, "alert_kinds")
    if table is None:
        return {}
    return {
        str(arg): str(kind)
        for arg, kind in table.items()
        if isinstance(kind, str) and kind in ("bell", "gate")
    }


def resolve_alert_map(agent_dir: Path) -> dict[str, str]:
    """Return the generated hook-arg→normalized-alert-kind map.

    Alerts intentionally have no embedded fallback: unlike Bloodbank lifecycle
    aliases, they must be registry-declared before they can move Deckard's
    display. A missing or corrupt projection therefore fails open by publishing
    nothing instead of broadening the alert surface.
    """
    table = _load_table(agent_dir, "alerts")
    if table is None:
        return {}
    return {
        str(arg): str(kind)
        for arg, kind in table.items()
        if isinstance(kind, str) and kind
    }
