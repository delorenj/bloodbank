#!/usr/bin/env python3
"""hook-hub — one dispatcher for every agent CLI's hooks.

Every supported agent CLI already re-triggers into one shared lifecycle-role
vocabulary (services/agent-hooks/hooks.master.json). What never followed was the
BEHAVIOR: each concern stayed hand-wired into every CLI's native config, so
adding one meant editing six files in five dialects. This daemon is where that
behavior moves. One registry (handlers.toml) binds handlers to lifecycle roles,
and every CLI reaches it through the same `bb-hook` re-trigger.

Scope note (deliberate): this daemon DISPATCHES; it does not publish. Envelope
publishing stays in services/agent-hooks/publish.py until the cutover phase that
moves it, so the freshly-landed alert-fanout logic is not duplicated here. See
README.md.

Two invariants:

  * A handler can never wedge an agent. Sync handlers share one deadline set by
    the caller's budget; async handlers are launched and forgotten behind a
    bounded pool. Every subprocess gets a timeout and is killed at it.
  * A malformed request, a missing handler binary, a broken registry -- none of
    these may take the daemon down. Failures are logged and scoped to the one
    connection or the one handler that caused them.

Stdlib-only (tomllib is stdlib on 3.11+), matching the rest of agent-hooks.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

SERVICE_DIR = Path(__file__).resolve().parent
AGENT_HOOKS_DIR = SERVICE_DIR.parent / "agent-hooks"
MASTER = AGENT_HOOKS_DIR / "hooks.master.json"
REGISTRY = Path(os.environ.get("HOOK_HUB_REGISTRY", SERVICE_DIR / "handlers.toml"))

MAX_REQUEST_BYTES = 1 << 20
ASYNC_SLOTS = int(os.environ.get("HOOK_HUB_ASYNC_SLOTS", "8"))
SYNC_BUDGET = float(os.environ.get("HOOK_HUB_SYNC_BUDGET", "2.5"))
LOG_MAX_BYTES = 1 << 20

STATE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
) / "33god/hook-hub"
LOG_PATH = Path(os.environ.get("HOOK_HUB_LOG", STATE_DIR / "hub.log"))

SD_LISTEN_FDS_START = 3


# --------------------------------------------------------------------------
# Logging — best effort, size-rotated, never raises
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} [{os.getpid()}] {msg}\n"
    if os.environ.get("HOOK_HUB_STDERR"):
        sys.stderr.write(line)
        sys.stderr.flush()
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            LOG_PATH.replace(LOG_PATH.with_suffix(LOG_PATH.suffix + ".1"))
        with LOG_PATH.open("a") as fh:
            fh.write(line)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

class Handler:
    """One registry row."""

    __slots__ = ("id", "mode", "on", "on_native", "command", "timeout_ms",
                 "match_tool", "require_env", "order", "enabled")

    def __init__(self, raw: dict[str, Any]) -> None:
        self.id: str = str(raw["id"])
        self.mode: str = str(raw.get("mode", "async"))
        self.on: set[str] = set(raw.get("on", []) or [])
        self.on_native: set[str] = set(raw.get("on_native", []) or [])
        self.command: list[str] = [
            os.path.expanduser(str(part)) for part in raw["command"]
        ]
        self.timeout_ms: int = int(raw.get("timeout_ms", 5000))
        pattern = raw.get("match_tool")
        self.match_tool = re.compile(str(pattern)) if pattern else None
        self.require_env: list[str] = [str(k) for k in raw.get("require_env", [])]
        self.order: int = int(raw.get("order", 100))
        self.enabled: bool = bool(raw.get("enabled", True))
        if self.mode not in ("sync", "async"):
            raise ValueError(f"handler {self.id}: mode must be sync|async")
        if not self.on and not self.on_native:
            raise ValueError(f"handler {self.id}: needs `on` or `on_native`")


class Config:
    """hooks.master.json + handlers.toml, reloaded when either changes on disk."""

    def __init__(self) -> None:
        self.handlers: list[Handler] = []
        self.bindings: dict[tuple[str, str], dict[str, Any]] = {}
        self._stamps: tuple = ()

    @staticmethod
    def _stamp(path: Path) -> float:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return -1.0

    def maybe_reload(self) -> None:
        stamps = (self._stamp(MASTER), self._stamp(REGISTRY))
        if stamps == self._stamps:
            return
        try:
            self._load()
            self._stamps = stamps
        except Exception as exc:
            # Keep serving the last good config: dispatching against a
            # half-parsed registry is worse than dispatching against a stale one.
            log(f"config reload FAILED, keeping previous: {exc!r}")
            self._stamps = stamps

    def _load(self) -> None:
        bindings: dict[tuple[str, str], dict[str, Any]] = {}
        master = json.loads(MASTER.read_text())
        for cli, agent in (master.get("agents") or {}).items():
            for binding in agent.get("bindings") or []:
                native = binding.get("native")
                if native:
                    bindings[(cli, str(native))] = binding

        handlers: list[Handler] = []
        raw = tomllib.loads(REGISTRY.read_text())
        for row in raw.get("handler", []) or []:
            try:
                handler = Handler(row)
            except Exception as exc:
                log(f"skipping invalid handler row {row.get('id')!r}: {exc}")
                continue
            if handler.enabled:
                handlers.append(handler)
        handlers.sort(key=lambda h: (h.order, h.id))

        self.bindings, self.handlers = bindings, handlers
        log(f"config loaded: {len(handlers)} handlers, {len(bindings)} bindings")

    def select(self, role: str | None, native: str, payload: Any,
               env: dict[str, str]) -> list[Handler]:
        tool = ""
        if isinstance(payload, dict):
            tool = str(payload.get("tool_name") or payload.get("toolName") or "")
        out = []
        for h in self.handlers:
            if not ((role and role in h.on) or native in h.on_native):
                continue
            if h.match_tool is not None and not h.match_tool.search(tool):
                continue
            # A handler that needs pane context is not broken outside zellij --
            # it simply has nothing to act on. Skip quietly.
            if any(not env.get(k) for k in h.require_env):
                continue
            out.append(h)
        return out


# --------------------------------------------------------------------------
# Handler execution
# --------------------------------------------------------------------------

def _child_env(req: dict[str, Any], role: str | None) -> dict[str, str]:
    env = dict(os.environ)
    for key, value in (req.get("env") or {}).items():
        if isinstance(key, str) and isinstance(value, str):
            env[key] = value
    env["BB_HOOK_CLI"] = str(req.get("cli", ""))
    env["BB_HOOK_NATIVE"] = str(req.get("native", ""))
    env["BB_HOOK_ROLE"] = role or ""
    # Handlers that re-enter an agent CLI must not re-enter the hub.
    env["BB_HOOK_HUB"] = "off"
    return env


def _child_cwd(req: dict[str, Any]) -> str | None:
    cwd = req.get("cwd")
    if isinstance(cwd, str) and cwd and os.path.isdir(cwd):
        return cwd
    return None


async def run_handler(h: Handler, req: dict[str, Any], role: str | None,
                      stdin_bytes: bytes, budget_s: float) -> str:
    """Run one handler. Returns its stdout (empty on any failure)."""
    timeout = min(h.timeout_ms / 1000.0, budget_s) if budget_s > 0 else 0
    if timeout <= 0:
        log(f"handler {h.id}: no budget left, skipped")
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *h.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(req, role),
            cwd=_child_cwd(req),
            # New session => the handler is a process-group leader, so the
            # timeout path can reap its whole tree with killpg. It does NOT
            # make children die with the parent -- that is the opposite, and
            # assuming it is how 8 `play` processes ended up stuck on this box
            # for 1d11h, adopted by systemd --user after claude-notify exited.
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        log(f"handler {h.id}: spawn failed: {exc}")
        return ""

    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes), timeout=timeout
        )
    except asyncio.TimeoutError:
        log(f"handler {h.id}: TIMEOUT after {timeout:.2f}s, killed")
        _kill(proc)
        return ""
    except Exception as exc:
        log(f"handler {h.id}: failed: {exc!r}")
        _kill(proc)
        return ""

    if proc.returncode not in (0, None):
        tail = (err or b"").decode("utf-8", "replace").strip()[-400:]
        log(f"handler {h.id}: exit={proc.returncode} stderr={tail!r}")
    return (out or b"").decode("utf-8", "replace")


def _kill(proc: Any) -> None:
    """Reap the handler AND everything it spawned.

    `proc.kill()` alone signals only the direct child, so a handler that forks a
    player, a curl, or an ssh and then exits leaves that grandchild running
    forever -- exactly the leak observed on this host, where claude-notify's
    `play` children pile up blocked in futex_do_wait and get adopted by
    systemd --user.

    Because handlers start in a new session (see run_handler), the child is its
    own process-group leader and killpg reaps the whole tree. SIGTERM first so a
    handler can clean up, SIGKILL right after for anything ignoring it.

    Handlers that deliberately `setsid` their own worker (hindsight-session-end,
    merge-forward) put it in a FURTHER new session, so it correctly escapes this
    -- their detached work is meant to outlive the hook.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                continue
            except (ProcessLookupError, PermissionError, OSError):
                pgid = None          # fall through to the single-process path
        try:
            proc.send_signal(sig) if sig is signal.SIGTERM else proc.kill()
        except (ProcessLookupError, OSError):
            return


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class Server:
    def __init__(self) -> None:
        self.cfg = Config()
        self.slots = asyncio.Semaphore(ASYNC_SLOTS)
        self.background: set[asyncio.Task] = set()

    def spawn_async(self, h: Handler, req: dict, role: str | None,
                    stdin_bytes: bytes) -> None:
        async def guarded() -> None:
            async with self.slots:
                await run_handler(h, req, role, stdin_bytes, h.timeout_ms / 1000.0)

        task = asyncio.create_task(guarded())
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        self.cfg.maybe_reload()
        cli = str(req.get("cli", ""))
        native = str(req.get("native", ""))
        payload = req.get("payload")
        env = {k: v for k, v in (req.get("env") or {}).items()
               if isinstance(k, str) and isinstance(v, str)}

        binding = self.cfg.bindings.get((cli, native))
        role = binding.get("role") if binding else None
        if binding is None:
            log(f"no binding for {cli}/{native} (dispatching on native name only)")

        selected = self.cfg.select(role, native, payload, env)
        if not selected:
            return {"v": 1, "stdout": "", "exit_code": 0, "handled": []}

        try:
            stdin_bytes = json.dumps(payload if payload is not None else {}).encode()
        except (TypeError, ValueError):
            stdin_bytes = b"{}"

        sync = [h for h in selected if h.mode == "sync"]
        for h in selected:
            if h.mode == "async":
                self.spawn_async(h, req, role, stdin_bytes)

        chunks: list[str] = []
        deadline = time.monotonic() + SYNC_BUDGET
        for h in sync:
            out = await run_handler(
                h, req, role, stdin_bytes, deadline - time.monotonic()
            )
            if out.strip():
                chunks.append(out.rstrip("\n"))

        return {
            "v": 1,
            "stdout": "\n\n".join(chunks),
            "exit_code": 0,
            "handled": [h.id for h in selected],
        }

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(
                reader.read(MAX_REQUEST_BYTES + 1), timeout=SYNC_BUDGET + 1
            )
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("request too large")
            req = json.loads(raw)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
            reply = await self.dispatch(req)
        except Exception as exc:
            log(f"request failed: {exc!r}")
            reply = {"v": 1, "stdout": "", "exit_code": 0, "handled": []}

        try:
            writer.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
        except (OSError, ConnectionError):
            pass                       # client gave up; its own deadline covers it
        finally:
            try:
                writer.close()
            except OSError:
                pass


def listener() -> socket.socket | None:
    """Return the systemd-activated listening socket, if we were socket-activated."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    count = int(os.environ.get("LISTEN_FDS", "0") or 0)
    if count < 1:
        return None
    sock = socket.socket(fileno=SD_LISTEN_FDS_START)
    sock.setblocking(False)
    return sock


async def main() -> int:
    server_obj = Server()
    server_obj.cfg.maybe_reload()

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig, lambda: stop.done() or stop.set_result(None)
        )
    # SIGHUP forces a reload even if mtime granularity hid the change.
    loop.add_signal_handler(signal.SIGHUP, lambda: setattr(
        server_obj.cfg, "_stamps", ()
    ))

    sock = listener()
    if sock is not None:
        server = await asyncio.start_unix_server(server_obj.handle, sock=sock)
        log("listening on systemd-activated socket")
    else:
        path = os.environ.get("BB_HOOK_SOCKET") or str(
            Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
            / "33god/hook-hub.sock"
        )
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if Path(path).exists():
            Path(path).unlink()        # stale socket from an unclean exit
        server = await asyncio.start_unix_server(server_obj.handle, path=path)
        os.chmod(path, 0o600)
        log(f"listening on {path}")

    async with server:
        await stop
    log("shutting down")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
