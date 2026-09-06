#!/usr/bin/env python3
"""tabpaint — render the agent-state projection onto the zellij tab bar.

The reference consumer of `agent-state`, and deliberately DUMB: it owns no state
machine of its own. It reads Redis, aggregates panes up to tabs, and renames.
Every decision about what a state MEANS lives in the projector.

That is the whole point of the split. The previous tab painter ran its own
hook-driven state machine, which meant two sources of truth -- and it was wired
only into ~/.claude/settings.json, so a codex pane could never light up at all.
This one is agent-agnostic because the projector is.

  agentstate:pane:<session>:<pane>  ──► aggregate per tab ──► rename-tab-by-id

AGGREGATION
A tab holds several panes, so the tab shows the WORST of them by precedence
(attention > error > working > idle). One agent asking a question in a two-pane
tab must not be hidden by its neighbour merely working.

RENDERING
  🟢 working    blinks -- motion means busy
  🔔 attention  steady -- stillness means waiting on you
  🔴 error      steady
     idle       no marker

Every glyph is East_Asian_Width=W, exactly two columns, and the blink SWAPS
🟢 for ⚫ rather than removing it -- so the tab never changes width and the bar
never reflows. `⚠️` and `▫️` are a narrow base plus VS16 and jitter between 1
and 2 columns; do not use them. Check with:
    python3 -c "import unicodedata;print(unicodedata.east_asian_width('X'))"

STATELESS BY DESIGN
The unmarked tab name is recovered by stripping known markers off the current
name, not remembered in a file. So a tab the user renames is adopted
immediately, and a crash cannot strand a stale "original" that later overwrites
a good name.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from resp import Redis, RespError  # noqa: E402
from state import ATTENTION, ERROR, IDLE, RANK, WORKING  # noqa: E402

MARK = {WORKING: "🟢", ATTENTION: "🔔", ERROR: "🔴"}
BLINKS = {WORKING}
ALT = os.environ.get("TABPAINT_BLINK_ALT", "⚫")

# Every glyph this painter can leave on a name, plus legacy ones from the
# hook-era painter, so old markers are cleaned up rather than accumulating.
_MARKERS = "".join(["🟢", "🔔", "🔴", "🟠", "✅", "⚫", "🔕", "⚠️", "⚠", "️"])
_STRIP = re.compile(rf"^[{re.escape(_MARKERS)}\s]+|[{re.escape(_MARKERS)}\s]+$")

log = logging.getLogger("tabpaint")


def strip_markers(name: str) -> str:
    return _STRIP.sub("", name)


class Painter:
    def __init__(self) -> None:
        self.session = os.environ.get("ZELLIJ_SESSION_NAME") or os.environ.get(
            "TABPAINT_SESSION", "Workspace"
        )
        self.prefix = os.environ.get("AGENT_STATE_PREFIX", "agentstate")
        self.zellij = os.environ.get("ZELLIJ_BIN", "zellij")
        self.interval = float(os.environ.get("TABPAINT_INTERVAL", "0.5"))
        self.topology_every = int(os.environ.get("TABPAINT_TOPOLOGY_EVERY", "10"))
        self.redis = Redis(
            os.environ.get("AGENT_STATE_REDIS_HOST", "127.0.0.1"),
            int(os.environ.get("AGENT_STATE_REDIS_PORT", "6379")),
        )
        self.rendered: dict[int, str] = {}
        self.running = True

    def _zellij(self, *args: str) -> str | None:
        try:
            res = subprocess.run(
                [self.zellij, "--session", self.session, "action", *args],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "ZELLIJ": "", "ZELLIJ_SESSION_NAME": ""},
            )
            return res.stdout if res.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    def topology(self) -> tuple[dict[int, str], dict[int, list[int]]] | None:
        """(tab id -> current name, tab id -> its pane ids), or None if unknown.

        `list-panes --tab` is ~104ms; `--json` is 4.7s because it resolves every
        pane's command out of ps. Never use --json here.
        """
        out = self._zellij("list-panes", "--tab")
        if out is None:
            return None
        names: dict[int, str] = {}
        panes: dict[int, list[int]] = {}
        for line in out.splitlines()[1:]:
            cols = line.split("  ")
            if len(cols) < 4:
                continue
            try:
                tab = int(cols[0].strip())
            except ValueError:
                continue
            pane_col = cols[3].strip()
            names.setdefault(tab, cols[2])
            if pane_col.startswith("terminal_"):
                try:
                    panes.setdefault(tab, []).append(int(pane_col[len("terminal_"):]))
                except ValueError:
                    continue
        return names, panes

    def projection(self) -> dict[int, str] | None:
        """pane id -> state, straight from the projector. None if Redis is down.

        A MISSING key means unknown, never idle -- the TTL exists so a dead
        projector cannot assert anything. Unknown panes are simply not painted,
        which leaves the tab as the user last saw it rather than blanking it.
        """
        try:
            keys = self.redis.scan(f"{self.prefix}:pane:{self.session}:*")
            out: dict[int, str] = {}
            for key in keys:
                raw = self.redis.get(key)
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                    out[int(entry["pane"])] = str(entry["state"])
                except (ValueError, KeyError, TypeError):
                    continue
            return out
        except (RespError, OSError) as exc:
            log.warning("redis read failed: %s", exc)
            return None

    def run(self) -> int:
        log.info(
            "tabpaint up: session=%s prefix=%s interval=%.2fs",
            self.session, self.prefix, self.interval,
        )
        topo: tuple[dict[int, str], dict[int, list[int]]] | None = None
        phase = 0
        tick = 0
        while self.running:
            if tick % self.topology_every == 0 or topo is None:
                fresh = self.topology()
                if fresh is not None:
                    topo = fresh
            states = self.projection()

            if topo is not None and states is not None:
                names, panes = topo
                for tab, pane_ids in panes.items():
                    worst = IDLE
                    for pane in pane_ids:
                        s = states.get(pane)
                        if s and RANK.get(s, 0) > RANK[worst]:
                            worst = s
                    base = strip_markers(names.get(tab, ""))
                    if not base:
                        continue
                    if worst == IDLE:
                        want = base
                    elif worst in BLINKS and phase % 2 == 1:
                        want = f"{base} {ALT}"
                    else:
                        want = f"{base} {MARK.get(worst, '')}".rstrip()

                    # Only talk to zellij when the rendered name actually
                    # changes. Steady states cost one rename, ever.
                    if self.rendered.get(tab) != want:
                        if self._zellij("rename-tab-by-id", str(tab), want) is not None:
                            self.rendered[tab] = want
                            names[tab] = want

            phase += 1
            tick += 1
            time.sleep(self.interval)

        # Leave the bar clean rather than frozen mid-blink.
        if topo is not None:
            for tab, name in topo[0].items():
                base = strip_markers(name)
                if base and base != name:
                    self._zellij("rename-tab-by-id", str(tab), base)
        log.info("tabpaint stopped")
        return 0


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    painter = Painter()

    def stop(_s, _f):
        painter.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return painter.run()


if __name__ == "__main__":
    raise SystemExit(main())
