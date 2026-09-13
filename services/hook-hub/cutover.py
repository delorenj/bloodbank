#!/usr/bin/env python3
"""Retire known behavioral native hooks after the bb-hook projection is installed.

Default is read-only. --apply removes only recognized old concern commands.
--activate separately enables staged registry rows and records hub ownership,
after all inspected native configs have no legacy managed commands remaining.
No backups are written beside credential-bearing native configuration files.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import tempfile
import tomllib
import sys
from datetime import datetime, timezone
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent
REGISTRY = SERVICE_DIR / "handlers.toml"
CLIS = ["claude", "codex", "copilot", "kimi", "gemini", "opencode", "hermes", "antigravity"]
LEGACY_MARKERS = (
    "/.agents/hooks/reminder-for-skill-check.sh",
    "/.agents/hooks/hindsight/hindsight-recall.sh",
    "/.agents/hooks/hindsight/hindsight-retain.sh",
    "/.agents/hooks/hindsight/hindsight-session-end.sh",
    "/.agents/hooks/merge-forward/session-end.sh",
    "$HOOKS/merge-forward/session-end.sh",
    "/.agents/hooks/lint-skills.py",
    "/.agents/scripts/lint-skills.py",
    "/.agents/skills/project-notebook/hooks/session-",
    "/.orca/agent-hooks/",
)


def owned(command: object) -> bool:
    if isinstance(command, list):
        # Old Gemini registrations incorrectly stored shell argv arrays; they
        # still belong to the concern and must be retired before valid groups.
        return any(owned(part) for part in command)
    if not isinstance(command, str) or "bb-hook" in command:
        return False
    return any(marker in command for marker in LEGACY_MARKERS) or bool(
        re.search(r"(?:^|\s|/)claude-notify(?:\s|$)", command)
        or re.search(r"\bnlp\s+hook\s", command)
        or re.search(r"\bcodegraph\s+prompt-hook\b", command)
        or re.search(r"\bcode-review-graph\s+(?:status|update|detect-changes)\b", command)
    )


def prune(value: object) -> tuple[object, int]:
    """Remove inner commands, preserving sibling hooks and group matchers."""
    if isinstance(value, list):
        kept, count = [], 0
        for item in value:
            if isinstance(item, dict) and owned(item.get("command", item.get("bash"))):
                count += 1
                continue
            new, removed = prune(item)
            count += removed
            if removed and isinstance(new, dict) and new.get("hooks") == []:
                continue
            kept.append(new)
        return kept, count
    if isinstance(value, dict):
        result, count = {}, 0
        for key, item in value.items():
            new, removed = prune(item)
            result[key] = new
            count += removed
        return result, count
    return value, 0


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(prefix=".hook-cutover-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def strip_kimi(text: str) -> tuple[str, int]:
    pieces = re.split(r"(?m)(?=^\[\[hooks\]\]\s*$)", text)
    kept, removed = [], 0
    for part in pieces:
        if not part.startswith("[[hooks]]"):
            kept.append(part)
            continue
        # A following ordinary section belongs to the user, not this hook.
        boundary = re.search(r"(?m)^\[(?!\[hooks\]\])[^\n]+\]\s*$", part[len("[[hooks]]"):])
        end = len("[[hooks]]") + boundary.start() if boundary else len(part)
        block, tail = part[:end], part[end:]
        row = tomllib.loads(block).get("hooks", [{}])[0]
        if owned(row.get("command")):
            removed += 1
            kept.append(tail)
        else:
            kept.append(part)
    return "".join(kept), removed


def strip_notify(text: str) -> tuple[str, int]:
    notify = tomllib.loads(text).get("notify")
    if not isinstance(notify, list) or not any(owned(item) for item in notify):
        return text, 0
    # Only the known combined legacy callback is retired; arbitrary user
    # notify commands must survive. Its argv never carries non-shell extras.
    if any(isinstance(item, str) and item not in {"bash", "sh", "-c"}
           and not owned(item) for item in notify):
        return text, 0
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("["):
            break
        if re.match(r"^\s*notify\s*=", line):
            for end in range(index + 1, len(lines) + 1):
                try:
                    parsed = tomllib.loads("".join(lines[index:end]))
                except tomllib.TOMLDecodeError:
                    continue
                if parsed.get("notify") == notify:
                    return "".join(lines[:index] + lines[end:]), 1
    return text, 0


def native_sync():
    source = SERVICE_DIR.parent / "agent-hooks/sync.py"
    spec = importlib.util.spec_from_file_location("_hook_cutover_native_sync", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def codex_paths(home: Path) -> list[Path]:
    if home.resolve() != Path.home().resolve():
        return [home / ".codex/hooks.json"]
    module = native_sync()
    return [path for _label, path, _ in module.discover_codex_configs(module.load_master()["agents"]["codex"])]


def json_paths(home: Path, projects: list[Path]) -> list[Path]:
    paths = [home / ".claude/settings.json", *codex_paths(home), home / ".gemini/settings.json",
             home / ".gemini/config/hooks.json", home / ".copilot/settings.json"]
    paths.extend(sorted((home / ".copilot/hooks").glob("*.json")))
    paths.extend(root / ".claude/settings.json" for root in projects)
    return list(dict.fromkeys(path for path in paths if path.is_file()))


def inspect(home: Path, projects: list[Path], apply: bool = False) -> list[dict]:
    report = []
    for path in json_paths(home, projects):
        original = json.loads(path.read_text())
        updated, count = prune(original)
        if count:
            if apply:
                write(path, json.dumps(updated, indent=2, ensure_ascii=False) + "\n")
            report.append({"path": str(path), "removed": count, "applied": apply})
    kimi = home / ".kimi-code/config.toml"
    if kimi.exists():
        updated, count = strip_kimi(kimi.read_text())
        if count:
            if apply:
                write(kimi, updated)
            report.append({"path": str(kimi), "removed": count, "applied": apply})
    for hooks_path in codex_paths(home):
        codex = hooks_path.parent / "config.toml"
        if codex.exists():
            updated, count = strip_notify(codex.read_text())
            if count:
                if apply:
                    write(codex, updated)
                report.append({"path": str(codex), "removed": count, "applied": apply})
    for name, marker in (("hindsight-memory.ts", "/.agents/hooks/claude/publish.py"),
                         ("crg-plugin.ts", 'app.on("file.edited"')):
        path = home / ".config/opencode/plugins" / name
        if path.is_file() and marker in path.read_text():
            # These are known legacy owners, superseded by the canonical
            # bb-hook plugin. Their prior source remains in config Git history.
            if apply:
                path.unlink()
            report.append({"path": str(path), "removed": 1, "applied": apply})
    return report


def activate(home: Path) -> dict:
    text = REGISTRY.read_text().replace("enabled = false # cutover-managed", "enabled = true # cutover-managed")
    parsed = tomllib.loads(text)
    handlers = [row["id"] for row in parsed["handler"] if row.get("enabled", True)]
    write(REGISTRY, text)
    manifest = {"version": 1, "registry": str(REGISTRY), "clis": CLIS, "handler_ids": handlers,
                "installed_at": datetime.now(timezone.utc).isoformat()}
    path = home / ".config/33god/hook-hub/ownership.json"
    write(path, json.dumps(manifest, indent=2) + "\n")
    return {"activated": handlers, "manifest": str(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--project", type=Path, action="append", default=[])
    parser.add_argument("--install", action="store_true", help="with --apply, regenerate/install native hooks and preserve Codex trust across pruning")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    snapshots = {}
    module = None
    if args.install:
        if not args.apply or args.home.resolve() != Path.home().resolve():
            parser.error("--install requires --apply and the current user's home")
        module = native_sync()
        from codex_native import capture_trust
        # Native loader metadata and raw trust state stay in memory. Capture
        # before pruning: identical retained foreign hooks may be separately
        # enabled/disabled at their original positional keys.
        for path in codex_paths(args.home):
            snapshots[str(path)] = capture_trust(path.parent / "config.toml")
        generated = module.cmd_apply(module.load_master(), module.load_lock(), False)
        if generated:
            return generated
    report = inspect(args.home, args.project, args.apply)
    if module is not None:
        installed = module.cmd_install(module.load_master(), codex_trust_before=snapshots)
        if installed:
            return installed
        from tool_guards import install as install_tool_guards
        print(json.dumps({"tool_guard": install_tool_guards(args.home)}))
    if args.activate and report:
        print(json.dumps({"error": "legacy_native_handlers_remain", "changes": report}, indent=2))
        return 1
    print(json.dumps({"changes": report,
                      **({"next": "Verify native hooks/list before --activate." if args.install else "Use --apply --install for in-memory Codex trust preservation across native pruning; verify native hooks/list before --activate."} if args.apply else {}),
                      **(activate(args.home) if args.activate else {})}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
