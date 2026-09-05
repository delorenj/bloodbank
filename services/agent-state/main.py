#!/usr/bin/env python3
"""agent-state — per-pane agent state, projected from the bus and from reality.

Folds Bloodbank agent lifecycle events into one state per zellij pane, corrects
that projection against observed reality on a slow timer, and publishes the
result to Redis for any consumer (the zellij tab painter, Deckard, the Nanoleaf
wall) to read.

  bloodbank.evt.agent.>        ─┐
  bloodbank.evt.conversation.> ─┼─► fold ──┐
  deckard.evt.attention        ─┘          ├─► Redis ──► consumers
  ps + zellij list-panes ──► reconcile ────┘

WHY BOTH LEGS
-------------
Events give latency; they do not give correctness. Hooks publish over CORE
NATS -- at-most-once, no replay -- and a failed publish is deliberately
swallowed so a hook can never fail the user's turn. So events WILL be lost, and
for a state display a lost event is permanent corruption: miss one
`session.ended` and a tab claims "working" until a human intervenes.

Reconciliation is what makes that self-healing: any missed event is corrected
within one cycle. TTL is what makes a dead projector honest -- keys expire, so
consumers see "unknown" instead of a confident lie.

WHY REDIS
---------
Not for pub/sub. NATS already does that better. Redis holds the LEVEL: a
consumer that starts fresh -- Deckard restarting, a tab painter launching -- can
ask "what is true right now?" and get an answer immediately, instead of waiting
for the next event, which might be minutes away. A bus cannot answer that.

Configuration (all optional):
  BLOODBANK_NATS_HOST / _PORT      default 127.0.0.1 / 4222
  AGENT_STATE_REDIS_HOST / _PORT   default 127.0.0.1 / 6379
  AGENT_STATE_PREFIX               default "agentstate"
  AGENT_STATE_RECONCILE_SECS       default 10
  AGENT_STATE_TTL_SECS             default 45   (must exceed reconcile)
  ZELLIJ_BIN                       default "zellij"
  LOG_LEVEL                        INFO / DEBUG

Stdlib only, so this runs on the host with no virtualenv. It must run on the
host: reconciliation reads the process table and the zellij CLI.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import natssub  # noqa: E402
import state as st  # noqa: E402
from resp import Redis, RespError  # noqa: E402

SUBJECTS = (
    "bloodbank.evt.agent.>",
    # NOT redundant with the line above: prompt submission is published as
    # `bloodbank.conversation.turn.started` -- the CONVERSATION domain. A
    # subscriber listening only to `agent.>` silently misses every prompt, and
    # therefore never sees a turn begin.
    "bloodbank.evt.conversation.>",
    "deckard.evt.attention",
)

log = logging.getLogger("agent-state")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Projector:
    def __init__(self) -> None:
        self.prefix = os.environ.get("AGENT_STATE_PREFIX", "agentstate")
        self.reconcile_secs = _env_int("AGENT_STATE_RECONCILE_SECS", 10)
        self.ttl = _env_int("AGENT_STATE_TTL_SECS", 45)
        if self.ttl <= self.reconcile_secs:
            # A TTL shorter than the refresh cycle expires state that is still
            # true, which looks exactly like the staleness it exists to prevent.
            self.ttl = self.reconcile_secs * 3
        self.zellij = os.environ.get("ZELLIJ_BIN", "zellij")
        self.redis = Redis(
            os.environ.get("AGENT_STATE_REDIS_HOST", "127.0.0.1"),
            _env_int("AGENT_STATE_REDIS_PORT", 6379),
        )
        self.states: dict[str, dict] = {}
        self.running = True

    # -- redis surface --------------------------------------------------------
    def key_for(self, entry: dict) -> str:
        return f"{self.prefix}:pane:{entry['session']}:{entry['pane']}"

    def write(self, key: str) -> None:
        entry = self.states.get(key)
        try:
            if entry is None:
                return
            view = st.public_view(entry)
            body = json.dumps(view, separators=(",", ":"))
            self.redis.set_ex(self.key_for(entry), body, self.ttl)
            self.redis.publish(f"{self.prefix}:changes", body)
        except (RespError, OSError) as exc:
            log.warning("redis write failed: %s", exc)

    def forget(self, session: str, pane: int) -> None:
        try:
            self.redis.delete(f"{self.prefix}:pane:{session}:{pane}")
            self.redis.publish(
                f"{self.prefix}:changes",
                json.dumps({"session": session, "pane": pane, "state": st.IDLE, "gone": True},
                           separators=(",", ":")),
            )
        except (RespError, OSError) as exc:
            log.warning("redis delete failed: %s", exc)

    def refresh_all(self) -> None:
        """Re-SET every live key so TTL never expires state that is still true."""
        for key in list(self.states):
            self.write(key)

    # -- observation ----------------------------------------------------------
    def live_panes(self, sessions: set[str]) -> set[tuple[str, int]] | None:
        """Panes that currently exist, or None if we could not look.

        None is load-bearing: `reconcile` decays nothing when observation
        failed, so a wedged zellij cannot blank every tab.
        """
        if not sessions:
            return set()
        found: set[tuple[str, int]] = set()
        looked = False
        for session in sessions:
            try:
                out = subprocess.run(
                    [self.zellij, "--session", session, "action", "list-panes", "--tab"],
                    capture_output=True, text=True, timeout=5,
                    env={**os.environ, "ZELLIJ": "", "ZELLIJ_SESSION_NAME": ""},
                ).stdout
            except (OSError, subprocess.SubprocessError) as exc:
                log.debug("list-panes failed for %s: %s", session, exc)
                continue
            looked = True
            for line in out.splitlines()[1:]:
                cols = line.split("  ")
                if len(cols) < 4:
                    continue
                pane = cols[3].strip()
                if pane.startswith("terminal_"):
                    try:
                        found.add((session, int(pane[len("terminal_"):])))
                    except ValueError:
                        continue
        return found if looked else None

    def live_agents(self) -> set[tuple[str, int]] | None:
        try:
            out = subprocess.run(
                ["ps", "-eo", "pid=,comm="], capture_output=True, text=True, timeout=5
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("ps failed: %s", exc)
            return None
        rows = []
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                rows.append((int(parts[0]), parts[1].strip()))
            except ValueError:
                continue
        return st.agent_panes(rows, _environ_of)

    def reconcile(self) -> None:
        sessions = {e["session"] for e in self.states.values()}
        panes = self.live_panes(sessions)
        agents = self.live_agents()
        gone = {k: (self.states[k]["session"], self.states[k]["pane"]) for k in self.states}
        changed = st.reconcile(self.states, panes, agents, time.time())
        for key in changed:
            if key in self.states:
                self.write(key)
            else:
                session, pane = gone[key]
                self.forget(session, pane)
        self.refresh_all()
        if changed:
            log.info("reconciled: %d correction(s)", len(changed))

    # -- loop -----------------------------------------------------------------
    def run(self) -> int:
        host = os.environ.get("BLOODBANK_NATS_HOST", "127.0.0.1")
        port = _env_int("BLOODBANK_NATS_PORT", 4222)
        log.info(
            "agent-state up: nats=%s:%d redis=%s prefix=%s reconcile=%ds ttl=%ds",
            host, port, f"{self.redis.host}:{self.redis.port}",
            self.prefix, self.reconcile_secs, self.ttl,
        )
        next_reconcile = 0.0
        for item in natssub.stream(
            host, port, SUBJECTS,
            on_reconnect=lambda: log.info("subscribed: %s", ", ".join(SUBJECTS)),
        ):
            if not self.running:
                break
            if item is not None:
                _subject, payload = item
                try:
                    envelope = json.loads(payload)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(envelope, dict):
                    key = st.apply_event(self.states, envelope, time.time())
                    if key:
                        self.write(key)
                        log.debug("%s -> %s", key, self.states[key]["state"])

            now = time.monotonic()
            if now >= next_reconcile:
                next_reconcile = now + self.reconcile_secs
                self.reconcile()

        # Clean shutdown: drop our keys so consumers learn immediately rather
        # than waiting out the TTL. A crash still expires them; that is the TTL's
        # job, not this one's.
        for entry in list(self.states.values()):
            self.forget(entry["session"], entry["pane"])
        log.info("agent-state stopped")
        return 0


def _environ_of(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        name, _, value = chunk.partition(b"=")
        out[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    projector = Projector()

    def stop(_signum, _frame):
        projector.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return projector.run()


if __name__ == "__main__":
    raise SystemExit(main())
