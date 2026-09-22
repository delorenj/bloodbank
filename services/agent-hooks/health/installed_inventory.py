"""Read-only native wiring inventory for hook-hub and the operator UI.

Configuration is evidence of wiring, not proof that an invocation succeeded.
Codex's native loader is queried without executing hooks or changing config;
no payload/configuration secrets are returned.
"""
from __future__ import annotations

import json
import shlex
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))
import sync
from cli_paths import find_cli_binary

DIRECT_MARKERS = (
    "/hindsight/hindsight-", "hindsight-session-end", "reminder-for-skill-check.sh",
    "/orca/agent-hooks/", "/.orca/agent-hooks/", "claude-notify", "nlp-hook",
    "/merge-forward/session-end.sh", "/codegraph/", "codegraph-hook",
    "code-review-graph", "crg-hook", "PJ_HOOK_OWNER=project-notebook",
    "/skill-lint/", "lint-skills.sh", "/zellij/",
)


def config_paths(name: str, agent: dict) -> list[tuple[str, Path, Path | None]]:
    if agent.get("dialect") == "hermes_config":
        return sync.discover_hermes_configs(agent)
    if name == "codex":
        return sync.discover_codex_configs(agent)
    paths = [(name, sync._expand(agent["live_target"]), None)] if agent.get("live_target") else []
    if name == "copilot" and paths:
        primary = paths[0][1]
        for path in sorted(primary.parent.glob("*.json")):
            if path != primary:
                paths.append((path.stem, path, None))
        settings = primary.parent.parent / "settings.json"
        if settings.exists():
            paths.append(("settings", settings, None))
    return paths


def load_config(path: Path, dialect: str) -> dict:
    raw = path.read_text()
    if dialect == "hermes_config":
        import yaml
        return yaml.safe_load(raw) or {}
    if dialect == "kimi_toml":
        return tomllib.loads(raw)
    return json.loads(raw)


def native_rows(cfg: dict, dialect: str) -> list[dict]:
    rows = []
    if dialect == "kimi_toml":
        return [{"native": row.get("event"), **row} for row in cfg.get("hooks", []) if isinstance(row, dict)]
    blocks = cfg.values() if dialect == "antigravity_bundle" else [cfg.get("hooks") or {}]
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for native, groups in block.items():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                inner = group.get("hooks")
                for row in inner if isinstance(inner, list) else [group]:
                    if not isinstance(row, dict):
                        continue
                    command = row.get("command") or row.get("bash")
                    if isinstance(command, list):
                        command = shlex.join(str(x) for x in command)
                    if command:
                        rows.append({"native": native, "command": command,
                                     "matcher": group.get("matcher"),
                                     "timeout": row.get("timeoutSec", row.get("timeout")),
                                     "enabled": row.get("enabled", True),
                                     "condition": group.get("condition"),
                                     "malformed": dialect == "gemini_settings" and not isinstance(inner, list)})
    return rows


def _is_hub(command: Any) -> bool:
    try:
        return any(Path(token).name == "bb-hook" for token in shlex.split(str(command)))
    except ValueError:
        return False


def _is_direct(command: Any, markers: list[str]) -> bool:
    return not _is_hub(command) and sync._has_marker(command, [*markers, *DIRECT_MARKERS])


def _binary_available(name: str) -> bool:
    return find_cli_binary(name) is not None


