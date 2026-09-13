"""The socket unit, not a restarted Python service, owns native ingress."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

from test_hub import CLIENT, HUB
sys.path.insert(0, str(HUB.parent))
from hub import Server
from receipts import ReceiptStore

PYTHON_313 = sys.executable if sys.version_info >= (3, 13) else shutil.which("python3.13")


def activated_process(tmp_path, owned, extra=None):
    launcher = tmp_path / "activated.py"
    launcher.write_text("import os,sys\nfd=int(sys.argv[1])\nos.dup2(fd,3)\nos.set_inheritable(3,True)\nos.environ['LISTEN_PID']=str(os.getpid())\nos.environ['LISTEN_FDS']='1'\nos.execv(sys.executable,[sys.executable,sys.argv[2]])\n")
    env = {**os.environ, "HOOK_HUB_REGISTRY": str(tmp_path / "handlers.toml"),
           "HOOK_HUB_LOG": str(tmp_path / "hub.log"),
           "HOOK_HUB_RECEIPTS": str(tmp_path / "receipts.sqlite3"),
           "HOOK_HUB_HTTP_PORT": "0", "HOOK_HUB_PUBLISH": "false", **(extra or {})}
    return subprocess.Popen([PYTHON_313, str(launcher), str(owned.fileno()), str(HUB)],
                            pass_fds=(owned.fileno(),), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


@pytest.mark.skipif(not PYTHON_313, reason="Python 3.13 added automatic Unix socket cleanup")
def test_inherited_socket_survives_two_service_restarts(tmp_path):
    path = tmp_path / "activated.sock"
    registry = tmp_path / "handlers.toml"
    registry.write_text("")
    launcher = tmp_path / "activated.py"
    launcher.write_text("import os,sys\nfd=int(sys.argv[1])\nos.dup2(fd,3)\nos.set_inheritable(3,True)\nos.environ['LISTEN_PID']=str(os.getpid())\nos.environ['LISTEN_FDS']='1'\nos.execv(sys.executable,[sys.executable,sys.argv[2]])\n")
    env = {**os.environ, "HOOK_HUB_REGISTRY": str(registry),
           "HOOK_HUB_LOG": str(tmp_path / "hub.log"),
           "HOOK_HUB_RECEIPTS": str(tmp_path / "receipts.sqlite3"),
           "HOOK_HUB_HTTP_PORT": "0", "HOOK_HUB_PUBLISH": "false"}
    with socket.socket(socket.AF_UNIX) as owned:
        owned.bind(str(path))
        owned.listen(8)
        for _ in range(2):
            proc = subprocess.Popen([PYTHON_313, str(launcher), str(owned.fileno()), str(HUB)],
                                    pass_fds=(owned.fileno(),), env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(5)
                    client.connect(str(path))
                    client.sendall(b'{}')
                    client.shutdown(socket.SHUT_WR)
                    reply = json.loads(client.recv(1024))
                    assert reply["exit_code"] == 0
            finally:
                proc.terminate()
                proc.wait(timeout=5)
            assert proc.returncode == 0, proc.stderr.read().decode(errors="replace")
            assert path.is_socket(), "closing the service unlinked systemd's socket"


def test_missing_native_socket_degrades_status_even_when_receipts_work(tmp_path):
    server = Server.__new__(Server)
    class Config:
        error = None
        bindings = {}
        handlers = []
        def maybe_reload(self):
            pass
    server.cfg = Config()
    server.socket_path = tmp_path / "missing.sock"
    server.socket_activated = True
    server.started_at = "2026-09-13T00:00:00Z"
    server.journal_error = None
    server.background = set()
    async def journal(_):
        return {"native_activity": []}
    async def inventory():
        return {"clis": []}
    server.journal = journal
    server.installed_inventory = inventory
    status = asyncio.run(server.status())
    assert status["hub"]["state"] == "failed"
    assert status["hub"]["transport_error"] == "socket_path_missing"
    assert status["hub"]["socket"]["present"] is False


@pytest.mark.skipif(not PYTHON_313, reason="exercise the installed Python socket-activation runtime")
def test_restart_drains_publication_then_serves_backlog_once_with_same_socket(tmp_path):
    path = tmp_path / "activated.sock"
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(10)\n")
    registry = ""
    for name in ("long-running", "long-queued"):
        registry += f'[[handler]]\nid="{name}"\nmode="async"\non=["post_tool"]\ncommand=["{sys.executable}","{slow}"]\ntimeout_ms=15000\n'
    (tmp_path / "handlers.toml").write_text(registry)
    captured = []
    received, acknowledge = threading.Event(), threading.Event()
    with socket.socket() as broker_socket, socket.socket(socket.AF_UNIX) as owned:
        broker_socket.bind(("127.0.0.1", 0))
        broker_socket.listen(2)
        broker_socket.settimeout(5)
        owned.bind(str(path))
        owned.listen(8)
        inode = path.stat().st_ino

        def broker():
            conn, _ = broker_socket.accept()
            with conn:
                conn.settimeout(5)
                conn.sendall(b'INFO {"server_id":"shutdown-test"}\r\n')
                frame = bytearray()
                while b"PING\r\n" not in frame:
                    data = conn.recv(65536)
                    if not data:
                        return
                    frame.extend(data)
                wire = bytes(frame)
                header = wire.index(b"PUB ")
                start = wire.index(b"\r\n", header) + 2
                size = int(wire[header:start].split()[-1])
                captured.append(json.loads(wire[start:start + size]))
                received.set()
                if acknowledge.wait(4):
                    conn.sendall(b"PONG\r\n")

        thread = threading.Thread(target=broker, daemon=True)
        thread.start()
        env = {"HOOK_HUB_PUBLISH": "true", "HOOK_HUB_ASYNC_SLOTS": "1",
               "BLOODBANK_ENABLED": "true", "BLOODBANK_ASM": "false",
               "BLOODBANK_NATS_HOST": "127.0.0.1", "BLOODBANK_NATS_PORT": str(broker_socket.getsockname()[1]),
               "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
        payload = json.dumps({"session_id": "restart-session", "tool_use_id": "accepted-once", "tool_name": "Bash",
                              "tool_input": {}, "tool_response": {"stdout": "fixture", "exit_code": 0}}).encode()
        client_env = {**os.environ, "BB_HOOK_SOCKET": str(path)}
        client_env.pop("BB_HOOK_HUB", None)
        command = [str(CLIENT), "--cli", "codex", "--native", "PostToolUse"]
        first = activated_process(tmp_path, owned, env)
        second = None
        queued = None
        try:
            reply = subprocess.run(command, input=payload, capture_output=True, env=client_env, timeout=4)
            assert reply.returncode == 0
            assert received.wait(3), "publisher was starved behind long behavioral work"
            first.terminate()
            queued_at = time.monotonic()
            queued = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=client_env)
            queued.stdin.write(payload)
            queued.stdin.close()
            time.sleep(.15)
            assert first.poll() is None, "accepted publication was canceled immediately on SIGTERM"
            assert queued.poll() is None, "the socket backlog should await the replacement service"
            acknowledge.set()
            first.wait(timeout=4)
            assert first.returncode == 0, first.stderr.read().decode(errors="replace")
            assert path.stat().st_ino == inode
            second = activated_process(tmp_path, owned, env)
            queued.wait(timeout=3)
            assert queued.returncode == 0
            assert time.monotonic() - queued_at < 2.9
            store = ReceiptStore(tmp_path / "receipts.sqlite3")
            rows = store.history()["items"]
            assert len(rows) == 1 and rows[0]["deduplicated"] == 1
            executions = {r["handler_id"]: r for r in rows[0]["executions"]}
            assert executions["bloodbank-publisher"]["status"] == "succeeded"
            assert executions["bloodbank-publisher"]["publish_status"] == "sent"
            assert executions["bloodbank-publisher"]["event_id"] == captured[0]["id"]
            for name in ("long-running", "long-queued"):
                assert executions[name]["status"] == "interrupted"
                assert executions[name]["reason"] == "shutdown_grace_expired"
            assert rows[0]["status"] == "interrupted"
            assert store.summary()["native_activity"][0]["interrupted"] == 1
            assert len(captured) == 1
        finally:
            acknowledge.set()
            for process in (first, second, queued):
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            thread.join(timeout=5)
        assert path.stat().st_ino == inode


def test_shutdown_interruption_cannot_replace_a_completed_publish_receipt(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    store.claim({"invocation_id": "accepted", "cli": "codex", "native": "PostToolUse", "role": "post_tool",
                 "event_type": "bloodbank.agent.tool.completed", "session_id": "s", "identity_kind": "tool_call",
                 "received_at": "2026-09-13T00:00:00Z"})
    store.select("accepted", "bloodbank-publisher", "async")
    store.update("accepted", "bloodbank-publisher", "succeeded", event_id="event-id", publish_status="sent")
    store.interrupt("accepted", "bloodbank-publisher")
    store.interrupt_pending()
    receipt = store.detail("accepted")
    assert receipt["status"] == "succeeded"
    assert receipt["executions"][0]["event_id"] == "event-id"
    assert receipt["executions"][0]["publish_status"] == "sent"
    assert all(row["status"] != "interrupted" for row in receipt["timeline"])
