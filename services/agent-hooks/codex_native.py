"""Codex's installed hook loader and trust writer, without opening a model turn."""
from __future__ import annotations

import json
import os
import select
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any
from cli_paths import find_cli_binary


class CodexAppServer:
    def __init__(self, *, binary: str | None = None, config_home: Path | None = None):
        executable = binary or find_cli_binary("codex")
        if not executable:
            raise FileNotFoundError("codex executable unavailable for native hook verification")
        child_env = dict(os.environ)
        if config_home is not None:
            # This is Codex's documented config selector, not a task scratch
            # variable; the caller targets a discovered native runtime home.
            child_env["CODEX_HOME"] = str(config_home)
        self.process = subprocess.Popen([executable, "app-server", "--stdio"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, cwd=Path.home(), env=child_env)
        self.counter = 0
        self.buffer = b""

    def __enter__(self):
        try:
            self.rpc("initialize", {"clientInfo": {"name": "bloodbank-hooks-sync", "version": "2"},
                                    "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
            return self
        except Exception:
            self.__exit__()
            raise

    def __exit__(self, *_):
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def rpc(self, method: str, params: dict, *, timeout: float = 8) -> Any:
        self.counter += 1
        request = self.counter
        self.send({"id": request, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                message = json.loads(line)
                if message.get("id") != request:
                    continue
                if "error" in message:
                    self.last_error = message["error"]
                    # Do not leak config values echoed by a native error.
                    raise RuntimeError(f"Codex {method} failed (code {message['error'].get('code')})")
                return message.get("result")
            readable, _, _ = select.select([self.process.stdout], [], [], max(0, deadline - time.monotonic()))
            if readable:
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    raise RuntimeError(f"Codex exited during {method}")
                self.buffer += data
        raise TimeoutError(f"Codex {method} exceeded {timeout}s")

    def hooks(self) -> list[dict]:
        result = self.rpc("hooks/list", {"cwds": [str(Path.home())]})
        groups = result.get("data", [])
        if any(group.get("errors") for group in groups):
            raise RuntimeError("Codex native hook loader reported configuration errors")
        return [hook for group in groups for hook in group.get("hooks", [])]


def capture_trust(config_path: Path | None = None) -> dict:
    config_path = config_path or Path.home() / ".codex/config.toml"
    states = tomllib.loads(config_path.read_text()).get("hooks", {}).get("state", {}) if config_path.exists() else {}
    with CodexAppServer(config_home=config_path.parent) as native:
        hooks = native.hooks()
    return {"hooks": hooks, "states": states}


def trust_edits(before: dict, after: list[dict], source_path: Path) -> list[dict]:
    """Carry unchanged foreign trust by hash; approve only this hub's new hooks.

    Native keys include group/inner indexes. Removing one managed command moves
    a foreign sibling's key, although its code is byte-identical. Copying the
    prior state by native currentHash preserves that effective trust decision.
    """
    source = str(source_path)
    previous: dict[tuple, list[dict]] = {}
    for hook in before["hooks"]:
        signature = (hook["sourcePath"], hook["eventName"], hook["currentHash"])
        previous.setdefault(signature, []).append(hook)
    edits = []
    for hook in after:
        if hook.get("sourcePath") != source:
            continue
        command = hook.get("command", "")
        managed = "/bb-hook --cli codex --native " in command
        if managed:
            value = {"trusted_hash": hook["currentHash"], "enabled": True}
        else:
            old = previous.get((source, hook["eventName"], hook["currentHash"]), [])
            if not old:
                continue  # a new foreign hook does not get our approval
            prior = next((h for h in old if h["key"] == hook["key"]), old[0])
            value = dict(before["states"].get(prior["key"], {}))
        current = before["states"].get(hook["key"], {})
        if current != value:
            edits.append({"keyPath": f"hooks.state.{json.dumps(hook['key'])}",
                          "mergeStrategy": "replace", "value": value})
    return edits


def reconcile_trust(before: dict, source_path: Path) -> int:
    with CodexAppServer(config_home=source_path.parent) as native:
        edits = trust_edits(before, native.hooks(), source_path)
        if edits:
            native.rpc("config/batchWrite", {"edits": edits, "reloadUserConfig": True})
    # A fresh loader proves persisted trust, not a cache in the writer process.
    with CodexAppServer(config_home=source_path.parent) as native:
        managed = [h for h in native.hooks() if h.get("sourcePath") == str(source_path)
                   and "/bb-hook --cli codex --native " in h.get("command", "")]
    if not managed or any(not h["enabled"] or h["trustStatus"] != "trusted" for h in managed):
        raise RuntimeError("Codex managed hooks remain disabled or untrusted after install")
    return len(edits)
