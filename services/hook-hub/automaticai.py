#!/usr/bin/env python3
"""Codex routing checks and completed-usage proof through the canonical hub."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlsplit

from concerns import context_output, normalized, result

HOME = Path.home()
STATE_ROOT = HOME / ".local/state/automaticai/session-proof"
PROOF_CLI = HOME / "docker/stacks/ai/newapi/ops/gateway-proof.py"
SKILL = HOME / "code/skillex/all-skills/automaticai-provider-gateway-lazy-migration-strategy/SKILL.md"
OWNER = HOME / ".agents/providers/automaticai"
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,120}\Z")
MARKER = re.compile(r"AAI_ROUTE_PROOF_[0-9a-f]{32}\Z")


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, state: dict) -> None:
    # No prompts or resolved credentials belong in the runtime record.
    descriptor, temporary = tempfile.mkstemp(prefix=".proof-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(state, stream, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_text(value) -> str:
    return value if isinstance(value, str) and len(value) < 200 and not any(ord(c) < 32 for c in value) else ""


def gateway_origin(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "api.automaticai.io"
                and parsed.port in {None, 443} and not parsed.username and not parsed.password
                and not parsed.query and parsed.path.rstrip("/") == "/v1")
    except ValueError:
        return False


def codex_arguments(parent_pid: str) -> list[str]:
    """Inspect only the actual caller ancestry, never another Codex process."""
    try:
        pid = int(parent_pid)
    except ValueError:
        return []
    for _ in range(8):
        if pid <= 1:
            break
        try:
            arguments = [part.decode("utf-8", errors="replace") for part in
                         (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0") if part]
            if arguments and Path(arguments[0]).name == "codex":
                return arguments[1:]
            status = (Path("/proc") / str(pid) / "status").read_text()
            pid = int(re.search(r"(?m)^PPid:\s*(\d+)", status).group(1))
        except (OSError, ValueError, AttributeError):
            break
    return []


def merge_config(target: dict, value: dict) -> None:
    for key, child in value.items():
        if isinstance(child, dict) and isinstance(target.get(key), dict):
            merge_config(target[key], child)
        else:
            target[key] = child


def routing_config(codex_home: Path, arguments: list[str]) -> dict:
    def load(path):
        try:
            return tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return {}
    config = load(codex_home / "config.toml")
    profile = ""
    overrides = []
    for i, arg in enumerate(arguments):
        if arg in {"-p", "--profile"} and i + 1 < len(arguments):
            profile = arguments[i + 1]
        elif arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]
        if arg in {"-c", "--config"} and i + 1 < len(arguments):
            overrides.append(arguments[i + 1])
        elif arg.startswith("--config="):
            overrides.append(arg.split("=", 1)[1])
        elif arg.startswith("-c") and not arg.startswith("--") and len(arg) > 2:
            overrides.append(arg[2:])
    if IDENTIFIER.fullmatch(profile):
        merge_config(config, load(codex_home / (profile + ".config.toml")))
    for override in overrides:
        try:
            merge_config(config, tomllib.loads(override))
        except tomllib.TOMLDecodeError:
            pass
    for i, arg in enumerate(arguments):
        if arg in {"-m", "--model"} and i + 1 < len(arguments):
            config["model"] = arguments[i + 1]
        elif arg.startswith("--model="):
            config["model"] = arg.split("=", 1)[1]
    return config


def session_metadata(data: dict, codex_home: Path) -> dict:
    session = data["session_id"]
    explicit = data.get("transcript_path")
    candidates = [Path(explicit)] if explicit else []
    if not candidates:
        candidates = list((codex_home / "sessions").glob(f"*/*/*/*{session}*.jsonl"))[:2]
    for path in candidates:
        try:
            # The first record is small even when the full conversation is large.
            with path.open() as stream:
                row = json.loads(stream.readline(128 * 1024))
            meta = row.get("payload", {})
            if row.get("type") == "session_meta" and meta.get("id") == session:
                # A changed next-launch config must not relabel this open
                # thread's model. Read only a bounded tail, not its full history.
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 2 * 1024 * 1024))
                    lines = stream.read().splitlines()
                for line in reversed(lines):
                    if b'"type":"turn_context"' not in line and b'"type": "turn_context"' not in line:
                        continue
                    try:
                        context = json.loads(line)
                    except ValueError:
                        continue
                    if context.get("type") == "turn_context":
                        meta["model"] = safe_text(context.get("payload", {}).get("model"))
                        break
                return meta
        except (OSError, ValueError, AttributeError):
            continue
    return {}


def audit(data: dict) -> dict:
    codex_home = Path(os.environ.get("CODEX_HOME", str(HOME / ".codex")))
    arguments = codex_arguments(os.environ.get("BB_HOOK_PARENT_PID", ""))
    config = routing_config(codex_home, arguments)
    meta = session_metadata(data, codex_home)
    provider = safe_text(meta.get("model_provider")) or safe_text(data.get("model_provider")) or safe_text(config.get("model_provider")) or "openai"
    model = safe_text(data.get("model")) or safe_text(meta.get("model")) or safe_text(config.get("model"))
    selected = config.get("model_providers", {}).get(provider, {})
    base = selected.get("base_url", "") if isinstance(selected, dict) else ""
    gateway = gateway_origin(base) and model.startswith(("automaticai/", "aai/"))
    authority = "session_record" if meta.get("model_provider") else "launch_arguments" if arguments else "configuration"
    return {"provider": provider, "model": model,
            "status": "gateway_configured" if gateway else "bypass",
            "authority": authority, "source_owner": str(OWNER)}


def migration_context(snapshot: dict) -> str:
    return (
        f"AutomaticAI routing: {snapshot['authority']} selects {snapshot['provider']} / {snapshot['model'] or 'unknown model'} outside the gateway. "
        "Apply $automaticai-provider-gateway-lazy-migration-strategy now. "
        f"Read {SKILL}. Immediately fork a remediation agent for the canonical Codex owner {OWNER}; "
        "give explicit file ownership, preserve unrelated edits, and join an existing worker for that owner instead of duplicating it. "
        "If you are already assigned this migration, resolve it inline without recursively forking. "
        "Fix the durable next-launch provider, then verify a fresh actual Codex request and its completed gateway receipt. "
        "Do not relabel this executing connection, downgrade its model, change accounts, or invent a fallback. "
        "A missing dedicated OAuth grant or exact model route remains an open blocker; complete independent preparation and record that boundary."
    )


def completed_receipt(state: dict) -> dict:
    if not PROOF_CLI.is_file():
        return {"status": "unavailable", "verified": False}
    command = [sys.executable, "-B", str(PROOF_CLI), "check", "--marker", state["marker"],
               "--since", str(state["since"]), "--session-id", state["session_id"], "--wait", "0"]
    model = state.get("routing", {}).get("model", "")
    if model.startswith(("automaticai/", "aai/")):
        command += ["--route", model]
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=1.8,
                                 stdin=subprocess.DEVNULL)
        receipt = json.loads(process.stdout)
        if receipt.get("verified") is True and (process.returncode != 0 or not receipt.get("receipts")):
            return {"status": "unavailable", "verified": False}
        return receipt if isinstance(receipt, dict) else {"status": "unavailable", "verified": False}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"status": "unavailable", "verified": False}


def dispatch(action: str, payload: dict) -> dict:
    cli, native = os.environ.get("BB_HOOK_CLI", "codex"), os.environ.get("BB_HOOK_NATIVE", "SessionStart")
    if cli != "codex":
        return result("skipped", "codex_only")
    if os.environ.get("AUTOMATICAI_PROOF_PROBE") == "1":
        return result("skipped", "separate_proof_probe")
    data = normalized(payload)
    session = data["session_id"] or os.environ.get("BB_HOOK_SESSION_ID", "")
    if not IDENTIFIER.fullmatch(session):
        return result("skipped", "native_session_identity_missing")
    data["session_id"] = session
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = STATE_ROOT / (session + ".json")
    with (STATE_ROOT / (session + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_object(path)
        snapshot = audit(data)
        fresh = action == "start" or not MARKER.fullmatch(str(state.get("marker", "")))
        if fresh:
            requested = state.get("migration_requested", False)
            state = {"session_id": session, "marker": "AAI_ROUTE_PROOF_" + secrets.token_hex(16),
                     "since": int(time.time()), "status": "pending", "routing": snapshot,
                     "migration_requested": requested}
            if data.get("prompt"):
                state["submitted_prompt_sha256"] = hashlib.sha256(data["prompt"].encode()).hexdigest()
        texts = []
        state["routing"] = snapshot
        worker = os.environ.get("AUTOMATICAI_MIGRATION_WORKER") == "1"
        if snapshot["status"] == "bypass":
            state["status"] = "bypass"
            if not state.get("migration_requested") and not worker:
                texts.append(migration_context(snapshot))
                state["migration_requested"] = True  # requested, not falsely claimed as spawned
        elif action == "verify" and state.get("status") != "verified":
            receipt = completed_receipt(state)
            state["last_check"] = int(time.time())
            state["status"] = "verified" if receipt.get("verified") is True else "pending"
            state["proof_availability"] = receipt.get("status", "unavailable")
            if state["status"] == "verified":
                state["receipts"] = receipt.get("receipts", [])
                row = state["receipts"][0]
                texts.append(f"AutomaticAI verified this prompt marker in completed gateway usage: {state['marker']}; "
                             f"route {row.get('route')}, account {row.get('account')}, log {row.get('log_id')}, "
                             f"request {row.get('request_id')}. This proves the matching request, not every request in the session.")
        if fresh:
            texts.append(f"AutomaticAI prompt proof marker: {state['marker']}. Keep this non-secret marker in context. "
                         "Startup checks configuration; completed gateway usage verifies the subsequent actual model request. "
                         "A separate test call or missing receipt does not prove this session used or bypassed the gateway.")
        write_state(path, state)
        return result("succeeded", "routing_" + state["status"], context_output("\n\n".join(texts), cli, native))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "verify", "status"])
    parser.add_argument("--session-id")
    args = parser.parse_args()
    try:
        if args.action == "status":
            if not args.session_id or not IDENTIFIER.fullmatch(args.session_id):
                parser.error("status requires a valid --session-id")
            state = read_object(STATE_ROOT / (args.session_id + ".json"))
            print(json.dumps(state or {"status": "unknown", "verified": False}))
            return 0 if state else 2
        payload = json.load(sys.stdin)
        output = dispatch(args.action, payload if isinstance(payload, dict) else {})
    except (OSError, ValueError, TypeError, AttributeError):
        output = result("failed", "routing_check_unavailable")
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
