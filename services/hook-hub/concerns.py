#!/usr/bin/env python3
"""Portable behavioral handlers for hook-hub, with explicit execution outcomes.

The hub supplies raw native JSON on stdin and CLI/native/role identity in the
environment. Native adapters only dispatch; this module owns behavioral work.
Its structured stdout is consumed by the hub, never exposed as model context.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SERVICE_DIR = Path(__file__).resolve().parent
WRITE_TOOLS = re.compile(r"^(Write|Edit|MultiEdit|apply_patch|write_file|edit_file|replace|replace_in_file|insert_code_at|write|edit|patch|str_replace_editor|write_to_file|replace_file_content|multi_replace_file_content)$", re.I)
ALIASES = {"skill-reminder": "skill-check-reminder", "merge-forward": "merge-forward-session-rebalance"}


def result(status: str, reason: str, stdout: str = "", exit_code: int = 0) -> dict:
    return {"_hook_hub": {"status": status, "reason": reason, "exit_code": exit_code}, "stdout": stdout}


def value(payload: dict, *names: str, default: Any = "") -> Any:
    for name in names:
        part: Any = payload
        for key in name.split("."):
            part = part.get(key) if isinstance(part, dict) else None
        if part is not None and part != "":
            return part
    return default


def normalized(payload: dict) -> dict:
    """Preserve native fields while making the behavioral input portable."""
    data = dict(payload)
    args = value(payload, "tool_input", "toolInput", "toolArgs", "arguments", "args", "toolCall.args", "tool_call.arguments", default={})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"patch": args} if "*** Begin Patch" in args else {"command": args}
    data["tool_input"] = args if isinstance(args, dict) else {}
    data["tool_name"] = value(payload, "tool_name", "toolName", "tool", "toolCall.name", "tool_call.name")
    data["prompt"] = value(payload, "prompt", "message", "user_message", "userMessage", "input", default="")
    if not isinstance(data["prompt"], str):
        data["prompt"] = ""
    session = value(payload, "session_id", "sessionId", "session.id", "thread_id", "threadId", "conversationId")
    data["session_id"] = str(session) if session else ""
    data["cwd"] = value(payload, "cwd", "workingDirectory", "working_directory", "workspace_dir", default=os.getcwd())
    if payload.get("workspacePaths") and isinstance(payload["workspacePaths"], list):
        data["cwd"] = payload["workspacePaths"][0]
    data["transcript_path"] = value(payload, "transcript_path", "transcriptPath", default="")
    # Gemini CLI's AfterAgent names the final answer `prompt_response`.
    data["last_assistant_message"] = value(payload, "last_assistant_message", "assistant_response", "response",
                                           "prompt_response", default="")
    data["hook_event_name"] = os.environ.get("BB_HOOK_NATIVE", str(payload.get("hook_event_name", "")))
    return data


def context_output(text: str, cli: str, native: str) -> str:
    if not text.strip():
        return ""
    if cli in {"claude", "codex", "gemini"}:
        return json.dumps({"hookSpecificOutput": {"hookEventName": native, "additionalContext": text}}, ensure_ascii=False)
    if cli == "antigravity":
        return json.dumps({"injectSteps": [{"ephemeralMessage": text}]}, ensure_ascii=False)
    if cli == "hermes":
        # Hermes json.loads() every hook's stdout (agent/shell_hooks.py) and
        # keeps only {"context": "..."}; anything unparseable is logged and
        # dropped. Plain text here was silently discarded, never injected.
        return json.dumps({"context": text}, ensure_ascii=False)
    # Kimi treats successful stdout as context; OpenCode's native bridge adds
    # it to its message parts. Copilot accepts plain context.
    return text


def repository(cwd: Path | None = None) -> Path | None:
    try:
        process = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                                 capture_output=True, text=True, timeout=1)
        return Path(process.stdout.strip()) if process.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def disabled(concern: str, cli: str) -> bool:
    root = repository() or Path.cwd()
    try:
        config = json.loads((root / ".agents/local.json").read_text()).get("hooks", {})
    except (OSError, ValueError, AttributeError):
        return False
    identifiers = {concern, ALIASES.get(concern, concern)}
    return bool(identifiers.intersection(config.get("disabled", []))) or cli in config.get("disabled_agents", [])


def executable(name: str) -> str | None:
    path = Path(name).expanduser()
    if "/" in name:
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(name)


def invoke(command: list[str], payload: dict, *, context: bool = False,
           timeout: float = 10, environment: dict[str, str] | None = None) -> dict:
    binary = executable(command[0])
    if not binary:
        return result("skipped", "handler_binary_missing")
    try:
        process = subprocess.run([binary, *command[1:]], input=json.dumps(payload),
                                 capture_output=True, text=True, timeout=timeout,
                                 env=environment or os.environ.copy())
    except subprocess.TimeoutExpired:
        return result("failed", "handler_deadline_exceeded", exit_code=1)
    except OSError:
        return result("failed", "handler_spawn_failed", exit_code=1)
    if process.returncode:
        return result("failed", "handler_exit_nonzero", process.stdout if context else "", process.returncode)
    try:
        nested = json.loads(process.stdout)
        if isinstance(nested, dict) and isinstance(nested.get("_hook_hub"), dict):
            return nested
    except ValueError:
        pass
    # Older PJ launchers fail open after printing a diagnostic. Preserve their
    # session semantics without misreporting that diagnostic as successful work.
    if "project-notebook" in " ".join(command) and any(word in process.stderr.lower() for word in ("failed open", "skipped;", "timed out")):
        return result("failed", "notebook_handler_failed_open", exit_code=1)
    return result("succeeded", "handler_completed", process.stdout if context else "")


def file_edits(payload: dict) -> list[tuple[str, str]]:
    args = payload.get("tool_input", {})
    if not isinstance(args, dict):
        return []
    path = value(args, "file_path", "path", "absolute_path", "target_file", "TargetFile")
    content = value(args, "new_string", "content", "new_content", "replacement", "ReplacementContent")
    edits: list[tuple[str, str]] = []
    if path:
        edits.append((str(path), str(content)))
    patch = value(args, "patch", "input", "patch_text")
    if not patch:
        patch = value(payload, "patch")
    if isinstance(patch, str):
        current = ""
        lines: list[str] = []
        for line in patch.splitlines():
            match = re.match(r"\*\*\* (?:Add|Update|Delete) File: (.+)$", line)
            if match:
                if current:
                    edits.append((current, "\n".join(lines)))
                current, lines = match.group(1), []
            elif current and line.startswith("+") and not line.startswith("+++"):
                lines.append(line[1:])
        if current:
            edits.append((current, "\n".join(lines)))
    return list(dict.fromkeys(edits))


def orca(payload: dict, cli: str, native: str) -> dict:
    environment = os.environ.copy()
    endpoint = environment.get("ORCA_AGENT_HOOK_ENDPOINT")
    if endpoint:
        try:
            for line in Path(endpoint).read_text().splitlines():
                if line.startswith("export "):
                    line = line[7:]
                if line.startswith("set "):
                    line = line[4:]
                key, separator, raw = line.partition("=")
                if separator and key in {"ORCA_AGENT_HOOK_PORT", "ORCA_AGENT_HOOK_TOKEN"}:
                    words = shlex.split(raw)
                    if words:
                        environment[key] = words[0]
        except (OSError, ValueError):
            return result("failed", "orca_endpoint_unreadable", exit_code=1)
    if not environment.get("ORCA_PANE_KEY"):
        return result("skipped", "not_an_orca_pane")
    port, token = environment.get("ORCA_AGENT_HOOK_PORT", ""), environment.get("ORCA_AGENT_HOOK_TOKEN", "")
    if not port.isdigit() or not token:
        return result("skipped", "orca_endpoint_unavailable")
    # All supported Orca endpoint adapters consume the native event name in
    # the payload. Hermes uses JSON; shell-client endpoints use form fields.
    native_payload = dict(payload, hook_event_name=native)
    body = {"paneKey": environment["ORCA_PANE_KEY"], "tabId": environment.get("ORCA_TAB_ID", ""),
            "launchToken": environment.get("ORCA_AGENT_LAUNCH_TOKEN", ""),
            "worktreeId": environment.get("ORCA_WORKTREE_ID", ""),
            "env": environment.get("ORCA_AGENT_HOOK_ENV", ""),
            "version": environment.get("ORCA_AGENT_HOOK_VERSION", "")}
    if cli == "hermes":
        body["payload"] = native_payload
        data = json.dumps(body).encode()
        content_type = "application/json"
    else:
        body["payload"] = json.dumps(native_payload)
        data = urllib.parse.urlencode(body).encode()
        content_type = "application/x-www-form-urlencoded"
    request = urllib.request.Request(f"http://127.0.0.1:{port}/hook/{cli}", data=data,
        headers={"Content-Type": content_type, "X-Orca-Agent-Hook-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            if not 200 <= response.status < 300:
                return result("failed", "orca_http_rejected", exit_code=1)
    except (OSError, urllib.error.URLError):
        return result("failed", "orca_http_failed", exit_code=1)
    return result("succeeded", "orca_status_delivered")


def dispatch(concern: str, raw: dict) -> dict:
    cli = os.environ.get("BB_HOOK_CLI", "unknown")
    native = os.environ.get("BB_HOOK_NATIVE", "")
    role = os.environ.get("BB_HOOK_ROLE", "")
    if disabled(concern, cli):
        return result("skipped", "project_disabled")
    payload = normalized(raw)
    if concern.startswith("hindsight-"):
        from hindsight import dispatch as memory_dispatch
        return memory_dispatch(concern, payload, cli, native)
    if concern == "skill-reminder":
        if cli == "antigravity" and int(raw.get("invocationNum", 1) or 1) > 1:
            return result("skipped", "not_first_invocation")
        out = invoke(["~/.agents/hooks/reminder-for-skill-check.sh"], payload, context=True, timeout=0.5)
        out["stdout"] = context_output(out["stdout"], cli, native)
        return out
    if concern == "skill-lint":
        paths = [Path(path).expanduser() for path, _ in file_edits(payload)
                 if re.search(r"(?:^|/)\.agents/skills/[^/]+/SKILL\.md$", path)]
        if not paths:
            return result("skipped", "no_skill_manifest_edited")
        command = [sys.executable, str(Path.home() / ".agents/hooks/lint-skills.py"), "--quiet", *map(str, paths)]
        return invoke(command, payload, timeout=3)
    if concern == "orca-status":
        return orca(raw, cli, native)
    if concern == "nanoleaf":
        event = {"session_start": "session-start", "session_end": "session-end", "prompt_submit": "prompt-submit", "turn_completed": "turn-end",
                 "subagent_start": "subagent-start", "subagent_stop": "subagent-stop", "tool_failed": "tool-failure"}.get(role)
        event = event or {"Notification": "notification", "PermissionRequest": "permission-request",
                          "StopFailure": "error", "PostToolUseFailure": "tool-failure"}.get(native)
        return invoke(["~/.local/bin/nlp", "hook", event], payload, timeout=2) if event else result("skipped", "no_panel_transition")
    if concern == "claude-notify":
        return invoke(["~/.local/bin/claude-notify"], payload, timeout=5)
    if concern == "zellij-notify":
        if not os.environ.get("ZELLIJ_SESSION_NAME") or not os.environ.get("ZELLIJ_PANE_ID"):
            return result("skipped", "not_a_zellij_pane")
        action = "--clear" if role == "prompt_submit" else "attention"
        return invoke(["~/.config/zellij/scripts/zellij-notify", action], payload, timeout=1)
    if concern.startswith("project-notebook-"):
        # The canonical PJ notebook engine currently accepts Claude identities
        # only. Never misattribute another CLI as Claude to bypass that contract.
        if cli != "claude":
            return result("skipped", "notebook_cli_unsupported")
        if repository(Path(payload["cwd"])) is None:
            return result("skipped", "not_a_git_repository")
        event = "start" if concern.endswith("start") else "end"
        environment = os.environ.copy()
        environment["PJ_HOOK_OWNER"] = "project-notebook.v1"
        return invoke([f"~/.agents/skills/project-notebook/hooks/session-{event}.sh"], payload,
                      context=event == "start", timeout=4, environment=environment)
    if concern.startswith("codegraph-"):
        root = repository()
        if not root or not (root / ".codegraph").is_dir():
            return result("skipped", "repository_not_indexed")
        action = "prompt-hook" if concern.endswith("prompt") else "sync"
        output = invoke(["codegraph", action], payload, context=action == "prompt-hook", timeout=2)
        if output["stdout"]:
            output["stdout"] = context_output(output["stdout"], cli, native)
        return output
    if concern.startswith("code-review-graph-"):
        root = repository()
        if not root or not (root / ".code-review-graph").is_dir():
            return result("skipped", "repository_not_indexed")
        action = concern.removeprefix("code-review-graph-")
        if action == "before-commit":
            if not re.match(r"^\s*git\s+commit\b", str(payload.get("tool_input", {}).get("command", ""))):
                return result("skipped", "not_a_git_commit")
            command = ["code-review-graph", "detect-changes", "--brief"]
        else:
            command = ["code-review-graph", action]
            if action == "update":
                command.append("--skip-flows")
        output = invoke([*command, "--repo", str(root)], payload, context=action != "update", timeout=30)
        if output["stdout"]:
            output["stdout"] = context_output(output["stdout"], cli, native)
        return output
    if concern == "merge-forward":
        root = repository()
        if not root:
            return result("skipped", "not_a_git_repository")
        # A repo extension must opt in by owning its worker; never tune the
        # global base skill as a side effect of an arbitrary project's session.
        worker = root / ".agents/skills/33god-merge-forward/scripts/rebalance.py"
        if not worker.is_file():
            return result("skipped", "repository_has_no_merge_forward_tuner")
        environment = os.environ.copy()
        environment["GOD_MERGE_FORWARD_REBALANCE"] = "1"
        return invoke([sys.executable, str(worker), "--client", cli, "--stdin", "--hub"], payload,
                      context=True, timeout=900, environment=environment)
    if concern == "git-checkpoint":
        root = repository()
        if root is None:
            return result("skipped", "not_a_git_repository")
        if root == Path.home() / "code/intelliforia":
            return result("skipped", "project_checkpoint_opt_out")
        return invoke([str(Path.home() / "code/GitMark/bin/git-checkpoint"), str(root)], payload, timeout=120)
    return result("failed", "unknown_concern", exit_code=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("concern")
    args = parser.parse_args()
    try:
        raw = json.loads(sys.stdin.read() or "{}")
        output = dispatch(args.concern, raw if isinstance(raw, dict) else {})
    except Exception:
        output = result("failed", "handler_exception", exit_code=1)
    print(json.dumps(output, ensure_ascii=False))
    return 0  # The structured result carries failures; the hub fails open.


if __name__ == "__main__":
    raise SystemExit(main())
