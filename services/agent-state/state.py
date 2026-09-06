"""The projection itself: bus events + observed reality -> per-pane agent state.

Pure functions over plain dicts. No sockets, no Redis, no clock of its own --
every entry point takes `now`. That is deliberate: this module is the part with
the actual design decisions in it, so it must be testable without infrastructure.

WHY EVENTS ALONE ARE NOT ENOUGH
-------------------------------
Agent hooks publish over CORE NATS -- `core/nats_publish.py` says it plainly:
"open one TCP connection, PUB, drain with PING/PONG, close". At-most-once, no
JetStream, no replay. And a publish failure is logged and swallowed (the hook
returns 0 unless BLOODBANK_HOOK_STRICT=1, which is set nowhere), because a hook
must never fail the user's turn.

So events CAN be lost, silently, by design. For a state DISPLAY that is fatal
on its own: miss one `session.ended` and the tab claims "working" forever, with
nothing to correct it. That exact failure has already happened -- tabs stuck
showing a bell that could not be cleared.

Hence edges AND levels:

  events        instant   paint the transition
  reconcile     ~10s      ask what is actually true, correct the drift
  TTL           always    unrefreshed state expires, so staleness is VISIBLE

The property that buys robustness is not the transport. It is that any missed
event self-heals within one reconcile period.
"""

from __future__ import annotations

from typing import Any, Iterable

IDLE = "idle"
WORKING = "working"
ERROR = "error"
ATTENTION = "attention"

# Precedence when more than one signal is live for a pane. Attention outranks
# Error on purpose, matching Deckard's AgentState: a human being asked a
# question is more urgent than a turn that already failed and is now just
# sitting there. Idle is the floor.
RANK = {IDLE: 0, WORKING: 1, ERROR: 2, ATTENTION: 3}

# Bus type -> state. Every type the claude adapter actually emits is here; see
# services/agent-hooks/clients/claude.py `default_map`.
#
# Note `bloodbank.conversation.turn.started` is in the CONVERSATION domain, not
# `agent` -- a subscriber listening only to `bloodbank.evt.agent.>` silently
# misses every prompt submission. Subscribe to both.
EVENT_STATE = {
    "bloodbank.agent.session.started": WORKING,
    "bloodbank.conversation.turn.started": WORKING,
    "bloodbank.agent.tool.requested": WORKING,
    "bloodbank.agent.tool.completed": WORKING,
    # SubagentStop. The PARENT turn is still running, so this is a keep-alive,
    # not an end.
    "bloodbank.agent.invocation.completed": WORKING,
    # Claude's `Stop` hook. Misleadingly named: it is the end of a TURN, not of
    # a session, and it fires many times per session.
    "bloodbank.agent.session.ended": IDLE,
    "bloodbank.agent.invocation.failed": ERROR,
    "deckard.v1.agent.attention": ATTENTION,
}

# A new prompt from the human is a RESET, not a keep-alive. Everything else that
# maps to WORKING is mid-turn traffic and must not clobber a state that is
# waiting on a person -- a tool call firing must not erase the bell that says
# "answer me". But if the human types a new prompt, they have plainly moved on,
# so a stale bell or a dead turn's red should clear.
RESET_EVENTS = frozenset({"bloodbank.conversation.turn.started"})

# Processes that mean "an agent is alive in this pane". Matched against the
# process NAME, never a substring of the whole command line: `codex mcp-server`
# and a hermes daemon's python both contain an agent's name in their path
# without being an interactive agent, and that class of false positive has
# already corrupted state on this machine once.
AGENT_COMMS = frozenset(
    {"claude", "codex", "kimi", "kimi-co", "agy", "gemini", "copilot", "opencode", "hermes"}
)


def pane_key(session: str, pane: int) -> str:
    return f"{session}\t{pane}"


def origin_of(envelope: dict[str, Any]) -> tuple[str, int] | None:
    """The (session, pane) an envelope came from, or None if unattributable.

    Only events stamped by `zellij_origin()` in the agent-hooks publisher can be
    placed on a tab. Anything else -- a producer with no terminal, an older
    event -- is skipped rather than guessed at: attributing by working_directory
    alone cannot separate two tabs sitting in the same repo, which is the normal
    case here.
    """
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    pane = data.get("zellij_pane_id")
    session = data.get("zellij_session_name")
    if not isinstance(pane, int) or isinstance(pane, bool):
        return None
    if not isinstance(session, str) or not session:
        return None
    if pane < 0 or pane > 0xFFFF_FFFF:
        return None
    return session, pane


def apply_event(
    states: dict[str, dict[str, Any]], envelope: dict[str, Any], now: float
) -> str | None:
    """Fold one envelope in. Returns the key that changed, or None.

    Mutates `states` in place; returns None when the envelope is unattributable,
    of an unknown type, or would not change anything.
    """
    ce_type = envelope.get("type")
    if not isinstance(ce_type, str):
        return None
    target = EVENT_STATE.get(ce_type)
    if target is None:
        return None
    origin = origin_of(envelope)
    if origin is None:
        return None
    session, pane = origin
    key = pane_key(session, pane)

    current = states.get(key)
    current_state = current["state"] if current else IDLE

    if target == WORKING and ce_type not in RESET_EVENTS:
        # Mid-turn keep-alive: refresh liveness, never downgrade a state that is
        # waiting on the human.
        if RANK[current_state] > RANK[WORKING]:
            if current is not None:
                current["seen"] = now
            return None

    if current is not None and current["state"] == target:
        current["seen"] = now
        return None

    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    actor = envelope.get("actor") if isinstance(envelope.get("actor"), dict) else {}
    states[key] = {
        "session": session,
        "pane": pane,
        "state": target,
        "since": now,
        "seen": now,
        "agent": actor.get("cli") or actor.get("client") or "",
        "cwd": data.get("working_directory") or "",
        "source": ce_type,
    }
    return key


