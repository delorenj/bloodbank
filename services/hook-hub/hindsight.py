"""Supervised Hindsight concerns: recall, edit candidates, and retention receipts.

Candidate writes are deliberately distinct from a successful memory retain.
Session close uses a deterministic document id, so a retry replaces the same
document if the process dies after the API accepted it but before our receipt.
"""
from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from concerns import context_output, file_edits, invoke, repository, result


def safe(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return cleaned[:100] or hashlib.sha256(value.encode()).hexdigest()[:20]


def session_key(payload: dict, cli: str) -> str:
    # CLI and native session identity keep simultaneous clients apart. Without
    # a session id we cannot truthfully join prompt/tool/end work.
    session = str(payload.get("session_id", ""))
    return f"{safe(cli)}-{safe(session)}" if session else ""


def journal_path(payload: dict, cli: str) -> Path:
    root = Path(os.environ.get("HS_JOURNAL_DIR", Path.home() / ".agents/journal"))
    return root / "sessions" / f"{session_key(payload, cli)}.jsonl"


def journal(payload: dict, cli: str, event: dict) -> None:
    path = journal_path(payload, cli)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now(timezone.utc).isoformat(), "cli": cli,
              "session_id": payload.get("session_id"),
              "invocation_id": os.environ.get("BB_HOOK_INVOCATION_ID", ""), **event}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(record, ensure_ascii=False) + "\n").encode())
    finally:
        os.close(descriptor)


def records(payload: dict, cli: str) -> list[dict]:
    paths = [journal_path(payload, cli)]
    # Preserve candidates produced before this session's native cutover.
    old = paths[0].parent / f"{safe(str(payload.get('session_id', '')))}.jsonl"
    if old != paths[0]:
        paths.append(old)
    found: list[dict] = []
    for path in paths:
        try:
            for line in path.read_text().splitlines():
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        found.append(item)
                except ValueError:
                    continue
        except OSError:
            continue
    return found


