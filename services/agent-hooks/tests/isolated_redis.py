"""Owned Redis processes for ASM tests; never consult ambient Redis settings."""
from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from core.resp import Connection


class DisposableRedis:
    # Used by the suite's network guard, including during readiness checks.
    active: dict[int, subprocess.Popen] = {}

    def __init__(self):
        self.prefix = f"bb32:{uuid.uuid4().hex}:"
        self.url: str | None = None
        self.port: int | None = None
        self.process: subprocess.Popen | None = None
        self.directory: tempfile.TemporaryDirectory | None = None

    def start(self, *, readiness_timeout: float = 5.0) -> "DisposableRedis":
        executable = shutil.which("redis-server")
        if executable is None:
            raise RuntimeError("ASM tests require redis-server on PATH (no external fallback)")
        try:
            self.directory = tempfile.TemporaryDirectory(prefix="bb32-redis-")
            root = Path(self.directory.name)
            # The kernel chooses the port; 6379 is never a valid test endpoint.
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                self.port = reservation.getsockname()[1]
            if self.port == 6379:
                raise RuntimeError("refusing production Redis port")
            self.url = f"redis://127.0.0.1:{self.port}/0"
            log = root / "redis.log"
            self.process = subprocess.Popen(
                [executable, "--bind", "127.0.0.1", "--port", str(self.port),
                 "--protected-mode", "yes", "--daemonize", "no",
                 "--save", "", "--appendonly", "no", "--dir", str(root),
                 "--logfile", str(log)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.active[self.port] = self.process
            deadline = time.monotonic() + readiness_timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"disposable Redis exited: {log.read_text() if log.exists() else ''}")
                # Do not probe until OUR child has bound its listening socket.
                if log.exists() and "Ready to accept connections" in log.read_text():
                    with Connection(self.url, timeout=0.25) as conn:
                        info = conn.command("INFO", "server")
                        if f"process_id:{self.process.pid}\r\n" not in info:
                            raise RuntimeError("readiness endpoint is not the owned Redis process")
                        if conn.command("PING") == "PONG":
                            return self
                time.sleep(0.01)
            raise RuntimeError("disposable Redis readiness timed out (no external fallback)")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        finally:
            if self.port is not None:
                self.active.pop(self.port, None)
            if self.directory is not None:
                self.directory.cleanup()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
