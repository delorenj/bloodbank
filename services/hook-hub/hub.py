#!/usr/bin/env python3
"""hook-hub — one dispatcher for every agent CLI's hooks.

Every supported agent CLI already re-triggers into one shared lifecycle-role
vocabulary (services/agent-hooks/hooks.master.json). What never followed was the
BEHAVIOR: each concern stayed hand-wired into every CLI's native config, so
adding one meant editing six files in five dialects. This daemon is where that
behavior moves. One registry (handlers.toml) binds handlers to lifecycle roles,
and every CLI reaches it through the same `bb-hook` re-trigger.

The canonical publisher is one asynchronous execution alongside the behavioral
handlers. Broker availability never sits on the synchronous CLI response path.
Every selection and outcome is recorded in the payload-free receipt journal.

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
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
import importlib.util
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
from urllib.parse import parse_qs, unquote, urlsplit

from receipts import (SCHEMA_VERSION, ReceiptStore, invocation_identity,
                      native_session_id, now_iso)
from facts import configuration_fingerprint
from observations import OutboxWorker

SERVICE_DIR = Path(__file__).resolve().parent
AGENT_HOOKS_DIR = SERVICE_DIR.parent / "agent-hooks"
MASTER = AGENT_HOOKS_DIR / "hooks.master.json"
REGISTRY = Path(os.environ.get("HOOK_HUB_REGISTRY", SERVICE_DIR / "handlers.toml"))

MAX_REQUEST_BYTES = 1 << 20
ASYNC_SLOTS = int(os.environ.get("HOOK_HUB_ASYNC_SLOTS", "8"))
PUBLISH_SLOTS = max(1, int(os.environ.get("HOOK_HUB_PUBLISH_SLOTS", "2")))
SHUTDOWN_GRACE = min(2.0, max(0.0, float(os.environ.get("HOOK_HUB_SHUTDOWN_GRACE", "2.0"))))
SYNC_BUDGET = float(os.environ.get("HOOK_HUB_SYNC_BUDGET", "2.5"))
MAX_SYNC_BUDGET = float(os.environ.get("HOOK_HUB_MAX_SYNC_BUDGET", "14.0"))
LOG_MAX_BYTES = 1 << 20

STATE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
) / "33god/hook-hub"
LOG_PATH = Path(os.environ.get("HOOK_HUB_LOG", STATE_DIR / "hub.log"))
RECEIPT_PATH = Path(os.environ.get("HOOK_HUB_RECEIPTS", STATE_DIR / "receipts.sqlite3"))
HTTP_HOST = os.environ.get("HOOK_HUB_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("HOOK_HUB_HTTP_PORT", "8685"))
PUBLISH_ENABLED = os.environ.get("HOOK_HUB_PUBLISH", "true") == "true"
OBSERVATIONS_ENABLED = (os.environ.get("HOOK_HUB_OBSERVATIONS_PUBLISH", str(PUBLISH_ENABLED).lower()) == "true"
                        and os.environ.get("BLOODBANK_ENABLED", "true") == "true")
OBSERVATION_INTERVAL = max(1.0, float(os.environ.get("HOOK_HUB_OBSERVATION_INTERVAL", "30")))
# One native hook mutates its receipt ~13 times in a second or two (claim, then
# every handler's select/start/finish). Consumers keep only the newest revision
# per invocation, so an unsettled invocation publishes after this much quiet,
# and never later than MAX_DELAY after its first unpublished change. A settled
# invocation publishes at once.
OBSERVATION_DEBOUNCE = max(0.0, float(os.environ.get("HOOK_HUB_OBSERVATION_DEBOUNCE", "2.0")))
OBSERVATION_MAX_DELAY = max(OBSERVATION_DEBOUNCE, float(os.environ.get("HOOK_HUB_OBSERVATION_MAX_DELAY", "10.0")))
MAX_HANDLER_OUTPUT = 1 << 20

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
                 "match_tool", "match_transition", "require_env", "order",
                 "enabled", "clis", "after")

    def __init__(self, raw: dict[str, Any]) -> None:
        self.id: str = str(raw["id"])
        self.mode: str = str(raw.get("mode", "async"))
        self.on: set[str] = set(raw.get("on", []) or [])
        self.on_native: set[str] = set(raw.get("on_native", []) or [])
        self.clis: set[str] = set(raw.get("clis", []) or [])
        self.after: set[str] = set(raw.get("after", []) or [])
        self.command: list[str] = [
            os.path.expanduser(str(part)) for part in raw["command"]
        ]
        self.timeout_ms: int = int(raw.get("timeout_ms", 5000))
        pattern = raw.get("match_tool")
        self.match_tool = re.compile(str(pattern)) if pattern else None
        # Regex over the literal "<from>-><to>" of an agent-state transition.
        # ANCHOR IT. An unanchored "." matches every edge on the box -- roughly
        # 11 subprocess spawns a minute at current hook volume, forever.
        transition = raw.get("match_transition")
        self.match_transition = (
            re.compile(str(transition)) if transition else None
        )
        self.require_env: list[str] = [str(k) for k in raw.get("require_env", [])]
        self.order: int = int(raw.get("order", 100))
        self.enabled: bool = bool(raw.get("enabled", True))
        if self.mode not in ("sync", "async"):
            raise ValueError(f"handler {self.id}: mode must be sync|async")
        if not self.command or self.timeout_ms <= 0:
            raise ValueError(f"handler {self.id}: command and positive timeout required")
        if not self.on and not self.on_native:
            raise ValueError(f"handler {self.id}: needs `on` or `on_native`")


class Config:
    """hooks.master.json + handlers.toml, reloaded when either changes on disk."""

    def __init__(self) -> None:
        self.handlers: list[Handler] = []
        self.bindings: dict[tuple[str, str], dict[str, Any]] = {}
        self.error: str | None = None
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
            self.error = None
        except Exception as exc:
            # Keep serving the last good config: dispatching against a
            # half-parsed registry is worse than dispatching against a stale one.
            self.error = type(exc).__name__
            log(f"config reload FAILED, keeping previous: {self.error}")
            self._stamps = stamps

    def _load(self) -> None:
        bindings: dict[tuple[str, str], dict[str, Any]] = {}
        master = json.loads(MASTER.read_text())
        for cli, agent in (master.get("agents") or {}).items():
            for binding in agent.get("bindings") or []:
                native = binding.get("native")
                if native:
                    binding = dict(binding)
                    binding["support_status"] = agent.get("support_status", "supported")
                    binding["event_type"] = (master.get("lifecycle", {}).get(binding.get("lifecycle"), {}).get("type"))
                    if binding.get("alert") == "attention":
                        binding["event_type"] = "deckard.v1.agent.attention"
                    bindings[(cli, str(native))] = binding

        handlers: list[Handler] = []
        raw = tomllib.loads(REGISTRY.read_text())
        for row in raw.get("handler", []) or []:
            try:
                handler = Handler(row)
            except Exception as exc:
                log(f"skipping invalid handler row {row.get('id')!r}: {exc}")
                continue
            if any(h.id == handler.id for h in handlers):
                raise ValueError("duplicate handler id")
            handlers.append(handler)
        handlers.sort(key=lambda h: (h.order, h.id))

        self.bindings, self.handlers = bindings, handlers
        log(f"config loaded: {len(handlers)} handlers, {len(bindings)} bindings")

    def selections(self, role: str | None, native: str, payload: Any,
                   env: dict[str, str], transition: str = "", cli: str = "") -> list[tuple[Handler, str | None]]:
        tool = ""
        if isinstance(payload, dict):
            tool = str(payload.get("tool_name") or payload.get("toolName") or "")
        out = []
        for h in self.handlers:
            if h.clis and cli not in h.clis:
                continue
            if not ((role and role in h.on) or native in h.on_native):
                continue
            reason = None
            if not h.enabled:
                reason = "disabled"
            elif h.match_tool is not None and not h.match_tool.search(tool):
                reason = "tool_not_matched"
            elif (h.match_transition is not None
                    and not h.match_transition.search(transition)):
                reason = "transition_not_matched"
            # A handler that needs pane context is not broken outside zellij --
            # it simply has nothing to act on. Skip quietly.
            elif any(not env.get(k) for k in h.require_env):
                reason = "missing_environment"
            out.append((h, reason))
        return out

    def select(self, role: str | None, native: str, payload: Any,
               env: dict[str, str], transition: str = "", cli: str = "") -> list[Handler]:
        return [h for h, reason in self.selections(role, native, payload, env, transition, cli) if reason is None]


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
    env["BB_HOOK_INVOCATION_ID"] = str(req.get("invocation_id", ""))
    env["BB_HOOK_SESSION_ID"] = native_session_id(req)
    env["BB_HOOK_PARENT_PID"] = str(req.get("parent_pid", ""))
    # Handlers that re-enter an agent CLI must not re-enter the hub.
    env["BB_HOOK_HUB"] = "off"
    return env


def _child_cwd(req: dict[str, Any]) -> str | None:
    cwd = req.get("cwd")
    if isinstance(cwd, str) and cwd and os.path.isdir(cwd):
        return cwd
    return None


@dataclass
class HandlerResult:
    status: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    reason: str | None = None
    duration_ms: float = 0.0
    event_id: str | None = None
    event_type: str | None = None
    publish_status: str | None = None


def decode_result(out: bytes, err: bytes, code: int, *, publisher: bool = False) -> HandlerResult:
    """Metadata is consumed here; only the native result reaches the CLI."""
    text = out.decode("utf-8", "replace")
    result = HandlerResult("succeeded" if code == 0 else "failed", text,
                           err.decode("utf-8", "replace") if code == 2 else "", code,
                           None if code == 0 else "nonzero_exit")
    try:
        value = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        value = None
    if publisher:
        if not isinstance(value, dict) or value.get("status") not in {"succeeded", "failed", "skipped"}:
            return HandlerResult("failed", exit_code=code, reason="invalid_publisher_receipt", publish_status="unknown")
        return HandlerResult(value["status"], exit_code=code,
                             reason=_reason(value.get("reason")),
                             event_id=_field(value.get("event_id")),
                             event_type=_field(value.get("event_type")),
                             publish_status=_field(value.get("publish_status")))
    if isinstance(value, dict) and isinstance(value.get("_hook_hub"), dict):
        meta = value["_hook_hub"]
        status = meta.get("status")
        if status in {"succeeded", "failed", "skipped"}:
            result.status = status
            result.reason = _reason(meta.get("reason"))
        result.stdout = value.get("stdout", "") if isinstance(value.get("stdout", ""), str) else ""
        # Only deliberate native blocking (2) is propagated. Other subprocess
        # failures remain fail-open, regardless of the wrapper's metadata.
        if meta.get("exit_code") == 2:
            result.exit_code = 2
    return result


def _field(value: Any) -> str | None:
    return value[:256] if isinstance(value, str) else None


def _reason(value: Any) -> str | None:
    # Diagnostic codes, never a handler's arbitrary error/payload text.
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", value) else None


async def _communicate(proc: Any, data: bytes) -> tuple[bytes, bytes]:
    async def read(stream: Any) -> bytes:
        result = bytearray()
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return bytes(result)
            result.extend(chunk)
            if len(result) > MAX_HANDLER_OUTPUT:
                raise ValueError("output_limit_exceeded")

    async def write() -> None:
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    _, out, err, _ = await asyncio.gather(write(), read(proc.stdout), read(proc.stderr), proc.wait())
    return out, err


async def run_handler(h: Handler, req: dict[str, Any], role: str | None,
                      stdin_bytes: bytes, budget_s: float) -> HandlerResult:
    """Run once with a bounded process group and a truthful execution result."""
    started = time.monotonic()
    timeout = min(h.timeout_ms / 1000.0, budget_s) if budget_s > 0 else 0
    if timeout <= 0:
        log(f"handler {h.id}: no budget left, skipped")
        return HandlerResult("skipped", reason="sync_budget_exhausted")
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
        log(f"handler {h.id}: spawn failed: {type(exc).__name__}")
        return HandlerResult("failed", reason="spawn_failed")

    try:
        out, err = await asyncio.wait_for(
            _communicate(proc, stdin_bytes), timeout=timeout
        )
    except asyncio.TimeoutError:
        log(f"handler {h.id}: TIMEOUT after {timeout:.2f}s, killed")
        _kill(proc)
        await proc.wait()
        return HandlerResult("timed_out", reason="timeout", duration_ms=round((time.monotonic() - started) * 1000, 2))
    except Exception as exc:
        log(f"handler {h.id}: failed: {type(exc).__name__}")
        _kill(proc)
        await proc.wait()
        return HandlerResult("failed", reason="output_limit_exceeded" if isinstance(exc, ValueError) else "execution_error")
    except asyncio.CancelledError:
        _kill(proc)
        await proc.wait()
        raise

    if proc.returncode not in (0, None):
        log(f"handler {h.id}: exit={proc.returncode}")
    result = decode_result(out or b"", err or b"", proc.returncode or 0,
                           publisher=h.id == "bloodbank-publisher")
    if result.status in {"failed", "skipped"} and result.exit_code != 2:
        result.stdout = ""
    result.duration_ms = round((time.monotonic() - started) * 1000, 2)
    return result


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

def compose_stdout(chunks: list[str]) -> str:
    """Keep multiple native JSON hook responses valid and preserve denials."""
    if not chunks:
        return ""
    objects: list[dict] = []
    plain: list[str] = []
    for chunk in chunks:
        try:
            value = json.loads(chunk)
        except ValueError:
            value = None
        if isinstance(value, dict):
            objects.append(value)
        elif chunk.strip():
            plain.append(chunk.rstrip("\n"))
    if not objects:
        return "\n\n".join(plain)

    def merge(dst: dict, src: dict) -> None:
        for key, value in src.items():
            if key not in dst:
                dst[key] = value
            elif isinstance(dst[key], dict) and isinstance(value, dict):
                merge(dst[key], value)
            elif key in {"additionalContext", "systemMessage", "reason", "permissionDecisionReason", "context"}:
                dst[key] = "\n\n".join(str(v) for v in (dst[key], value) if v)
            elif key == "permissionDecision":
                rank = {"allow": 0, "ask": 1, "deny": 2}
                if rank.get(str(value), -1) > rank.get(str(dst[key]), -1):
                    dst[key] = value
            elif key == "decision" and value == "block":
                dst[key] = value
            elif key == "continue" and value is False:
                dst[key] = False
    combined: dict[str, Any] = {}
    for obj in objects:
        merge(combined, obj)
    if plain:
        extra = combined.setdefault("hookSpecificOutput", {})
        if isinstance(extra, dict):
            merge(extra, {"additionalContext": "\n\n".join(plain)})
    return json.dumps(combined, separators=(",", ":"))


class Server:
    def __init__(self) -> None:
        self.cfg = Config()
        self.slots = asyncio.Semaphore(ASYNC_SLOTS)
        self.publish_slots = asyncio.Semaphore(PUBLISH_SLOTS)
        self.background: set[asyncio.Task] = set()
        self.connections: set[asyncio.Task] = set()
        self.draining = False
        self.pending: dict[str, asyncio.Future] = {}
        self.replies: OrderedDict[str, dict] = OrderedDict()
        self.session_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.session_tasks: dict[tuple[str, str], dict[asyncio.Task, str]] = {}
        self.store = ReceiptStore(RECEIPT_PATH, debounce=OBSERVATION_DEBOUNCE,
                                  max_delay=OBSERVATION_MAX_DELAY)
        self.store.recover()
        self.observations = OutboxWorker(self.store, log=log)
        self.started_at = now_iso()
        self.journal_error: str | None = None
        self.inventory: dict | None = None
        self.inventory_at = 0.0
        self.inventory_lock = asyncio.Lock()
        self.socket_path: Path | None = None
        self.socket_activated = False

    async def journal(self, method: str, *args, **kwargs):
        try:
            result = await asyncio.to_thread(getattr(self.store, method), *args, **kwargs)
            self.journal_error = None
            if method in {"claim", "select", "update", "finish", "interrupt", "interrupt_pending", "observe_snapshot"}:
                self.observations.wake.set()
            return result
        except Exception as exc:
            self.journal_error = type(exc).__name__
            log(f"receipt journal {method} failed: {self.journal_error}")
            raise

    def task(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def execute(self, h: Handler, req: dict, role: str | None,
                      stdin_bytes: bytes, budget_s: float) -> HandlerResult:
        iid = req["invocation_id"]
        try:
            await self.journal("update", iid, h.id, "started")
            result = await run_handler(h, req, role, stdin_bytes, budget_s)
            await self.journal("update", iid, h.id, result.status, reason=result.reason,
                               duration_ms=result.duration_ms, exit_code=result.exit_code,
                               event_id=result.event_id, event_type=result.event_type,
                               publish_status=result.publish_status)
        except asyncio.CancelledError:
            await self.journal("interrupt", iid, h.id)
            raise
        return result

    def spawn_async(self, h: Handler, req: dict, role: str | None,
                    stdin_bytes: bytes) -> None:
        key = (str(req.get("cli")), native_session_id(req) or str(req.get("cwd", "")))
        related = self.session_tasks.setdefault(key, {})
        dependencies = [task for task, handler_id in related.items() if handler_id in h.after]
        async def work() -> None:
            slots = self.publish_slots if h.id == "bloodbank-publisher" else self.slots
            async with slots:
                await self.execute(h, req, role, stdin_bytes, h.timeout_ms / 1000.0)

        async def guarded() -> None:
            try:
                if dependencies:
                    await asyncio.gather(*dependencies, return_exceptions=True)
                if h.id == "bloodbank-publisher":
                    # Causation chains and counters belong to one native
                    # session. Preserve event arrival order within it while
                    # independent sessions publish concurrently.
                    lock = self.session_locks.setdefault(key, asyncio.Lock())
                    async with lock:
                        await work()
                    # All waiters share this lock; remove only after the last.
                    if not lock.locked() and not getattr(lock, "_waiters", None):
                        self.session_locks.pop(key, None)
                else:
                    await work()
            except asyncio.CancelledError:
                await self.journal("interrupt", req["invocation_id"], h.id)
                raise
            except Exception as exc:
                log(f"async handler {h.id} failed: {type(exc).__name__}")
        task = self.task(guarded())
        related[task] = h.id
        def release(done: asyncio.Task) -> None:
            related.pop(done, None)
            if not related:
                self.session_tasks.pop(key, None)
        task.add_done_callback(release)

    async def drain(self) -> None:
        """Finish accepted work before a bounded, explicitly recorded stop."""
        self.draining = True
        log(f"draining {len(self.connections)} connections and {len(self.background)} handlers for at most {SHUTDOWN_GRACE:.1f}s")
        deadline = time.monotonic() + SHUTDOWN_GRACE
        # Closing the listener prevents new accepts; run already queued
        # connection callbacks before taking the first task snapshot.
        await asyncio.sleep(0)
        while time.monotonic() < deadline:
            active = {task for task in self.connections | self.background if not task.done()}
            if not active:
                break
            await asyncio.wait(active, timeout=max(0.0, deadline - time.monotonic()),
                               return_when=asyncio.FIRST_COMPLETED)
        remaining = {task for task in self.connections | self.background if not task.done()}
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        await self.journal("interrupt_pending")
        log(f"shutdown drain finished; interrupted {len(remaining)} remaining tasks")

    def publisher(self, cli: str, native: str, binding: dict | None) -> Handler | None:
        if not PUBLISH_ENABLED or native == "transition" or binding is None:
            return None
        if binding.get("support_status", "supported") != "supported":
            return None
        if not binding.get("event_type"):
            return None
        return Handler({
            "id": "bloodbank-publisher", "mode": "async", "on_native": [native],
            "command": [sys.executable, str(AGENT_HOOKS_DIR / "publish.py"),
                        "--report", "--client", cli, "--hook", native],
            "timeout_ms": 10000,
        })

    async def dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        if req.get("op") == "status":
            return await self.status()
        self.cfg.maybe_reload()
        cli = str(req.get("cli", ""))[:64]
        native = str(req.get("native", ""))[:128]
        if not cli or not native:
            return {"v": 1, "stdout": "", "exit_code": 0, "handled": []}
        payload = req.get("payload")
        raw_env = req.get("env")
        env = {k: v for k, v in (raw_env if isinstance(raw_env, dict) else {}).items()
               if isinstance(k, str) and isinstance(v, str)}
        binding = self.cfg.bindings.get((cli, native))
        role = binding.get("role") if binding else None
        iid, identity_kind = invocation_identity(req)
        base = {"v": 1, "invocation_id": iid, "stdout": "", "exit_code": 0, "handled": []}
        fresh = await self.journal("claim", {
            "invocation_id": iid, "cli": cli, "native": native, "role": role,
            "event_type": binding.get("event_type") if binding else None,
            "session_id": native_session_id(req) or None,
            "identity_kind": identity_kind, "received_at": now_iso(),
        })
        if not fresh:
            # In-memory replies may contain context; the durable receipt does
            # not. Never rerun a side effect merely to reconstruct old output.
            future = self.pending.get(iid)
            if future is not None:
                try:
                    reply = await asyncio.wait_for(asyncio.shield(future), min(MAX_SYNC_BUDGET, max(float(req.get("budget_s", SYNC_BUDGET)), 0.1)))
                except asyncio.TimeoutError:
                    reply = base
            else:
                reply = self.replies.get(iid, base)
            return {**reply, "deduplicated": True}

        future = asyncio.get_running_loop().create_future()
        self.pending[iid] = future
        req = {**req, "cli": cli, "native": native, "env": env, "invocation_id": iid}
        try:
            transition = ""
            if native == "transition" and isinstance(payload, dict) and payload.get("to"):
                transition = f"{payload.get('from', '')}->{payload['to']}"
            selections = self.cfg.selections(role, native, payload, env, transition, cli)
            publisher = self.publisher(cli, native, binding)
            if publisher is not None:
                selections.insert(0, (publisher, None))
            for h, reason in selections:
                await self.journal("select", iid, h.id, h.mode, reason=reason)
            selected = [h for h, reason in selections if reason is None]
            stdin_bytes = json.dumps(payload if payload is not None else {}).encode()
            for h in selected:
                if h.mode == "async":
                    self.spawn_async(h, req, role, stdin_bytes)
            chunks: list[str] = []
            stderr: list[str] = []
            exit_code = 0
            requested_budget = req.get("budget_s", SYNC_BUDGET)
            budget = min(float(requested_budget), MAX_SYNC_BUDGET) if isinstance(requested_budget, (int, float)) else SYNC_BUDGET
            deadline = time.monotonic() + max(budget, 0)
            for h in selected:
                if h.mode != "sync":
                    continue
                result = await self.execute(h, req, role, stdin_bytes, deadline - time.monotonic())
                if result.stdout.strip():
                    chunks.append(result.stdout)
                if result.exit_code == 2:
                    exit_code = 2
                    if result.stderr:
                        stderr.append(result.stderr)
            await self.journal("finish", iid)
            reply = {**base, "stdout": compose_stdout(chunks), "exit_code": exit_code,
                     "handled": [h.id for h in selected], "deduplicated": False}
            if stderr:
                reply["stderr"] = "\n".join(stderr)
            self.replies[iid] = reply
            if len(self.replies) > 512:
                self.replies.popitem(last=False)
            future.set_result(reply)
            return reply
        finally:
            self.pending.pop(iid, None)
            if not future.done():
                future.set_result(base)

    async def status(self) -> dict[str, Any]:
        self.cfg.maybe_reload()
        summary = await self.journal("summary")
        observation_delivery = {**await self.journal("observation_status"), "enabled": OBSERVATIONS_ENABLED}
        inventory = await self.installed_inventory()
        socket_present = self.socket_path.is_socket() if self.socket_path else None
        transport_error = "socket_path_missing" if socket_present is False else None
        deployed = {(cli["cli"], native["native"]): native
                    for cli in inventory.get("clis", [])
                    for native in cli.get("natives", [])}
        activity = {(row["cli"], row["native"]): row for row in summary["native_activity"]}
        bindings = []
        for (cli, native), binding in sorted(self.cfg.bindings.items()):
            observed = activity.get((cli, native))
            # Native installation health is a separate deployment check. A
            # configured route with no observations is unobserved, never broken.
            state = "unobserved"
            if observed:
                age = time.time() - datetime.fromisoformat(observed["last_received_at"].replace("Z", "+00:00")).timestamp()
                state = "idle" if age > 3600 else "active"
            installation = deployed.get((cli, native))
            observed_state = state
            supported = binding.get("support_status", "supported") == "supported"
            if installation and installation.get("status") == "missing":
                state = "missing"
            elif installation and installation.get("status") in {"duplicate", "drift"}:
                state = "failed"
            handler_ids = [h.id for h in self.cfg.handlers
                           if h.enabled and (not h.clis or cli in h.clis)
                           and (binding.get("role") in h.on or native in h.on_native)]
            if self.publisher(cli, native, binding):
                handler_ids.insert(0, "bloodbank-publisher")
            if not supported:
                state = "unsupported"
                handler_ids = []
            bindings.append({"cli": cli, "native": native, "role": binding.get("role"),
                             "support_status": binding.get("support_status", "supported"),
                             "event_type": binding.get("event_type"), "state": state,
                             "observed_state": observed_state,
                             "configured": supported, "activity": observed,
                             "installation": installation,
                             "handler_ids": handler_ids})
        handlers = [{"id": h.id, "mode": h.mode, "on": sorted(h.on),
                     "on_native": sorted(h.on_native), "clis": sorted(h.clis),
                     "enabled": h.enabled, "timeout_ms": h.timeout_ms, "order": h.order,
                     "after": sorted(h.after),
                     "require_env": h.require_env,
                     "match_tool": h.match_tool.pattern if h.match_tool else None,
                     "state": "configured" if h.enabled else "disabled"}
                    for h in self.cfg.handlers]
        if PUBLISH_ENABLED:
            handlers.insert(0, {"id": "bloodbank-publisher", "mode": "async",
                               "on": sorted({b.get("role") for b in self.cfg.bindings.values() if b.get("role") and b.get("event_type")}),
                               "on_native": [], "clis": [], "enabled": True,
                               "timeout_ms": 10000, "order": 0, "require_env": [],
                               "match_tool": None, "state": "configured"})
        return {"schema_version": SCHEMA_VERSION, "generated_at": now_iso(),
                "hub": {"state": "failed" if self.cfg.error or self.journal_error or transport_error else "draining" if getattr(self, "draining", False) else "running",
                        "started_at": self.started_at, "pid": os.getpid(),
                        "registry_error": self.cfg.error, "journal_error": self.journal_error,
                        "transport_error": transport_error,
                        "socket": {"path": str(self.socket_path) if self.socket_path else None,
                                   "present": socket_present, "activated": self.socket_activated},
                        "publish_enabled": PUBLISH_ENABLED, "async_running": len(self.background),
                        "observation_delivery": observation_delivery},
                "bindings": bindings, "handlers": handlers, "installed_inventory": inventory, **summary}

    async def observe(self) -> None:
        """Publish inventory changes and compact health, never native payloads.

        Send a full inventory before backfill starts. Hourly snapshots ensure a
        new collector can bootstrap even after broker retention expires the
        initial startup/configuration event.
        """
        fingerprint = None
        last_full = 0.0
        worker = None
        try:
            while True:
                try:
                    snapshot = await self.status()
                    current = configuration_fingerprint(snapshot)
                    full = current != fingerprint or time.monotonic() - last_full >= 3600
                    await self.journal("observe_snapshot", snapshot, heartbeat=not full)
                    fingerprint = current
                    if full:
                        last_full = time.monotonic()
                except Exception as exc:
                    log(f"hook snapshot observation retry: {type(exc).__name__}")
                if worker is None:
                    worker = asyncio.create_task(self.observations.run())
                await asyncio.sleep(OBSERVATION_INTERVAL)
        finally:
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def installed_inventory(self) -> dict:
        async with self.inventory_lock:
            if self.inventory is not None and time.monotonic() - self.inventory_at < 30:
                return self.inventory

            def collect() -> dict:
                path = AGENT_HOOKS_DIR / "health" / "installed_inventory.py"
                if not path.is_file():
                    return {"generated_at": now_iso(), "status": "unavailable", "reason": "inventory_not_installed", "clis": []}
                name = "hook_hub_installed_inventory"
                spec = importlib.util.spec_from_file_location(name, path)
                if spec is None or spec.loader is None:
                    raise ImportError("inventory_loader_unavailable")
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                sync_module = sys.modules.get("sync")
                if sync_module is not None and Path(getattr(sync_module, "__file__", "")).resolve() == (AGENT_HOOKS_DIR / "sync.py").resolve():
                    importlib.reload(sync_module)
                spec.loader.exec_module(module)
                return module.collect_installed_inventory()

            try:
                self.inventory = await asyncio.to_thread(collect)
            except Exception as exc:
                self.inventory = {"generated_at": now_iso(), "status": "unavailable", "reason": type(exc).__name__, "clis": []}
            self.inventory_at = time.monotonic()
            return self.inventory

    async def http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self.connection(self.http_request, reader, writer)

    async def connection(self, callback, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            await callback(reader, writer)
        finally:
            try:
                if task.cancelling():
                    writer.transport.abort()
                else:
                    writer.close()
                    await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
            finally:
                self.connections.discard(task)

    async def http_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        code, body = 200, {}
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            if len(headers) > 16384:
                raise ValueError("headers too large")
            method, target, _ = headers.split(b"\r\n", 1)[0].decode("ascii").split(" ", 2)
            path = urlsplit(target)
            if method != "GET":
                code, body = 405, {"error": "method_not_allowed"}
            elif path.path in {"/health", "/v1/hooks/status"}:
                body = await self.status()
            elif path.path == "/v1/hooks/invocations":
                query = parse_qs(path.query)
                args = {name: query[name][0] for name in ("cli", "native", "handler", "status", "limit", "offset") if name in query}
                body = await self.journal("history", **args)
            elif path.path.startswith("/v1/hooks/invocations/"):
                detail = await self.journal("detail", unquote(path.path.rsplit("/", 1)[1]))
                if detail is None:
                    code, body = 404, {"error": "invocation_not_found"}
                else:
                    body = {"invocation": detail}
            else:
                code, body = 404, {"error": "not_found"}
        except (ValueError, UnicodeDecodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            code, body = 400, {"error": "invalid_request"}
        except asyncio.TimeoutError:
            code, body = 408, {"error": "request_timeout"}
        except Exception as exc:
            code, body = 503, {"error": "journal_unavailable", "reason": type(exc).__name__}
        body = {"schema_version": SCHEMA_VERSION, "generated_at": now_iso(), **body}
        raw = json.dumps(body, separators=(",", ":")).encode()
        try:
            writer.write(f"HTTP/1.1 {code} Response\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
            await writer.drain()
        except (OSError, ConnectionError):
            pass

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        await self.connection(self.handle_request, reader, writer)

    async def handle_request(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.read(MAX_REQUEST_BYTES + 1), timeout=SYNC_BUDGET + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("request too large")
            req = json.loads(raw)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
            reply = await self.dispatch(req)
        except Exception as exc:
            log(f"request failed: {type(exc).__name__}")
            reply = {"v": 1, "stdout": "", "exit_code": 0, "handled": []}
        try:
            writer.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
        except (OSError, ConnectionError):
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
        # The socket unit owns the pathname across daemon restarts. Python 3.13
        # otherwise unlinks it when this inherited server closes, leaving the
        # next service with a listening fd that no native CLI can reach.
        kwargs = {"cleanup_socket": False} if sys.version_info >= (3, 13) else {}
        server_obj.socket_path = Path(os.fsdecode(sock.getsockname()))
        server_obj.socket_activated = True
        server = await asyncio.start_unix_server(server_obj.handle, sock=sock, **kwargs)
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
        server_obj.socket_path = Path(path)
        os.chmod(path, 0o600)
        log(f"listening on {path}")

    http_server = None
    if HTTP_PORT:
        try:
            http_server = await asyncio.start_server(server_obj.http, HTTP_HOST, HTTP_PORT, limit=16384)
            log(f"receipt API listening on {HTTP_HOST}:{HTTP_PORT}")
        except OSError as exc:
            log(f"receipt API unavailable: {type(exc).__name__}")

    async def maintenance() -> None:
        while True:
            await server_obj.journal("prune")
            await asyncio.sleep(300)

    maintenance_task = asyncio.create_task(maintenance())
    observation_task = asyncio.create_task(server_obj.observe()) if OBSERVATIONS_ENABLED else None
    await stop
    server.close()
    if http_server is not None:
        http_server.close()
    maintenance_task.cancel()
    await server_obj.drain()
    if observation_task is not None:
        # Accepted handler completions are already in the durable outbox.
        # Shutdown does not wait for an unavailable broker; restart retries.
        observation_task.cancel()
        await asyncio.gather(observation_task, return_exceptions=True)
    await server.wait_closed()
    if http_server is not None:
        await http_server.wait_closed()
    await asyncio.gather(maintenance_task, return_exceptions=True)
    log("shutting down")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