def collect_installed_inventory(master: dict | None = None, *, probe_native: bool = True) -> dict:
    master = master or sync.load_master()
    lock = sync.load_lock()
    native_codex = None
    if probe_native and _binary_available("codex"):
        try:
            from codex_native import capture_trust
            native_codex = []
            for _, path, _ in config_paths("codex", master["agents"]["codex"]):
                # Native hooks/list also includes project sources for its cwd.
                # Verify each user source in its own runtime trust context.
                native_codex.extend(h for h in capture_trust(path.parent / "config.toml")["hooks"]
                                    if h.get("sourcePath") == str(path))
        except Exception:
            native_codex = None
    clis = []
    for name, agent in master["agents"].items():
        dialect = agent.get("dialect")
        supported = agent.get("support_status", "supported") == "supported"
        item = {"cli": name, "label": agent.get("label", name),
                "support_status": agent.get("support_status", "supported"),
                "binary_available": _binary_available(name), "configs": [], "natives": []}
        if not supported or dialect in {"watcher", "runtime"}:
            item["status"] = "unsupported"
            clis.append(item)
            continue
        generated = sync.render_config(agent, master["lifecycle"], lock)
        expected = native_rows(generated, dialect) if dialect != "opencode_plugin" else []
        by_native = {row["native"]: row for row in expected}
        sources = []
        paths = config_paths(name, agent)
        for label, path, allowlist in paths:
            entry = {"label": label, "source": str(path), "status": "configured", "errors": []}
            rows = []
            if not path.is_file():
                entry.update(status="missing", errors=["config_absent"])
            elif dialect == "opencode_plugin":
                canonical = SERVICE_DIR / agent["plugin_source"]
                if path.read_bytes() != canonical.read_bytes():
                    entry.update(status="drift", errors=["plugin_source_drift"])
                else:
                    rows = [{"native": b["native"], "command": generated["hooks"][b["native"]]}
                            for b in agent["bindings"]]
                legacy = [path.parent / filename for filename in ("hindsight-memory.ts", "crg-plugin.ts")
                          if (path.parent / filename).is_file()]
                if legacy:
                    entry.update(status="drift", errors=[f"legacy_plugin:{p.name}" for p in legacy])
            else:
                try:
                    cfg = load_config(path, dialect)
                    rows = native_rows(cfg, dialect)
                    if dialect == "hermes_config" and cfg.get("hooks_auto_accept") is not True:
                        approvals = set()
                        if allowlist and allowlist.exists():
                            alw = json.loads(allowlist.read_text())
                            approvals = {(a.get("event"), a.get("command")) for a in alw.get("approvals", [])}
                        for row in rows:
                            if _is_hub(row["command"]) and (row["native"], row["command"]) not in approvals:
                                row["trust_error"] = "approval_missing"
                except Exception as exc:
                    entry.update(status="unreadable", errors=[f"config_parse:{type(exc).__name__}"])
            item["configs"].append(entry)
            sources.append((path, rows, entry))
        markers = sync._publisher_markers(name, agent)
        for binding in agent["bindings"]:
            native = binding["native"]
            # Every Hermes profile is an independent native hook configuration.
            count = len(paths) if dialect in {"hermes_config", "codex"} else 1
            row = {"native": native, "role": binding["role"], "expected_count": count,
                   "actual_hub_count": 0, "direct_managed_count": 0, "sources": []}
            has_issues = False
            for path, installed, config in sources:
                native_hooks = [h for h in installed if h["native"] == native]
                hub = [h for h in native_hooks if _is_hub(h["command"])]
                direct = [h for h in native_hooks if _is_direct(h["command"], markers)]
                issues = list(config["errors"])
                want = by_native.get(native)
                for hook in hub:
                    if want:
                        for field in ("command", "matcher", "timeout", "condition"):
                            if hook.get(field) != want.get(field):
                                issues.append(f"{field}_drift")
                    hook_enabled = hook.get("enabled", True)
                    if hook_enabled is False:
                        issues.append("disabled")
                    elif hook_enabled is not True:
                        issues.append("enabled_invalid")
                    if hook.get("trust_error"):
                        issues.append(hook["trust_error"])
                    if hook.get("malformed"):
                        issues.append("invalid_native_schema")
                    if name == "codex" and probe_native:
                        if native_codex is None:
                            issues.append("native_verification_unavailable")
                        else:
                            loaded = [h for h in native_codex if h.get("sourcePath") == str(path)
                                      and h.get("command") == hook.get("command")]
                            if not loaded:
                                issues.append("native_loader_missing")
                            for loaded_hook in loaded:
                                # No key means enabled; only an explicit false
                                # disables, and a present non-boolean is invalid.
                                native_enabled = loaded_hook.get("enabled", True)
                                if native_enabled is False:
                                    issues.append("native_disabled")
                                elif native_enabled is not True:
                                    issues.append("native_enabled_invalid")
                                if loaded_hook.get("trustStatus") != "trusted":
                                    issues.append("native_untrusted")
                                if want and loaded_hook.get("timeoutSec") != want.get("timeout"):
                                    issues.append("native_timeout_drift")
                row["actual_hub_count"] += len(hub)
                row["direct_managed_count"] += len(direct)
                if hub or direct or dialect == "hermes_config" or path == paths[0][1]:
                    row["sources"].append({"source": str(path), "hub_count": len(hub),
                                           "direct_count": len(direct), "issues": sorted(set(issues))})
                has_issues |= bool(issues)
            actual = row["actual_hub_count"]
            row["status"] = ("duplicate" if actual > count or (actual and row["direct_managed_count"])
                             else "missing" if actual < count else "drift" if has_issues else "configured")
            item["natives"].append(row)
        item["trust_verification"] = ("native_verified" if native_codex is not None else "native_probe_unavailable") if name == "codex" else "configured"
        drift = any(row["status"] != "configured" for row in item["natives"])
        item["status"] = "drift" if drift else "configured" if item["binary_available"] else "not_installed"
        clis.append(item)
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "drift" if any(c["status"] == "drift" for c in clis) else "healthy",
            "clis": clis}


build_deployed_inventory = collect_installed_inventory
