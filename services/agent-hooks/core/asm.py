"""Agent State Machine -- the hook-side proposer.

Every agent CLI already re-triggers into one shared lifecycle vocabulary and
publishes a normalized CloudEvent per role. What nothing did was FOLD that
stream into "what is each agent doing right now". This module does, from
inside the hook process, and `core/asm.lua` arbitrates.

WHY IN THE HOOK AND NOT A BUS CONSUMER. Three reasons, in order of weight:

  1. Two of the most valuable signals never reach the bus at all.
     `_fanout_alert` (publisher.py) returns early and publishes Notification /
     PermissionRequest / TeammateIdle to `deckard.evt.attention`, a core-NATS
     subject captured by NO JetStream stream. A durable consumer is
     structurally blind to `awaiting_human`.
  2. Only a host-local process can key on the agent PROCESS, and only a
     process key is a liveness oracle -- `/proc/<pid>` existing IS the agent
     being alive, which turns `gone` from a TTL guess into an observation.
  3. It is free. Measured on this box: redis connect+EVAL+close is p50
     0.023 ms against a 3000 ms hook ceiling, and against the 0.171 ms NATS
     publish already sitting on this same code path.

FAIL-OPEN IS ABSOLUTE. This runs on every hook of every CLI. Nothing here may
raise, block, or slow an agent: one 250 ms socket deadline, every entry point
wrapped, and a total no-op when Redis is absent.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path

from .resp import Connection

# --- tunables --------------------------------------------------------------

TTL_SECONDS   = int(os.environ.get("BLOODBANK_ASM_TTL", "900"))
LANE_GRACE_MS = 300_000      # a subagent lane older than this is presumed done
ERR_GRACE_MS  = 30_000       # how long a failure keeps the row red
ATTENTION_MS  = 1_800_000    # a BELL: capped at 30 min
# A GATE gets no such cap. An agent can sit on a permission prompt for hours, and
# expiring the block would report `working` for an agent that is still stuck --
# the exact false negative the bell/gate split exists to remove. The real bound
# is /proc liveness, which the sweeper owns.
GATE_MS       = int(os.environ.get("BLOODBANK_ASM_GATE_MS", 12 * 3600 * 1000))
STREAM_MAXLEN = 500
TIMEOUT       = float(os.environ.get("BLOODBANK_ASM_TIMEOUT", "0.25"))

_LUA = Path(__file__).with_name("asm.lua")

# Which (cli, event type) pairs have actually fired. A KEY rather than a literal
# inside the Lua so tests can redirect it; see core/asm.lua.
SEEN_KEY = "asm:seen"

# ce_type -> signal. Derived from the SSOT-generated event map rather than a
# hardcoded role list, because hooks.master.json declares TEN roles, not the
# eight its own header comment lists (`turn_complete` and `tool_invoked` are
# both omitted there).
SIGNAL_FOR_TYPE = {
    "bloodbank.agent.session.started":       "start",
    "bloodbank.conversation.turn.started":   "prompt",
    "bloodbank.agent.tool.requested":        "tool_req",
    "bloodbank.agent.tool.completed":        "tool_done",
    "bloodbank.agent.invocation.started":    "sub_start",
    "bloodbank.agent.invocation.completed":  "sub_done",
    "bloodbank.agent.invocation.failed":     "fail",
    # `Stop` is TURN QUIESCENCE, not process exit -- claude, codex and
    # antigravity all bind native Stop to role session_end. Mapping this to a
    # terminal state would delete every live agent's row after every turn.
    # Real exit is observed by the sweeper via /proc, never from an event.
    "bloodbank.agent.session.ended":         "quiesce",
    "bloodbank.conversation.turn.completed": "quiesce",
}

# Rung 1 of the identity ladder: a CLI that publishes its own pid into the hook
# environment. Verified live for claude -- CLAUDE_PID=1698500 matched the
# ancestry walk exactly (starttime 133952459).
PID_ENV_FOR_CLI = {
    "claude": ("CLAUDE_PID",),
    "codex":  ("CODEX_PID",),
}

# Rung 2: the /proc comm to look for when walking up the ancestry chain.
COMM_FOR_CLI = {
    "claude":      ("claude",),
    "codex":       ("codex",),
    "copilot":     ("copilot", "gh"),
    "hermes":      ("hermes",),
    "antigravity": ("antigravity",),
}

# Native session id inside the raw payload, per CLI. Rung 4.
SID_KEYS = ("session_id", "sessionId", "conversationId")

# Rung 2 -- an env var that names the AGENT rather than the process.
#
# Hermes needs this and the other CLIs do not, because a Hermes agent is not a
# process: one profile runs as a long-lived per-profile gateway service AND as N
# transient `hermes-worker-proc_*.scope` units (systemd-run) for cron and tool
# invocations. Keying on the process would file one PM as many agents -- there
# are 9 concurrent james-brennan-pm workers on this box right now, which is one
# agent doing nine things, not nine agents.
#
# HERMES_HOME is present in every one of those processes and names the profile
# exactly, so it is a better identity than any pid. Liveness for these scopes is
# resolved separately, from the per-profile gateway unit -- see core/sweep.py.
IDENTITY_ENV_FOR_CLI = {
    "hermes": "HERMES_HOME",
}

# HERMES_HOME shapes that are infrastructure, not an agent. The single
# fleet-shared command router presents exactly like a profile but represents all
# 25 PMs at once; keying anything on it would collapse the fleet into one row.
# The fleet router presents under TWO different names depending on the source,
# and excluding only one of them excludes neither in practice:
#   HERMES_HOME basename      -> "fleet-bloodbank-gateway"
#   systemd unit, prefix/suffix stripped
#     (hermes-fleet-bloodbank-gateway.service) -> "fleet-bloodbank"
# The first draft listed only the former, and its test asserted the former was
# absent from a map keyed by the latter -- a check that could never fail and
# never verified anything, while the router sat in the gateway map the whole
# time.
NOT_AN_AGENT_PROFILE = frozenset({"fleet-bloodbank-gateway", "fleet-bloodbank"})


def _redis_url() -> str:
    return (
        os.environ.get("ASM_REDIS_URL")
        or os.environ.get("TOOLING_REDIS_URL")
        or os.environ.get("REDIS_URL")
        or "redis://127.0.0.1:6379"
    )


# --- identity --------------------------------------------------------------

def proc_stat(pid: int) -> tuple[str, int, int] | None:
    """Return (comm, ppid, starttime) for *pid*, or None if it is gone.

    comm can itself contain spaces and parentheses, so the split is anchored on
    the LAST ')' rather than on whitespace.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    open_paren = raw.find("(")
    comm = raw[open_paren + 1 : close]
    rest = raw[close + 2 :].split()
    if len(rest) < 20:
        return None
    try:
        # rest[0] is field 3 (state), so field N lives at index N-3.
        return comm, int(rest[1]), int(rest[19])
    except (ValueError, IndexError):
        return None