def bank() -> str:
    root = repository()
    if root:
        # In worktrees the canonical override belongs to the primary checkout.
        try:
            common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                    capture_output=True, text=True, timeout=1)
            if common.returncode == 0:
                root = Path(common.stdout.strip()).parent
            override = root / ".hindsight/bank"
            if override.is_file():
                for line in override.read_text().splitlines():
                    if line.strip() and not line.lstrip().startswith("#"):
                        return line.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if os.environ.get("HINDSIGHT_BANK"):
        return os.environ["HINDSIGHT_BANK"]
    try:
        remote = subprocess.run(["git", "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=1)
        if remote.returncode == 0:
            return remote.stdout.strip().removesuffix(".git").rsplit("/", 1)[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    return root.name if root else "general"


def binary() -> str | None:
    candidate = os.environ.get("HINDSIGHT_BIN") or str(Path.home() / ".local/bin/hindsight")
    return candidate if os.access(candidate, os.X_OK) else shutil.which("hindsight")


def cli_environment(cli: str) -> dict[str, str]:
    return {**os.environ, "HINDSIGHT_AGENT": cli, "NO_COLOR": "1", "FORCE_COLOR": "0",
            "TERM": "dumb", "HINDSIGHT_NO_SPINNER": "1", "HINDSIGHT_DISABLE_SPINNER": "1"}


def linked_banks(primary: str) -> list[str]:
    if os.environ.get("HINDSIGHT_FANOUT", "1") == "0":
        return []
    path = Path(os.environ.get("HINDSIGHT_DREAM_GRAPH", Path.home() / ".hindsight/dream/bank-graph.json"))
    try:
        items = json.loads(path.read_text()).get("graph", {}).get(primary, [])
        minimum = float(os.environ.get("HINDSIGHT_FANOUT_MIN_SCORE", "0.1"))
        maximum = max(0, min(4, int(os.environ.get("HINDSIGHT_FANOUT_MAX", "2"))))
        return [str(item["bank"]) for item in sorted(items, key=lambda item: float(item.get("score", 0)), reverse=True)
                if item.get("bank") and float(item.get("score", 0)) >= minimum][:maximum]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def recall_text(response: dict) -> list[str]:
    values = response.get("results", response.get("memories", response.get("items", [])))
    if not isinstance(values, list):
        return []
    return [str(item.get("text", item.get("content", ""))).strip()
            for item in values if isinstance(item, dict) and (item.get("text") or item.get("content"))]


def recall(payload: dict, cli: str, native: str) -> dict:
    prompt = payload.get("prompt", "")
    if len(prompt.strip()) < 24:
        return result("skipped", "prompt_under_min_length")
    command = binary()
    if not command:
        return result("skipped", "hindsight_binary_missing")
    primary = bank()
    banks = list(dict.fromkeys([primary, "general", *os.environ.get("HINDSIGHT_GLOBAL_BANKS", "infra").split(), *linked_banks(primary)]))[:7]
    deadline = max(0.2, min(10.0, float(os.environ.get("HINDSIGHT_RECALL_TIMEOUT", "9"))))

    def fetch(target: str) -> tuple[str, list[str], str]:
        args = [command, "memory", "recall", target, prompt, "--output", "json",
                "--budget", "mid" if target == primary else "low", "--max-tokens", "2048" if target == primary else "1024"]
        try:
            completed = subprocess.run(args, capture_output=True, text=True, timeout=deadline, env=cli_environment(cli))
            if completed.returncode:
                return target, [], "recall_command_failed"
            decoded = json.loads(completed.stdout)
            return target, recall_text(decoded) if isinstance(decoded, dict) else [], ""
        except subprocess.TimeoutExpired:
            return target, [], "recall_deadline_exceeded"
        except (OSError, ValueError):
            return target, [], "recall_response_invalid"

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(banks)) as pool:
        responses = list(pool.map(fetch, banks))
    context: list[str] = []
    failures = [error for _, _, error in responses if error]
    for target, texts, error in responses:
        if texts:
            context.append(f"<!-- hindsight:recall bank={target} -->\n" + "\n".join(texts)[:10000] + "\n<!-- /hindsight:recall -->")
    rendered = "\n\n".join(context)[:20000]
    if payload.get("session_id"):
        journal(payload, cli, {"event": "recall", "bank": primary, "banks": banks,
                              "prompt_len": len(prompt), "total_chars": len(rendered),
                              "returned_anything": bool(rendered), "failed_banks": len(failures)})
    if not rendered and failures:
        return result("failed", failures[0], exit_code=1)
    return result("succeeded", "recall_partial" if failures else "recall_completed", context_output(rendered, cli, native))


def candidate(payload: dict, cli: str) -> dict:
    if not payload.get("session_id"):
        return result("skipped", "native_session_id_missing")
    tool_response = payload.get("tool_response", payload.get("tool_result", payload.get("result", {})))
    failed_response = isinstance(tool_response, dict) and (
        tool_response.get("error") or tool_response.get("is_error") or tool_response.get("success") is False
    )
    if payload.get("error") or payload.get("is_error") or failed_response or str(payload.get("hook_event_name", "")).lower().endswith("failure"):
        return result("skipped", "tool_failed")
    edits = [(path, content) for path, content in file_edits(payload) if len(content) >= 50]
    if not edits:
        return result("skipped", "no_substantial_file_edit")
    primary = bank()
    # Each native invocation becomes one candidate row; several file paths in
    # apply_patch are retained together without losing the full edit identity.
    journal(payload, cli, {"event": "retain_candidate", "bank": primary,
                          "files": [path for path, _ in edits],
                          "edits": [{"file": path, "snippet": " ".join(content.split())[:400]} for path, content in edits],
                          "source": "auto_posttooluse"})
    return result("succeeded", "edit_candidate_recorded")


def end(payload: dict, cli: str) -> dict:
    if not payload.get("session_id"):
        return result("skipped", "native_session_id_missing")
    command = binary()
    if not command:
        return result("skipped", "hindsight_binary_missing")
    path = journal_path(payload, cli)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".retain.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        history = records(payload, cli)
        edits: list[str] = []
        for item in history:
            if item.get("event") == "retain_candidate":
                edits.extend(f"- {edit.get('file')}: {edit.get('snippet', '')}" for edit in item.get("edits", []))
            elif item.get("event") == "retain" and item.get("source") == "auto_posttooluse":
                edits.append(f"- {item.get('file')}: {item.get('snippet', '')}")
        summary = "Session completed. Files edited:\n" + "\n".join(dict.fromkeys(edits)) if edits else ""
        assistant = payload.get("last_assistant_message", "")
        if isinstance(assistant, str) and len(assistant.strip()) >= 50:
            summary += "\nSession outcome:\n" + assistant[-8000:]
        summary = summary.strip()[:20000]
        if not summary:
            return result("skipped", "no_retention_candidates")
        fingerprint = hashlib.sha256(summary.encode()).hexdigest()
        if any(item.get("event") == "retain_receipt" and item.get("retained") and item.get("fingerprint") == fingerprint for item in history):
            return result("skipped", "session_summary_already_retained")
        primary = bank()
        doc_id = "hook-session-" + hashlib.sha256(f"{cli}:{payload['session_id']}".encode()).hexdigest()[:32]
        tags = f"user:{safe(os.environ.get('HINDSIGHT_USER', os.environ.get('USER', 'unknown')))},agent:{safe(cli)},host:{safe(socket.gethostname().split('.')[0])}"
        try:
            process = subprocess.run([command, "memory", "retain", primary, summary, "--context", "session-summary",
                                      "--doc-id", doc_id, "--output", "json", "--document-tags", tags],
                                     capture_output=True, text=True, timeout=45, env=cli_environment(cli))
            response = json.loads(process.stdout) if process.returncode == 0 else {}
        except subprocess.TimeoutExpired:
            journal(payload, cli, {"event": "retain_receipt", "retained": False, "fingerprint": fingerprint, "reason": "retain_deadline_exceeded"})
            return result("failed", "retain_deadline_exceeded", exit_code=1)
        except (OSError, ValueError):
            return result("failed", "retain_response_invalid", exit_code=1)
        accepted = process.returncode == 0 and isinstance(response, dict) and bool(response) and not response.get("error") and response.get("success") is not False
        journal(payload, cli, {"event": "retain_receipt", "bank": primary, "retained": accepted,
                              "fingerprint": fingerprint, "document_id": doc_id,
                              "response_keys": sorted(response) if isinstance(response, dict) else []})
        return result("succeeded" if accepted else "failed", "session_summary_retained" if accepted else "retain_command_failed", exit_code=0 if accepted else 1)


def dispatch(concern: str, payload: dict, cli: str, native: str) -> dict:
    if os.environ.get("DISABLE_HINDSIGHT_HOOKS") == "1":
        return result("skipped", "hindsight_disabled")
    if concern == "hindsight-recall":
        return recall(payload, cli, native)
    if concern == "hindsight-retain":
        return candidate(payload, cli)
    if concern == "hindsight-session-end":
        return end(payload, cli)
    if concern == "hindsight-journal":
        if not payload.get("session_id") or not journal_path(payload, cli).exists():
            return result("skipped", "session_has_no_memory_journal")
        normalized = dict(payload, session_id=session_key(payload, cli))
        return invoke(["~/.agents/hooks/hindsight/hindsight-journal-write.sh"], normalized, timeout=5)
    if concern == "hindsight-jot-flush":
        root = Path(os.environ.get("HS_JOURNAL_DIR", Path.home() / ".agents/journal"))
        key = hashlib.sha1(str(payload.get("cwd", os.getcwd())).encode()).hexdigest()[:12]
        jotfile = root / "jots" / f"{key}.md"
        jotfile.parent.mkdir(parents=True, exist_ok=True)
        with jotfile.with_suffix(".flush.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not jotfile.is_file() or not jotfile.stat().st_size:
                return result("skipped", "no_pending_jots")
            (jotfile.parent / "flushed").mkdir(exist_ok=True)
            output = invoke(["~/.agents/hooks/hindsight/hindsight-jot-flush.sh", "--jotfile", str(jotfile)], payload,
                            timeout=180, environment=cli_environment(cli))
            if output["_hook_hub"]["status"] == "succeeded" and jotfile.exists():
                return result("skipped", "jots_remain_pending")
            return output
    return result("failed", "unknown_hindsight_concern", exit_code=1)
