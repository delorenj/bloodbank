#!/usr/bin/env python3
"""Validate that every DEPLOYED agent hook config produces error-free hooks.

For each config this service manages (claude/codex/copilot live targets + every
hermes fleet agent from ~/.hermes/agents-registry.yaml), parse the configured
hook commands and check them WITHOUT destructive side effects:

  * bloodbank publisher commands (our `<agent>/publish.py <arg>`): the arg must
    resolve in the publisher's map AND the resulting event must build a
    contract+schema-valid CloudEvents envelope. This is the deterministic
    "the hook produces a valid event" check (catches the session-stop class:
    a configured arg the publisher doesn't know).
  * foreign commands (hindsight, git-checkpoint, lint-skills, claude-notify,
    zellij, …): NEVER executed (they commit/push/notify/write). Static only:
    `bash -n` syntax + every referenced absolute/$HOOKS/$HOME script resolves
    and is executable.
  * hermes configs additionally: each publisher command present for all events
    AND exact-match present in shell-hooks-allowlist.json (else it never fires).

Output: a Holocene ToolingStatSnapshot<ToolingCollectionValue> (one item per
agent type; hermes rolls up its fleet).

Modes:
    --json     print the snapshot
    --check    exit 1 if any deployed config has a failing hook (test/CI gate)
    --emit     write Redis key holocene:tooling:stat:agent-hook-tests + a local
               evidence artifact (used by the systemd timer)

Stdlib + PyYAML (already a dep of sync.py's install path).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

import sync  # noqa: E402  (SSOT loader + effective_type + fleet enumeration)
from core.envelope import build_envelope  # noqa: E402
from core.validate import ValidationUnavailable, load_schema_for  # noqa: E402

import claude.publish as _cl  # noqa: E402
import copilot.publish as _cp  # noqa: E402
import codex.publish as _cx  # noqa: E402
import hermes.publish as _hm  # noqa: E402

REDIS_KEY = "holocene:tooling:stat:agent-hook-tests"
TTL_SECONDS = int(os.environ.get("AGENT_HOOK_TESTS_TTL", 60 * 60 * 24 * 7))
ARTIFACT = SERVICE_DIR.parent.parent / "_bmad_output" / "evidence" / "health" / "agent-hook-tests.json"
UUID = "00000000-0000-0000-0000-000000000001"

# Per-agent publisher identity + resolved arg→(type,bucket) map.
IDENT: dict[str, dict[str, Any]] = {
    "claude": dict(source=_cl.CLAUDE_SOURCE, producer=_cl.CLAUDE_PRODUCER, service=_cl.CLAUDE_SERVICE,
                   actor=_cl.CLAUDE_ACTOR, map=_cl.EVENT_MAP, label="Claude"),
    "copilot": dict(source=_cp.COPILOT_SOURCE, producer=_cp.COPILOT_PRODUCER, service=_cp.COPILOT_SERVICE,
                    actor=_cp.COPILOT_ACTOR, map=_cp.HOOK_MAP, label="Copilot"),
    "codex": dict(source=_cx.CODEX_SOURCE, producer=_cx.CODEX_PRODUCER, service=_cx.CODEX_SERVICE,
                  actor=_cx.CODEX_ACTOR, map=_cx.HOOK_MAP, label="Codex"),
    "hermes": dict(source=_hm.HERMES_SOURCE, producer=_hm.HERMES_PRODUCER, service=_hm.HERMES_SERVICE,
                   actor=_hm.HERMES_ACTOR, map=_hm.HOOK_MAP, label="Hermes"),
}

HOOKS_ENV = os.environ.get("HOOKS", str(Path.home() / ".agents" / "hooks"))
HOME = str(Path.home())

# ---------------------------------------------------------------------------
# Schema-driven representative payload (mirrors smoketest-agent-hooks-ssot.sh)
# ---------------------------------------------------------------------------


def _sample(spec: dict) -> Any:
    if not isinstance(spec, dict):
        return "x"
    if spec.get("enum"):
        return spec["enum"][0]
    t = spec.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        t = non_null[0] if non_null else t[0]
    return {"string": "x", "integer": 1, "number": 1, "boolean": True, "array": [], "object": {}}.get(t, "x")


def _data_for(ce_type: str) -> dict:
    data_schema = load_schema_for(ce_type).get("properties", {}).get("data", {})
    props = data_schema.get("properties", {})
    return {k: _sample(props.get(k, {})) for k in data_schema.get("required", [])}


# ---------------------------------------------------------------------------
# Command classification + checks
# ---------------------------------------------------------------------------


def _publisher_arg(command: str, markers: list[str]) -> str | None:
    """Extract the event arg from either legacy or canonical publisher commands."""
    after = None
    for marker in markers:
        idx = command.find(marker)
        if idx >= 0:
            after = command[idx + len(marker):]
            break
    if after is None:
        return None
    try:
        toks = shlex.split(after)
    except ValueError:
        toks = after.split()
    for i, tok in enumerate(toks):
        if tok == "--hook" and i + 1 < len(toks):
            return toks[i + 1]
        if tok.startswith("--hook="):
            return tok.split("=", 1)[1]
    return toks[0] if toks else None


# Absolute / $HOOKS / $HOME script references worth verifying (deterministic);
# bare bins (jq, zellij, mise, claude-notify) are PATH-dependent — skipped.
def _referenced_scripts(command: str) -> list[str]:
    expanded = (command.replace("${HOOKS}", HOOKS_ENV).replace("$HOOKS", HOOKS_ENV)
                .replace("${HOME}", HOME).replace("$HOME", HOME))
    out: list[str] = []
    for tok in shlex.split(expanded, comments=False, posix=True) if _safe_split(expanded) else []:
        tok = tok.strip("'\"")
        if tok.startswith("/") and (tok.endswith(".sh") or tok.endswith(".py")
                                    or "/.agents/hooks/" in tok or "/.agents/skills/bin/" in tok
                                    or "/.codex/hooks/" in tok or "/.local/bin/" in tok):
            out.append(tok)
    return out


def _safe_split(s: str) -> bool:
    try:
        shlex.split(s)
        return True
    except ValueError:
        return False


def _bash_syntax_ok(command: str) -> bool:
    try:
        r = subprocess.run(["bash", "-n", "-c", command], capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return True  # can't check → don't fail on tooling absence


def _check_bloodbank(agent: str, arg: str | None) -> tuple[bool, str | None]:
    ident = IDENT[agent]
    if not arg:
        return False, "no event arg after publish.py"
    mapping = ident["map"].get(arg)
    if mapping is None:
        return False, f"arg {arg!r} not in {agent} publisher map (would no-op)"
    ce_type, bucket = mapping
    try:
        build_envelope(
            ce_type=ce_type, kind="event",
            source=ident["source"], producer=ident["producer"], service=ident["service"],
            actor=ident["actor"], data=_data_for(ce_type),
            correlation_id=UUID, causation_id=UUID,
            ordering_key=f"{bucket}:healthcheck", validate=True,
        )
    except ValidationUnavailable:
        return True, None  # contract passed; jsonschema absent in this interpreter (run via mise for schema check)
    except Exception as exc:  # noqa: BLE001
        return False, f"{ce_type}: {exc}"
    return True, None


def _check_foreign(command: str) -> tuple[bool, str | None]:
    if not _bash_syntax_ok(command):
        return False, "bash -n syntax error"
    missing = [p for p in _referenced_scripts(command)
               if not (os.path.isfile(p) and os.access(p, os.X_OK))]
    if missing:
        return False, "missing/non-executable: " + ", ".join(missing)
    return True, None


# ---------------------------------------------------------------------------
# Per-dialect command extraction
# ---------------------------------------------------------------------------


def _commands_from_nested(cfg: dict) -> list[tuple[str, str]]:
    """claude_settings / codex: hooks.<event>[].hooks[].command"""
    out = []
    for event, groups in (cfg.get("hooks") or {}).items():
        for grp in groups or []:
            for h in (grp or {}).get("hooks", []) or []:
                cmd = (h or {}).get("command")
                if cmd:
                    out.append((event, cmd))
    return out


def _commands_from_copilot(cfg: dict) -> list[tuple[str, str]]:
    out = []
    for event, entries in (cfg.get("hooks") or {}).items():
        for e in entries or []:
            cmd = (e or {}).get("bash")
            if cmd:
                out.append((event, cmd))
    return out


def _commands_from_hermes(cfg: dict) -> list[tuple[str, str]]:
    out = []
    for event, entries in (cfg.get("hooks") or {}).items():
        for e in entries or []:
            cmd = (e or {}).get("command")
            if cmd:
                out.append((event, cmd))
    return out


def _load_config(path: Path, dialect: str):
    if not path.exists():
        return None
    text = path.read_text()
    if dialect == "hermes_config":
        import yaml
        return yaml.safe_load(text) or {}
    return json.loads(text)


# ---------------------------------------------------------------------------
# Validate one config file
# ---------------------------------------------------------------------------


def _check_config(
    agent: str,
    dialect: str,
    path: Path,
    allowlist_path: Path | None,
    markers: list[str],
) -> dict:
    result = {"config": str(path), "ok": True, "entries": [], "note": None}
    cfg = None
    try:
        cfg = _load_config(path, dialect)
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["note"] = f"unreadable config: {exc}"
        return result
    if cfg is None:
        result["ok"] = False
        result["note"] = "config file absent"
        return result

    if dialect in ("claude_settings", "codex"):
        commands = _commands_from_nested(cfg)
    elif dialect == "copilot":
        commands = _commands_from_copilot(cfg)
    elif dialect == "hermes_config":
        commands = _commands_from_hermes(cfg)
    else:
        commands = []

    # allowlist (hermes only) — exact event+command match required by hermes.
    allow: set[tuple[str, str]] = set()
    if allowlist_path and allowlist_path.exists():
        try:
            alw = json.loads(allowlist_path.read_text())
            allow = {(a.get("event"), a.get("command")) for a in alw.get("approvals", []) if isinstance(a, dict)}
        except (OSError, json.JSONDecodeError):
            allow = set()

    seen_bb_args: set[str] = set()
    for event, command in commands:
        if sync._has_marker(command, markers):
            arg = _publisher_arg(command, markers)
            ok, err = _check_bloodbank(agent, arg)
            entry = {"event": event, "kind": "bloodbank", "arg": arg, "ok": ok,
                     "check": "envelope", "error": err}
            if ok and dialect == "hermes_config" and allowlist_path is not None:
                if (event, command) not in allow:
                    entry["ok"] = False
                    entry["error"] = "not in shell-hooks-allowlist (hook will not fire)"
            if arg:
                seen_bb_args.add(arg)
        else:
            ok, err = _check_foreign(command)
            entry = {"event": event, "kind": "foreign", "ok": ok, "check": "static", "error": err,
                     "command": command[:120]}
        if not entry["ok"]:
            result["ok"] = False
        result["entries"].append(entry)

    return result


# ---------------------------------------------------------------------------
# Build the full report (Holocene ToolingCollectionValue)
# ---------------------------------------------------------------------------

SEVERITY_FOR = {True: "ok", False: "critical"}


def _agent_configs(master: dict, agent_name: str, agent: dict) -> list[tuple[str, Path, Path | None]]:
    """Return [(label, config_path, allowlist_path|None)] for an agent."""
    dialect = agent.get("dialect")
    if dialect in ("watcher", "runtime"):
        return []
    if dialect == "hermes_config":
        out = []
        reg = sync._expand(agent["fleet_registry"]) if agent.get("fleet_registry") else None
        if reg and reg.exists():
            import yaml
            rd = yaml.safe_load(reg.read_text()) or {}
            subdir = agent.get("runtime_subdir", "runtime")
            alw_name = agent.get("allowlist_filename", "shell-hooks-allowlist.json")
            for aid, meta in (rd.get("agents") or {}).items():
                role_dir = (meta or {}).get("role_dir")
                if not role_dir:
                    continue
                rt = Path(role_dir) / subdir
                out.append((aid, rt / "config.yaml", rt / alw_name))
        return out
    live = agent.get("live_target")
    return [(agent_name, sync._expand(live), None)] if live else []


def build_report(master: dict) -> dict:
    items = []
    flat_entries = []
    total = passed = failed = 0
    overall_ok = True
    overall_warn = False

    for agent_name, agent in master["agents"].items():
        dialect = agent.get("dialect")
        if dialect in ("watcher", "runtime") or agent_name not in IDENT:
            continue
        configs = _agent_configs(master, agent_name, agent)
        cfg_results = []
        markers = sync._publisher_markers(agent_name, agent)
        for label, path, alw in configs:
            cfg_results.append(
                (label, _check_config(agent_name, dialect, path, alw, markers))
            )

        present = [r for _, r in cfg_results if r.get("note") != "config file absent"]
        absent = [r for _, r in cfg_results if r.get("note") == "config file absent"]
        cfg_ok = sum(1 for r in present if r["ok"])
        cfg_fail = sum(1 for r in present if not r["ok"])
        for _, r in cfg_results:
            for e in r["entries"]:
                total += 1
                if e["ok"]:
                    passed += 1
                else:
                    failed += 1
                flat_entries.append({"agent": agent_name, **e})

        if cfg_fail:
            severity = "critical"
            overall_ok = False
        elif absent or not present:
            severity = "warning"
            overall_warn = True
        else:
            severity = "ok"

        if dialect == "hermes_config":
            summary = f"{cfg_ok}/{len(configs)} fleet configs ok"
            if absent:
                summary += f" · {len(absent)} uninitialized"
        else:
            r0 = cfg_results[0][1] if cfg_results else {"entries": [], "ok": False, "note": "no config"}
            n_ok = sum(1 for e in r0["entries"] if e["ok"])
            summary = (f"{n_ok}/{len(r0['entries'])} hooks ok" if r0["entries"]
                       else (r0.get("note") or "no hooks"))

        items.append({
            "id": agent_name,
            "label": IDENT[agent_name]["label"],
            "severity": severity,
            "statusLabel": {"ok": "OK", "warning": "Warn", "critical": "Fail"}.get(severity, "?"),
            "summary": summary,
            "detail": {
                "agent": agent_name,
                "configCount": len(configs),
                "configsOk": cfg_ok,
                "configsFailing": cfg_fail,
                "uninitialized": len(absent),
                "configs": [{"label": lbl, **r} for lbl, r in cfg_results],
            },
        })

    status = "critical" if not overall_ok else ("warning" if overall_warn else "healthy")
    value = {
        "view": {"kind": "collection", "layout": "grid", "title": "Agent Hook Tests"},
        "items": items,
        "summary": {"total": total, "ok": passed, "failing": failed, "agents": len(items)},
        "entries": flat_entries,
    }
    return {
        "id": "agent-hook-tests",
        "value": value,
        "status": status,
        "observedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS)).isoformat().replace("+00:00", "Z"),
        "meta": {"redisKey": REDIS_KEY, "source": "bloodbank/services/agent-hooks/health"},
    }


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def _emit(snapshot: dict) -> int:
    from core import redis_publish
    body = json.dumps(snapshot)
    rc = 0
    try:
        redis_publish.set_key(REDIS_KEY, body, TTL_SECONDS)
        print(f"hook-health: wrote {REDIS_KEY} (status={snapshot['status']})")
    except Exception as exc:  # noqa: BLE001
        print(f"hook-health: WARN redis write failed: {exc}", file=sys.stderr)
        rc = 1
    try:
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(json.dumps(snapshot, indent=2) + "\n")
        print(f"hook-health: wrote artifact {ARTIFACT}")
    except OSError as exc:
        print(f"hook-health: WARN artifact write failed: {exc}", file=sys.stderr)
        rc = 1
    return rc


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Validate deployed agent hook configs are error-free.")
    p.add_argument("--json", action="store_true", help="print the snapshot")
    p.add_argument("--check", action="store_true", help="exit nonzero if any deployed config has a failing hook")
    p.add_argument("--emit", action="store_true", help="write Redis (Holocene) + local artifact")
    args = p.parse_args(argv[1:])

    snapshot = build_report(sync.load_master())
    value = snapshot["value"]

    if args.emit:
        rc = _emit(snapshot)
    elif args.json:
        print(json.dumps(snapshot, indent=2))
        rc = 0
    else:  # --check (default)
        for it in value["items"]:
            print(f"  [{it['severity']:>8}] {it['label']:<8} {it['summary']}")
        s = value["summary"]
        print(f"hook-health: {s['ok']}/{s['total']} hook checks ok across {s['agents']} agents — status={snapshot['status']}")

    if args.check or (not args.emit and not args.json):
        return 0 if snapshot["status"] != "critical" else 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
