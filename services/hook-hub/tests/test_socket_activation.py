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

import pytest

from test_hub import HUB
sys.path.insert(0, str(HUB.parent))
from hub import Server

PYTHON_313 = sys.executable if sys.version_info >= (3, 13) else shutil.which("python3.13")


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