def _matches(comm: str, cli: str) -> bool:
    wanted = COMM_FOR_CLI.get(cli, (cli,))
    return any(comm == w or comm.startswith(w) for w in wanted)


def resolve_scope(cli: str, payload: object) -> tuple[str, str, int, int]:
    """Resolve (scope, basis, pid, starttime) via the four-rung ladder.

    NOT the zellij pane, and this is settled by measurement rather than taste:
    pane presence is not a per-process invariant. Over 12h of live events, 33
    of 90 codex correlation-sessions contained BOTH paned and unpaned events,
    and codex `session.started` was 0/9 paned against 635/734 paned tool
    events. A pane-keyed row files one agent's tool events and its session
    events in two different places. Headless hermes is 0/1292 paned -- the
    whole PM fleet would simply vanish.

    NOT correlationid either: every CLI keeps ONE session file per machine
    (~/.claude/bloodbank-session.json), written with no locking and reminted on
    every session.ended, so concurrent panes share and rotate it. 58% of claude
    correlationids span more than one working directory.

    `starttime` is what makes pid reuse unconfusable, so no epoch counter is
    needed.
    """
    # Rung 1 -- the CLI told us its own pid.
    for var in PID_ENV_FOR_CLI.get(cli, ()):
        raw = os.environ.get(var, "")
        if raw.isdigit():
            st = proc_stat(int(raw))
            if st is not None:
                return f"{cli}:p:{raw}.{st[2]}", "proc-env", int(raw), st[2]

    # Rung 2 -- an env var that names the agent. Ahead of the ancestry walk on
    # purpose: for a Hermes worker scope the walk WOULD find a `hermes` process,
    # but a transient per-invocation one, producing a fresh row every cron tick.
    env_var = IDENTITY_ENV_FOR_CLI.get(cli)
    if env_var:
        raw = os.environ.get(env_var, "").rstrip("/")
        profile = os.path.basename(raw) if raw else ""
        if profile and profile not in NOT_AN_AGENT_PROFILE:
            return f"{cli}:a:{profile[:96]}", "agent-env", 0, 0

    # Rung 3 -- walk up the ancestry to the first process that IS the CLI.
    pid = os.getpid()
    for _ in range(12):
        st = proc_stat(pid)
        if st is None:
            break
        comm, ppid, starttime = st
        if _matches(comm, cli):
            return f"{cli}:p:{pid}.{starttime}", "proc-walk", pid, starttime
        if ppid <= 1:
            break
        pid = ppid

    # Rung 4 -- a native session id, for a daemon whose comm never matches.
    if isinstance(payload, dict):
        for key in SID_KEYS:
            val = payload.get(key)
            if isinstance(val, str) and val:
                return f"{cli}:s:{val[:96]}", "sid", 0, 0

    # Rung 5 -- the floor. Always available.
    ppid = os.getppid()
    st = proc_stat(ppid)
    starttime = st[2] if st else 0
    return f"{cli}:x:{ppid}.{starttime}", "ppid", ppid, starttime


