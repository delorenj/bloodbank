"""Which agents are open in a terminal right now — the true live registry.

`asm:live` is NOT this. It holds only agents that fired a hook inside the TTL
window: measured at 2 scopes against 13 agents actually sitting in zellij panes
and 71 agent processes overall. Anything that selects candidates from asm:live
returns "nobody" for most real tickets and silently falls back to the fleet PM.

`~/.claude/sessions/*.json` is not it either -- that knows only Claude Code,
while the operator's tabs are a mix (measured: 7 claude, 6 codex).

/proc is. Every agent CLI inherits ZELLIJ_SESSION_NAME and ZELLIJ_PANE_ID into
its own environment, so the process itself says which tab it occupies, what its
working directory is, and -- for Hermes -- which profile it runs. One scan,
every CLI, no per-CLI registry to keep in sync.

Stdlib only.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import asm
from .board import board_for

# comm values that mean "an interactive coding agent". Matched exactly against
# /proc/<pid>/comm, which is truncated to 15 bytes by the kernel.
AGENT_COMMS = ("claude", "codex", "hermes", "copilot", "aider",
               "antigravity", "opencode")


def _environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (OSError, PermissionError):
        return {}
    out: dict[str, str] = {}
    for entry in raw.decode("utf-8", "replace").split("\0"):
        key, sep, value = entry.partition("=")
        if sep:
            out[key] = value
    return out


def discover() -> list[dict]:
    """Every live agent process, with its pane, cwd and board.

    Returns one record per PROCESS. A record with no `pane` is headless (the
    Hermes fleet: 23 of 71 measured) -- still a real agent, but not "a terminal
    that is open", so a keystroke courier must skip it while a socket courier
    need not.
    """
    found: list[dict] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return found

    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        stat = asm.proc_stat(pid)
        if stat is None:
            continue
        comm, _ppid, starttime = stat
        if comm not in AGENT_COMMS:
            continue

        env = _environ(pid)
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except (OSError, PermissionError):
            continue

        pane = env.get("ZELLIJ_PANE_ID", "")
        profile = os.path.basename(env.get("HERMES_HOME", "").rstrip("/"))
        if profile in asm.NOT_AN_AGENT_PROFILE:
            continue

        # Mirror the identity the hook itself would have produced, so a record
        # joins to its ASM row without a second convention to keep in sync.
        if comm == "hermes" and profile:
            scope = f"hermes:a:{profile}"
        else:
            scope = f"{comm}:p:{pid}.{starttime}"

        found.append({
            "scope": scope,
            "cli": comm,
            "pid": pid,
            "starttime": starttime,
            "cwd": cwd,
            "pane": pane,
            "session": env.get("ZELLIJ_SESSION_NAME", ""),
            "profile": profile,
            "board": board_for(cwd),
        })
    return found


def for_board(board_id: str, agents: list[dict] | None = None) -> list[dict]:
    """Agents whose working directory resolves to *board_id* exactly.

    Exact comparison on the WALKED-UP board, never a path prefix: four
    registered projects live under 33GOD, so prefix matching would hand every
    submodule agent to the parent board.
    """
    return [
        a for a in (discover() if agents is None else agents)
        if a["board"] and a["board"]["board_id"] == board_id
    ]