def agent_panes(ps_rows: Iterable[tuple[int, str]], environ_of) -> dict[tuple[str, int], str]:
    """Which (session, pane) have a live agent, mapped to WHICH agent.

    A dict rather than a set so promotion can name the agent it found; `in`
    still means the same thing at every call site.

    `ps_rows` is (pid, comm); `environ_of(pid)` returns that process's environ as
    a dict. Filtering on comm FIRST keeps this cheap -- environ is only read for
    the handful of processes that could possibly be an agent.
    """
    live: dict[tuple[str, int], str] = {}
    for pid, comm in ps_rows:
        if comm not in AGENT_COMMS:
            continue
        env = environ_of(pid)
        if not env:
            continue
        session = env.get("ZELLIJ_SESSION_NAME")
        pane = env.get("ZELLIJ_PANE_ID")
        if not session or not pane:
            continue
        try:
            live[(session, int(pane))] = comm
        except (TypeError, ValueError):
            continue
    return live


def focused_panes(
    pane_to_tab: dict[tuple[str, int], int], active_tab: dict[str, int]
) -> set[tuple[str, int]]:
    """Panes belonging to the tab the user is currently looking at."""
    out = set()
    for ident, tab in pane_to_tab.items():
        if active_tab.get(ident[0]) == tab:
            out.add(ident)
    return out


def clear_on_focus(
    states: dict[str, dict[str, Any]], focused: set[tuple[str, int]], now: float
) -> list[str]:
    """Arriving at a tab acknowledges its bell. Returns changed keys.

    `attention` means "a human is being asked something", so looking at it IS
    the answer. `error` deliberately does NOT clear this way -- an unresolved
    failure should survive a glance and a move-on -- and `working` obviously
    must not, since it is still working.
    """
    changed = []
    for key, entry in states.items():
        if entry["state"] != ATTENTION:
            continue
        if (entry["session"], entry["pane"]) not in focused:
            continue
        entry["state"] = IDLE
        entry["since"] = now
        entry["seen"] = now
        entry["source"] = "focus:acknowledged"
        changed.append(key)
    return changed


def reconcile(
    states: dict[str, dict[str, Any]],
    live_panes: set[tuple[str, int]] | None,
    live_agents: dict[tuple[str, int], str] | set[tuple[str, int]] | None,
    now: float,
    *,
    working_grace: float = 20.0,
) -> list[str]:
    """Correct the projection against observed reality. Returns changed keys.

    THE RULE: on conflict, observation wins. An event-derived state must never
    outlive its evidence -- that is the whole reason this layer exists.

    - a pane that no longer exists is dropped
    - `working` with no live agent process decays to idle, which is how a missed
      `session.ended` heals
    - `attention` and `error` are NOT decayed by process absence. They are
      addressed to a human and outlive the process that raised them; they end by
      being acknowledged (a new turn) or by TTL.

    `live_panes`/`live_agents` may be None when that observation could not be
    made this cycle -- a wedged zellij, a failed ps. In that case nothing is
    decayed, because "I could not look" must never be mistaken for "it is gone".
    """
    changed: list[str] = []
    for key, entry in list(states.items()):
        ident = (entry["session"], entry["pane"])

        if live_panes is not None and ident not in live_panes:
            del states[key]
            changed.append(key)
            continue

        if entry["state"] != WORKING or live_agents is None:
            continue
        # Grace: a turn can legitimately have no agent process for a moment
        # between a hook firing and the process appearing.
        if now - entry["seen"] < working_grace:
            continue
        if ident not in live_agents:
            entry["state"] = IDLE
            entry["since"] = now
            entry["seen"] = now
            entry["source"] = "reconcile:no-agent-process"
            changed.append(key)

    # PROMOTION. Observation creates state, it does not only correct it.
    #
    # Without this the projector can only ever describe panes that happened to
    # publish while it was listening: an agent already running when the service
    # started, or one whose CLI has no bloodbank hooks at all, stays invisible
    # forever.
    #
    # Seeded as IDLE, never WORKING. A live process is evidence of PRESENCE, not
    # of ACTIVITY -- an agent sitting at a prompt waiting for you has a process
    # too. Marking those `working` lit every tab green at once, which is worse
    # than useless: a signal that is always on carries no information. Events
    # are what raise a pane to `working`, and they arrive within seconds
    # (every tool call publishes), so nothing is lost by waiting for one.
    if live_agents is not None:
        for ident in live_agents:
            if live_panes is not None and ident not in live_panes:
                continue
            key = pane_key(*ident)
            if key in states:
                continue
            states[key] = {
                "session": ident[0],
                "pane": ident[1],
                "state": IDLE,
                "since": now,
                "seen": now,
                "agent": live_agents.get(ident, "") if isinstance(live_agents, dict) else "",
                "cwd": "",
                "source": "reconcile:agent-process-seen",
            }
            changed.append(key)
    return changed


def public_view(entry: dict[str, Any]) -> dict[str, Any]:
    """What consumers read. Deliberately small and stable."""
    return {
        "session": entry["session"],
        "pane": entry["pane"],
        "state": entry["state"],
        "since": round(entry["since"], 3),
        "agent": entry.get("agent", ""),
        "cwd": entry.get("cwd", ""),
        "source": entry.get("source", ""),
    }