def _lane(signal: str, payload: object) -> str:
    """Which lane this event belongs to.

    Reports an IDENTITY, never a judgement -- deciding whether this lane is a
    subagent is asm.lua's job, because only it can compare against the first
    lane the scope ever reported. That split matters: CLAUDE_CODE_SESSION_ID is
    set in every child process Claude Code spawns, hook runners included, so a
    judgement made here would mark every claude agent as delegating forever.
    """
    child = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if child:
        return child[:64]
    if isinstance(payload, dict):
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        for key in ("agent_id", "agentId", "subagent_id"):
            val = inner.get(key) if isinstance(inner, dict) else None
            if isinstance(val, str) and val:
                return val[:64]
    if signal in ("sub_start", "sub_done"):
        # We know a subagent exists but cannot name it. One shared lane means N
        # concurrent subagents count as one -- imprecise, never an underflow.
        return "anon"
    return "main"


def _failed(payload: object) -> bool:
    """Does this post_tool payload actually signal an error?

    Honest coverage is poor and that is not this module's fault: claude
    published 39,460 tool completions in 7 days with ZERO errors, because
    PostToolUseFailure and StopFailure exist in the live settings.json but are
    absent from hooks.master.json. Binding those is a separate, high-value fix.
    """
    if not isinstance(payload, dict):
        return False
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    if not isinstance(inner, dict):
        return False
    if inner.get("error"):
        return True
    extra = inner.get("extra")
    if isinstance(extra, dict):
        if extra.get("error_type") or extra.get("error_message"):
            return True
        if str(extra.get("status", "")).lower() in ("error", "failed", "failure"):
            return True
    return False


# --- dispatch --------------------------------------------------------------

def _hub_socket() -> str:
    return os.environ.get("BB_HOOK_SOCKET") or str(
        Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        / "33god/hook-hub.sock"
    )


def dispatch(transition: dict, cli: str, cwd: str) -> None:
    """Fire-and-forget one transition into the live hook-hub socket.

    Deliberately never reads the reply: hub.py tolerates that explicitly
    ("client gave up; its own deadline covers it") and spawns async handlers
    BEFORE composing the reply. Measured p50 0.004 ms / max 0.035 ms over 60
    frames against the running daemon -- versus ~4 ms to spawn a `bb-hook`
    subprocess.

    This needs no hub patch to reach a handler: dispatch() falls back to
    native-name-only selection, and select() already matches
    `native in handler.on_native`, so a row with on_native = ["transition"]
    fires today.
    """
    env = {
        "BB_ASM_FROM":    str(transition.get("from", "")),
        "BB_ASM_TO":      str(transition.get("to", "")),
        "BB_ASM_SCOPE":   str(transition.get("scope", "")),
        "BB_ASM_HELD_MS": str(transition.get("held_ms", "")),
    }
    for var in ("ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        if os.environ.get(var):
            env[var] = os.environ[var]

    req = json.dumps(
        {"v": 1, "cli": cli, "native": "transition",
         "payload": transition, "env": env, "cwd": cwd},
        separators=(",", ":"),
    ).encode("utf-8")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(TIMEOUT)
        sock.connect(_hub_socket())
        sock.sendall(req + b"\n")
        sock.shutdown(socket.SHUT_WR)
    finally:
        try:
            sock.close()
        except OSError:
            pass


# --- entry point -----------------------------------------------------------

def _eval(conn: Connection, keys: list[str], args: list[object]) -> object:
    """EVALSHA with a NOSCRIPT fallback.

    The sha is DERIVED from the script body rather than cached in a file, and
    that is not a micro-optimization -- it is the only version of this that is
    correct. Redis keys its script cache by the SHA1 of the body, so computing
    it here means an edited asm.lua is a different sha by construction.

    The cached-in-a-file version silently kept running the PREVIOUS script
    after every edit: EVALSHA succeeded against Redis's still-warm cache, so
    there was no error to notice, and new signals were simply ignored. Caught
    only because a freshly-added `gone` signal did nothing at all.

    Costs one ~8KB read and a sha1 per hook -- tens of microseconds against a
    250ms deadline.
    """
    body = _LUA.read_bytes()
    sha = hashlib.sha1(body).hexdigest()          # noqa: S324 -- Redis's own key
    try:
        return conn.command("EVALSHA", sha, len(keys), *keys, *args)
    except Exception as exc:
        if "NOSCRIPT" not in str(exc):
            raise
    # First use on this Redis, or the cache was flushed / the server restarted.
    conn.command("SCRIPT", "LOAD", body)
    return conn.command("EVALSHA", sha, len(keys), *keys, *args)


def fire(conn: Connection, scope: str, signal: str, h: dict,
         live_key: str = "asm:live", seen_key: str = "asm:seen") -> dict | None:
    """Push one signal for an EXISTING scope through asm.lua. The transition, or None.

    The proposer entry point for everything that is not a hook: the sweeper's
    `stale`/`gone`/`discover` verdicts and a surface's `ack`. Those observers
    know a scope and a fact about it, not a CloudEvent, so `record()`'s identity
    ladder has nothing to resolve -- but the arbitration must still be the same
    arbitration, or there are two state machines again.

    `h` is the scope's current hash; its fields are carried straight back so an
    out-of-band signal cannot blank the identity that hooks established.
    """
    zsess, zpane = h.get("zellij_session", ""), h.get("zellij_pane", "")
    pane_idx = f"asm:idx:pane:{zsess}:{zpane}" if (zsess and zpane) else ""
    meta = {
        "cli": h.get("cli", ""), "pid": h.get("pid", ""),
        "starttime": h.get("starttime", ""), "cwd": h.get("cwd", ""),
        "basis": h.get("basis", ""), "zellij_session": zsess,
        "zellij_pane": zpane, "correlationid": h.get("correlationid", ""),
        # NOT a role, and deliberately NOT written into the telemetry hash:
        # `stale`, `gone`, `discover` and `ack` are VERDICTS about an agent, not
        # event types it emitted. Stamping them as roles put `claude|sweep:ack`
        # and `hermes|sweep:gone` into asm:seen beside real CloudEvents types,
        # and attributed a surface's ack to the sweeper. The verdict is already
        # carried by `reason` on the transition.
        "session_id": h.get("session_id", ""), "last_role": "",
        "profile": h.get("profile", ""),
    }
    keys = [f"asm:a:{scope}", f"asm:t:{scope}", f"asm:lane:{scope}",
            live_key, pane_idx, seen_key]
    args = [signal, TTL_SECONDS, "main", json.dumps(meta, separators=(",", ":")),
            LANE_GRACE_MS, ERR_GRACE_MS, ATTENTION_MS, STREAM_MAXLEN, scope]
    result = _eval(conn, keys, args)
    if not result:
        return None
    try:
        return json.loads(result)
    except (TypeError, ValueError):
        return None


def record(cli: str, ce_type: str | None, alert_kind: str | None,
           payload: object, log=None, attention_kind: str | None = None) -> None:
    """Fold one hook into the state machine. Never raises. Never blocks."""
    if os.environ.get("BLOODBANK_ASM", "true") != "true":
        return
    try:
        if alert_kind == "attention":
            signal = "attention"
        elif ce_type:
            signal = SIGNAL_FOR_TYPE.get(ce_type)
            if signal == "tool_done" and _failed(payload):
                signal = "fail"
        else:
            return
        if not signal:
            return

        scope, basis, pid, starttime = resolve_scope(cli, payload)
        cwd = os.getcwd()
        zsess = os.environ.get("ZELLIJ_SESSION_NAME", "")
        zpane = os.environ.get("ZELLIJ_PANE_ID", "")

        meta = {
            "cli": cli, "pid": pid, "starttime": starttime, "cwd": cwd,
            "basis": basis, "zellij_session": zsess, "zellij_pane": zpane,
            "correlationid": "", "session_id": "",
            "last_role": ce_type or alert_kind or "",
            # Which kind of attention, straight from the SSOT. A bell is answered
            # by being seen; a gate is answered only by a keypress, so a surface
            # must never clear one. Absent => bell, deliberately: wrongly
            # acknowledging a bell costs a repaint, wrongly holding a gate open
            # would freeze an agent in awaiting_human until its process dies.
            "attention_kind": attention_kind or "bell",
            "gate_ms": GATE_MS,
            # Only set for an agent-env scope; the sweeper resolves liveness
            # from this profile's gateway unit rather than from a pid.
            "profile": scope.split(":a:", 1)[1] if ":a:" in scope else "",
        }
        # The pane is carried as a FIELD and a secondary index, never as
        # identity -- see resolve_scope().
        pane_idx = f"asm:idx:pane:{zsess}:{zpane}" if (zsess and zpane) else ""

        keys = [f"asm:a:{scope}", f"asm:t:{scope}", f"asm:lane:{scope}",
                "asm:live", pane_idx, SEEN_KEY]
        args = [signal, TTL_SECONDS, _lane(signal, payload),
                json.dumps(meta, separators=(",", ":")),
                LANE_GRACE_MS, ERR_GRACE_MS, ATTENTION_MS, STREAM_MAXLEN, scope]

        with Connection(_redis_url(), timeout=TIMEOUT) as conn:
            result = _eval(conn, keys, args)

        if not result:
            return                      # no-op edge; nothing changed
        try:
            transition = json.loads(result)
        except (TypeError, ValueError):
            return
        dispatch(transition, cli, cwd)
    except Exception as exc:            # noqa: BLE001 -- fail-open is the point
        if log is not None:
            try:
                log(f"asm skipped: {exc!r}")
            except Exception:
                pass
