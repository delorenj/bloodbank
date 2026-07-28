#!/usr/bin/env python3
"""Propagate hooks.master.json (SSOT) → each agent's native hook implementation.

One source of truth — `hooks.master.json` — defines the canonical lifecycle
event catalog and, per agent, how that agent's *native* hook surface binds to
it. This tool maps and propagates the SSOT into every agent's unique
implementation:

  * <agent>/event_map.generated.json   — publisher's hook-arg → (ce_type, bucket)
  * claude/settings.hooks.json          — Claude settings fragment (merge target)
  * copilot/hooks.json                  — Copilot CLI hooks config
  * codex/hooks.json                    — Codex CLI hooks config

Ambiguous / divergent mappings (a contract-illegal type, or the same lifecycle
"role" bound to different types across agents) are resolved against
`hooks.mappings.lock.json`. Recorded resolutions are applied automatically so
the *next* sync is seamless; unrecorded ones are surfaced for interactive
resolution (`--resolve`) and then appended to the lock.

Modes:
    python3 sync.py --check          read-only; report; nonzero exit on drift/ambiguity
    python3 sync.py --check --json   machine-readable report (for agents/CI)
    python3 sync.py --apply          write generated artifacts (blocked on unresolved ambiguity)
    python3 sync.py --resolve        interactively resolve open ambiguities, append to lock
    python3 sync.py --apply --resolve  resolve then apply

Exit codes:
    0  clean (apply succeeded, or check found everything in sync)
    2  usage / load error
    3  unresolved ambiguities (need --resolve or a lock entry)
    4  generated artifacts are out of date (check only; run --apply)

Stdlib-only, like the publishers. Imports core.validate for contract checks.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SERVICE_DIR = Path(__file__).resolve().parent
MASTER_PATH = SERVICE_DIR / "hooks.master.json"
LOCK_PATH = SERVICE_DIR / "hooks.mappings.lock.json"
HOOKS_DIR = Path.home() / ".agents" / "hooks"
BLOODBANK_HOOK_LINK = HOOKS_DIR / "bloodbank"

if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from core.validate import (  # noqa: E402
    ContractViolation,
    _schema_path_for,
    assert_action_tense,
    assert_type_shape,
)

GENERATED_HEADER = {
    "_do_not_edit": "GENERATED from hooks.master.json by sync.py — run `mise run hooks:sync`.",
}


# --------------------------------------------------------------------------
# Load / save
# --------------------------------------------------------------------------


def _load_json(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def load_master() -> dict:
    if not MASTER_PATH.exists():
        _die(f"missing SSOT: {MASTER_PATH}")
    return _load_json(MASTER_PATH)


def load_lock() -> dict:
    if not LOCK_PATH.exists():
        return {"version": 1, "policy": {"on_unresolved": "prompt"}, "resolutions": {}}
    return _load_json(LOCK_PATH)


def save_lock(lock: dict) -> None:
    with LOCK_PATH.open("w") as f:
        json.dump(lock, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _die(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"hooks-sync: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------
# Resolution model
# --------------------------------------------------------------------------


def _resolutions(lock: dict) -> dict:
    return lock.get("resolutions", {})


def effective_type(binding: dict, lifecycle: dict, lock: dict) -> tuple[str, str]:
    """Resolve a binding to its (ce_type, ordering_bucket).

    A `role:<role>` entry in the lock overrides the binding's catalog type —
    this is what makes the lock authoritative and re-syncs seamless: any agent
    (even a newly added one) whose binding role is already decided gets the
    remembered resolution, regardless of which lifecycle key it references.
    """
    role = binding.get("role")
    res = _resolutions(lock).get(f"role:{role}")
    if res and res.get("resolution"):
        bucket = res.get("ordering_bucket")
        if not bucket:
            life = lifecycle.get(binding.get("lifecycle", ""), {})
            bucket = life.get("ordering_bucket", "invocation")
        return res["resolution"], bucket
    life = lifecycle.get(binding.get("lifecycle", ""))
    if not life:
        raise KeyError(
            f"binding role={role!r} references unknown lifecycle "
            f"{binding.get('lifecycle')!r}"
        )
    return life["type"], life["ordering_bucket"]


# --------------------------------------------------------------------------
# Ambiguity / drift detection
# --------------------------------------------------------------------------


def detect_ambiguities(master: dict, lock: dict) -> list[dict]:
    """Return open (unresolved) ambiguities. Empty list == clean."""
    lifecycle = master["lifecycle"]
    agents = master["agents"]
    ambiguities: list[dict] = []

    # (1) Contract-illegality of any catalog type that is actually emitted.
    for key, life in lifecycle.items():
        if life.get("emitted") is False:
            continue
        ce_type = life["type"]
        try:
            _, _, action = assert_type_shape(ce_type)
            assert_action_tense(action, life.get("kind", "event"))
        except ContractViolation as exc:
            if f"type:{ce_type}" in _resolutions(lock):
                continue
            ambiguities.append(
                {
                    "id": f"type:{ce_type}",
                    "kind": "illegal-type",
                    "detail": f"lifecycle {key!r} type {ce_type!r} violates contract: {exc}",
                    "candidates": [],
                }
            )

    # (2) Cross-agent divergence: same role, different *catalog* type, no lock entry.
    role_types: dict[str, dict[str, str]] = {}
    for agent_name, agent in agents.items():
        if agent.get("dialect") in ("watcher", "runtime"):
            continue
        for b in agent.get("bindings", []):
            role = b.get("role")
            life = lifecycle.get(b.get("lifecycle", ""))
            if not role or not life:
                continue
            role_types.setdefault(role, {})[agent_name] = life["type"]

    for role, per_agent in role_types.items():
        distinct = set(per_agent.values())
        if len(distinct) > 1 and f"role:{role}" not in _resolutions(lock):
            ambiguities.append(
                {
                    "id": f"role:{role}",
                    "kind": "divergent-role",
                    "detail": f"role {role!r} maps to {len(distinct)} types across agents: {per_agent}",
                    "candidates": sorted(distinct),
                }
            )

    # (3) Missing schema for any emitted, contract-legal type.
    seen: set[str] = set()
    for agent_name, agent in agents.items():
        if agent.get("dialect") in ("watcher", "runtime"):
            continue
        for b in agent.get("bindings", []):
            try:
                ce_type, _ = effective_type(b, lifecycle, lock)
            except KeyError:
                continue
            if ce_type in seen:
                continue
            seen.add(ce_type)
            try:
                assert_type_shape(ce_type)
            except ContractViolation:
                continue  # already reported in (1)
            if not _schema_path_for(ce_type).exists():
                ambiguities.append(
                    {
                        "id": f"schema:{ce_type}",
                        "kind": "missing-schema",
                        "detail": f"no schema file for {ce_type} at {_schema_path_for(ce_type)}",
                        "candidates": [],
                    }
                )
    return ambiguities


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def _runner(agent: dict) -> str:
    return (
        agent["runner"]
        .replace("{service_dir}", str(SERVICE_DIR))
        .replace("{hooks_dir}", str(HOOKS_DIR))
    )


def render_event_map(agent: dict, lifecycle: dict, lock: dict) -> dict:
    out: dict[str, Any] = dict(GENERATED_HEADER)
    table: dict[str, list[str]] = {}
    for b in agent.get("bindings", []):
        ce_type, bucket = effective_type(b, lifecycle, lock)
        table[b["arg"]] = [ce_type, bucket]
    out["map"] = table
    return out


def _command(agent: dict, b: dict, *, codex_empty_echo: bool) -> str:
    runner = _runner(agent)
    parts: list[str] = []
    payload = b.get("payload", "stdin")
    if payload == "stdin":
        parts.append("cat | ")
    elif payload == "empty" and codex_empty_echo:
        parts.append("echo '{}' | ")
    cmd = "".join(parts) + f"{runner} {b['arg']}"
    for extra in b.get("extra_args", []):
        cmd += f" {extra}"
    return cmd


def render_config(agent: dict, lifecycle: dict, lock: dict) -> dict | None:
    dialect = agent["dialect"]
    if dialect in ("watcher", "runtime"):
        return None
    if dialect == "hermes_config":
        # Hermes shell-hooks `hooks:` block: event -> [{command, timeout, matcher?}].
        # shell=False (shlex argv) — no pipes/$vars; payload is piped to stdin by hermes.
        hooks: dict[str, list] = {}
        for b in agent["bindings"]:
            entry: dict[str, Any] = {
                "command": f"{_runner(agent)} {b['arg']}",
                "timeout": agent.get("default_timeout", 5),
            }
            if b.get("matcher") is not None:
                entry["matcher"] = b["matcher"]
            hooks.setdefault(b["native"], []).append(entry)
        return {"hooks": hooks}
    if dialect == "copilot":
        hooks: dict[str, list] = {}
        for b in agent["bindings"]:
            hooks[b["native"]] = [
                {
                    "type": "command",
                    "bash": f"exec {_runner(agent)} {b['arg']}",
                    "timeoutSec": agent.get("default_timeout_sec", 5),
                }
            ]
        return {"version": 1, "hooks": hooks}
    if dialect in ("codex", "claude_settings"):
        codex_empty_echo = dialect == "codex"
        timeout = agent.get("default_timeout", 3000 if dialect == "codex" else 3)
        hooks = {}
        for b in agent["bindings"]:
            entry: dict[str, Any] = {}
            if b.get("matcher") is not None:
                entry["matcher"] = b["matcher"]
            entry["hooks"] = [
                {
                    "type": "command",
                    "command": _command(agent, b, codex_empty_echo=codex_empty_echo),
                    "timeout": timeout,
                }
            ]
            hooks[b["native"]] = [entry]
        return {"hooks": hooks}
    _die(f"unknown dialect {dialect!r} for agent")
    return None  # unreachable


def generate(master: dict, lock: dict) -> dict[Path, dict]:
    """Return {target_path: content_dict} for every artifact the SSOT produces."""
    lifecycle = master["lifecycle"]
    out: dict[Path, dict] = {}
    for agent_name, agent in master["agents"].items():
        if agent.get("event_map_target"):
            out[SERVICE_DIR / agent["event_map_target"]] = render_event_map(
                agent, lifecycle, lock
            )
        if agent.get("config_target"):
            cfg = render_config(agent, lifecycle, lock)
            if cfg is not None:
                out[SERVICE_DIR / agent["config_target"]] = cfg
    return out


def _serialize(content: dict) -> str:
    return json.dumps(content, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# Interactive resolution
# --------------------------------------------------------------------------


def resolve_interactive(master: dict, lock: dict, ambiguities: list[dict]) -> bool:
    """Prompt the operator for each open ambiguity; append to lock. Returns True if all resolved."""
    if not ambiguities:
        return True
    if not sys.stdin.isatty():
        print(
            "hooks-sync: unresolved ambiguities and no TTY for --resolve; "
            "resolve in hooks.mappings.lock.json or run interactively.",
            file=sys.stderr,
        )
        return False
    res = lock.setdefault("resolutions", {})
    for amb in ambiguities:
        print(f"\n[AMBIGUITY] {amb['id']}  ({amb['kind']})")
        print(f"  {amb['detail']}")
        cands = amb.get("candidates") or []
        for i, c in enumerate(cands, 1):
            print(f"    {i}) {c}")
        print("    or type a full ce_type / 'skip'")
        choice = input("  resolution> ").strip()
        if choice.lower() == "skip" or not choice:
            print("  (skipped)")
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(cands):
            chosen = cands[int(choice) - 1]
        else:
            chosen = choice
        rationale = input("  rationale> ").strip() or "operator decision"
        res[amb["id"]] = {
            "resolution": chosen,
            "strategy": "unify-to",
            "rationale": rationale,
            "decided_at": os.environ.get("HOOKS_SYNC_DATE", "manual"),
            "decided_by": "interactive",
        }
    save_lock(lock)
    print("\nhooks-sync: lock updated.")
    return len(detect_ambiguities(master, lock)) == 0


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_check(master: dict, lock: dict, as_json: bool) -> int:
    ambiguities = detect_ambiguities(master, lock)
    desired = generate(master, lock)
    stale: list[str] = []
    for path, content in desired.items():
        want = _serialize(content)
        have = path.read_text() if path.exists() else None
        if have != want:
            stale.append(str(path.relative_to(SERVICE_DIR)))

    # Soft check: publisher embedded fallback maps vs SSOT.
    drifted_fallbacks = _check_publisher_fallbacks(master, lock)

    if as_json:
        print(
            json.dumps(
                {
                    "ambiguities": ambiguities,
                    "stale_artifacts": stale,
                    "publisher_fallback_drift": drifted_fallbacks,
                    "clean": not ambiguities and not stale,
                },
                indent=2,
            )
        )
    else:
        if ambiguities:
            print(f"hooks-sync: {len(ambiguities)} UNRESOLVED ambiguity(ies):")
            for a in ambiguities:
                print(f"  - [{a['kind']}] {a['id']}: {a['detail']}")
        if stale:
            print(f"hooks-sync: {len(stale)} artifact(s) out of date (run --apply):")
            for s in stale:
                print(f"  - {s}")
        for d in drifted_fallbacks:
            print(f"hooks-sync: WARN publisher fallback drift: {d}")
        if not ambiguities and not stale:
            print("hooks-sync: OK — SSOT, lock, and all generated artifacts are in sync.")

    if ambiguities:
        return 3
    if stale:
        return 4
    return 0


def cmd_apply(master: dict, lock: dict, do_resolve: bool) -> int:
    ambiguities = detect_ambiguities(master, lock)
    if ambiguities:
        if do_resolve:
            if not resolve_interactive(master, lock, ambiguities):
                print("hooks-sync: ambiguities remain; not applying.", file=sys.stderr)
                return 3
            lock = load_lock()
        else:
            print(
                f"hooks-sync: {len(ambiguities)} unresolved ambiguity(ies); "
                "run with --resolve or add lock entries.",
                file=sys.stderr,
            )
            for a in ambiguities:
                print(f"  - [{a['kind']}] {a['id']}: {a['detail']}", file=sys.stderr)
            return 3

    desired = generate(master, lock)
    written = 0
    for path, content in desired.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        want = _serialize(content)
        if not path.exists() or path.read_text() != want:
            path.write_text(want)
            written += 1
            print(f"hooks-sync: wrote {path.relative_to(SERVICE_DIR)}")
    print(f"hooks-sync: apply complete ({written} file(s) changed, {len(desired)} total).")
    return 0


def _check_publisher_fallbacks(master: dict, lock: dict) -> list[str]:
    """Compare each publisher's embedded _DEFAULT_MAP to the SSOT-derived map."""
    drift: list[str] = []
    lifecycle = master["lifecycle"]
    for agent_name, agent in master["agents"].items():
        if agent.get("dialect") in ("watcher", "runtime"):
            continue
        mod_name = f"{agent_name}.publish"
        try:
            __import__(mod_name)
            mod = sys.modules[mod_name]
        except Exception:
            continue
        default_map = getattr(mod, "_DEFAULT_MAP", None)
        if not isinstance(default_map, dict):
            continue
        want = {}
        for b in agent.get("bindings", []):
            try:
                want[b["arg"]] = effective_type(b, lifecycle, lock)
            except KeyError:
                pass
        for arg, pair in want.items():
            got = default_map.get(arg)
            if got is not None and tuple(got) != tuple(pair):
                drift.append(f"{agent_name}:{arg} fallback={tuple(got)} ssot={tuple(pair)}")
    return drift


# --------------------------------------------------------------------------
# Install (deploy generated artifacts to each agent's live location)
# --------------------------------------------------------------------------


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def _ensure_bloodbank_hook_link() -> int:
    """Ensure ~/.agents/hooks/bloodbank resolves to this service directory."""
    try:
        HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"hooks-sync: WARN cannot create {HOOKS_DIR}: {exc}")
        return 0

    if BLOODBANK_HOOK_LINK.is_symlink():
        try:
            current = BLOODBANK_HOOK_LINK.resolve(strict=True)
        except OSError:
            current = Path(os.readlink(BLOODBANK_HOOK_LINK))
            if not current.is_absolute():
                current = (BLOODBANK_HOOK_LINK.parent / current).resolve()
        if current == SERVICE_DIR:
            print(f"hooks-sync: bloodbank hook link up to date ({BLOODBANK_HOOK_LINK})")
            return 0
        print(
            "hooks-sync: WARN "
            f"{BLOODBANK_HOOK_LINK} points to {current}, expected {SERVICE_DIR}; skipping"
        )
        return 0

    if BLOODBANK_HOOK_LINK.exists():
        try:
            current = BLOODBANK_HOOK_LINK.resolve()
        except OSError:
            current = BLOODBANK_HOOK_LINK
        if current == SERVICE_DIR:
            print(f"hooks-sync: bloodbank hook path up to date ({BLOODBANK_HOOK_LINK})")
            return 0
        print(
            "hooks-sync: WARN "
            f"{BLOODBANK_HOOK_LINK} exists and is not the Bloodbank hook link; skipping"
        )
        return 0

    try:
        BLOODBANK_HOOK_LINK.symlink_to(SERVICE_DIR, target_is_directory=True)
    except OSError as exc:
        print(f"hooks-sync: WARN cannot link {BLOODBANK_HOOK_LINK}: {exc}")
        return 0
    print(f"hooks-sync: linked {BLOODBANK_HOOK_LINK} -> {SERVICE_DIR}")
    return 1


def _publisher_markers(name: str, agent: dict) -> list[str]:
    """Return canonical + legacy command substrings that identify our hook."""
    raw: list[Any] = [agent.get("publisher", f"{name}/publish.py")]
    raw.extend(agent.get("legacy_publishers", []))
    raw.append(f"{name}/publish.py")
    markers: list[str] = []
    for marker in raw:
        if isinstance(marker, str) and marker and marker not in markers:
            markers.append(marker)
    return markers


def _has_marker(command: object, markers: list[str]) -> bool:
    text = str(command or "")
    return any(marker in text for marker in markers)


def _merge_hooks(live: dict, generated_hooks: dict, markers: list[str]) -> dict:
    """Update our publisher hook at INNER-hook granularity.

    The bloodbank publisher hook may be its own group OR nested among foreign
    sibling hooks inside a single group's ``hooks`` list (e.g. Claude's Stop
    group holds hindsight + git-checkpoint + publish + notify). We update ONLY
    our inner hook's ``command``/``timeout`` in place — never touching foreign
    hooks, groups, or matchers. If our hook is absent for an event, append the
    generated group(s) that carry it.
    """
    hooks = live.setdefault("hooks", {})
    for event, gen_groups in generated_hooks.items():
        gen_bb = [
            h
            for g in gen_groups
            for h in g.get("hooks", [])
            if _has_marker(h.get("command", ""), markers)
        ]
        if not gen_bb:
            continue
        groups = hooks.setdefault(event, [])
        live_bb = [
            (g, h)
            for g in groups
            for h in g.get("hooks", [])
            if _has_marker(h.get("command", ""), markers)
        ]
        if live_bb:
            _, lh = live_bb[0]
            gh = gen_bb[0]
            lh["command"] = gh["command"]
            if "timeout" in gh:
                lh["timeout"] = gh["timeout"]
            for g, h in live_bb[1:]:  # drop any duplicate publisher hooks
                if h in g.get("hooks", []):
                    g["hooks"].remove(h)
            hooks[event] = [g for g in groups if g.get("hooks")]
        else:
            groups.extend(gen_groups)
    return live


def _norm(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(path.name + f".bak-{stamp}")
    shutil.copy2(path, bak)
    return bak


def _splice_top_level_block(raw: str, key: str, block_yaml: str) -> str:
    """Replace/insert the top-level ``key:`` block in raw YAML text, preserving
    every other byte (comments, scalar styles, key order) verbatim.

    A whole-document safe_load→safe_dump round-trip would coerce YAML 1.1 scalars
    (on/off/yes/no → true/false) and strip comments in unrelated keys; splicing
    only our block avoids touching operator content.
    """
    block_yaml = block_yaml if block_yaml.endswith("\n") else block_yaml + "\n"
    if not raw.strip():
        return block_yaml
    lines = raw.splitlines(keepends=True)
    prefix = key + ":"
    start = next((i for i, ln in enumerate(lines) if ln.startswith(prefix)), None)
    if start is None:  # append as a new top-level key
        sep = "" if raw.endswith("\n") else "\n"
        return raw + sep + block_yaml
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.strip() == "":
            continue
        if not ln[:1].isspace():  # next top-level key or column-0 comment
            end = j
            break
    return "".join(lines[:start]) + block_yaml + "".join(lines[end:])


def _install_hermes_one(
    label: str,
    cfg_path: Path,
    alw_path: Path,
    gen_hooks: dict,
    markers: list[str],
) -> tuple[int, str]:
    """Merge the hooks: block into one agent's config.yaml + seed its allowlist.

    Returns (files_changed, status) where status is 'changed' | 'ok' | a warn string.
    Surgical: replaces only our entries (publisher substring) per event, preserving
    the operator's other config keys and any foreign hooks; backs up on real change.
    """
    import yaml  # caller guarantees availability

    changed = 0
    # 1) config.yaml `hooks:` block
    raw = cfg_path.read_text() if cfg_path.exists() else ""
    if raw:
        try:
            cfg = yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            return 0, "WARN unreadable config.yaml"
    else:
        cfg = {}
    if not isinstance(cfg, dict):
        return 0, "WARN config.yaml not a mapping"
    block = cfg.get("hooks")
    if block is None:  # absent OR present-but-null `hooks:` placeholder
        block = {}
    elif not isinstance(block, dict):
        return 0, "WARN hooks: not a mapping"
    old_block = copy.deepcopy(block)
    for event, entries in gen_hooks.items():
        existing = block.get(event)
        if existing is None:
            existing = []
        elif not isinstance(existing, list):
            return 0, f"WARN hooks.{event}: not a list"
        kept = [
            e
            for e in existing
            if isinstance(e, dict)
            and not _has_marker((e or {}).get("command", ""), markers)
        ]
        kept += [e for e in existing if not isinstance(e, dict)]  # preserve foreign non-dict entries verbatim
        kept.extend(entries)
        block[event] = kept
    if not cfg_path.exists() or _norm(block) != _norm(old_block):
        # Splice ONLY the `hooks:` block into the raw text — never round-trip the
        # whole operator document (that would coerce on/off/yes/no scalars and
        # strip comments in unrelated keys). The hooks block is entirely ours.
        block_yaml = yaml.safe_dump({"hooks": block}, sort_keys=False, allow_unicode=True)
        new_text = _splice_top_level_block(raw, "hooks", block_yaml)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        if cfg_path.exists():
            _backup(cfg_path)
        cfg_path.write_text(new_text)
        changed += 1

    # 2) allowlist (exact event+command match required by hermes)
    try:
        alw = json.loads(alw_path.read_text()) if alw_path.exists() else {"approvals": []}
    except (OSError, json.JSONDecodeError):
        alw = {"approvals": []}
    approvals = alw.setdefault("approvals", [])
    have = {(e.get("event"), e.get("command")) for e in approvals if isinstance(e, dict)}
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    added = 0
    for event, entries in gen_hooks.items():
        for e in entries:
            cmd = e.get("command")
            if cmd and (event, cmd) not in have:
                approvals.append({"event": event, "command": cmd, "approved_at": stamp,
                                  "approved_by": "bloodbank-hooks-deploy"})
                have.add((event, cmd))
                added += 1
    if added or not alw_path.exists():
        alw_path.parent.mkdir(parents=True, exist_ok=True)
        if alw_path.exists():
            _backup(alw_path)
        with alw_path.open("w") as f:
            json.dump(alw, f, indent=2, sort_keys=True)
            f.write("\n")
        changed += 1
    return changed, ("changed" if changed else "ok")


def _install_hermes_fleet(name: str, agent: dict) -> int:
    """Fleet-wide hermes install: read the registry and deploy into every agent.

    Each agent runs with HERMES_HOME=<role_dir>/<runtime_subdir>, so its shell-hooks
    config + allowlist live there. Uninitialized runtimes (no dir) are skipped;
    missing config.yaml is created.
    """
    try:
        import yaml
    except ImportError:
        print(f"hooks-sync: WARN {name}: PyYAML unavailable — cannot deploy hermes fleet.")
        return 0
    src = SERVICE_DIR / agent["config_target"]
    if not src.exists():
        print(f"hooks-sync: WARN {name}: generated {src} missing; run --apply first")
        return 0
    gen_hooks = _load_json(src).get("hooks", {})
    markers = _publisher_markers(name, agent)
    runtime_subdir = agent.get("runtime_subdir", "runtime")
    alw_name = agent.get("allowlist_filename", "shell-hooks-allowlist.json")

    targets: list[tuple[str, Path]] = []
    reg = agent.get("fleet_registry")
    if reg:
        reg_path = _expand(reg)
        if reg_path.exists():
            try:
                rd = yaml.safe_load(reg_path.read_text()) or {}
            except yaml.YAMLError:
                rd = {}
            for aid, meta in (rd.get("agents") or {}).items():
                role_dir = (meta or {}).get("role_dir")
                if role_dir:
                    targets.append((aid, Path(role_dir) / runtime_subdir))
        else:
            print(f"hooks-sync: WARN {name}: fleet registry {reg_path} not found")
    if agent.get("live_target"):  # optional explicit extra target
        targets.append(("(explicit)", _expand(agent["live_target"]).parent))

    if not targets:
        print(f"hooks-sync: WARN {name}: no fleet targets discovered")
        return 0

    total_changed = 0
    installed = skipped = up_to_date = warned = 0
    for aid, runtime in targets:
        if not runtime.is_dir():
            skipped += 1
            continue
        try:
            c, status = _install_hermes_one(
                aid,
                runtime / "config.yaml",
                runtime / alw_name,
                gen_hooks,
                markers,
            )
        except Exception as exc:  # one bad/locked agent must not abort the fleet
            warned += 1
            print(f"hooks-sync:   {aid}: WARN install error: {exc!r} ({runtime})")
            continue
        total_changed += c
        if status.startswith("WARN"):
            warned += 1
            print(f"hooks-sync:   {aid}: {status} ({runtime})")
        elif c:
            installed += 1
            print(f"hooks-sync:   {aid}: installed ({runtime})")
        else:
            up_to_date += 1
    print(
        f"hooks-sync: hermes fleet — {len(targets)} agent(s): "
        f"{installed} installed, {up_to_date} up-to-date, {skipped} skipped (uninitialized), {warned} warned"
    )
    return total_changed


def cmd_install(master: dict) -> int:
    """Deploy each agent's generated config to its live_target.

    copilot         → symlink live_target → repo config_target
    claude/codex    → surgical JSON merge of our entries into the live file
                      (replace-in-place; preserve order and all foreign hooks),
                      backing up the live file only when content actually changes.
    hermes_config   → fleet-wide: merge hooks: + seed allowlist into every agent
                      in the registry (see _install_hermes_fleet).
    watcher/runtime → skipped (no hook-config surface).
    """
    changed = _ensure_bloodbank_hook_link()
    for name, agent in master["agents"].items():
        dialect = agent.get("dialect")
        cfg = agent.get("config_target")
        if dialect == "hermes_config":
            if cfg:
                changed += _install_hermes_fleet(name, agent)
            continue
        live = agent.get("live_target")
        if not live or not cfg:
            if dialect in ("watcher", "runtime"):
                print(f"hooks-sync: {name} ({dialect}) — no live config to install (skip)")
            continue
        src = SERVICE_DIR / cfg
        dest = _expand(live)
        if not src.exists():
            print(f"hooks-sync: WARN {name}: generated {src} missing; run --apply first")
            continue

        if dialect == "copilot":
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_symlink() and Path(os.readlink(dest)).resolve() == src.resolve():
                print(f"hooks-sync: {name} symlink up to date ({dest})")
                continue
            if dest.exists() or dest.is_symlink():
                dest.unlink()
            dest.symlink_to(src)
            changed += 1
            print(f"hooks-sync: {name} linked {dest} -> {src}")
            continue

        if dialect in ("claude_settings", "codex"):
            markers = _publisher_markers(name, agent)
            gen_hooks = _load_json(src).get("hooks", {})
            if dest.exists():
                try:
                    liveobj = _load_json(dest)
                except (OSError, json.JSONDecodeError):
                    print(f"hooks-sync: WARN {name}: {dest} unreadable JSON; skipping")
                    continue
            else:
                liveobj = {}
            original = copy.deepcopy(liveobj)
            merged = _merge_hooks(liveobj, gen_hooks, markers)
            if dest.exists() and _norm(merged) == _norm(original):
                print(f"hooks-sync: {name} {dest} up to date")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                bak = _backup(dest)
                print(f"hooks-sync: {name} backed up {dest} -> {bak.name}")
            with dest.open("w") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
                f.write("\n")
            changed += 1
            print(f"hooks-sync: {name} installed into {dest}")
            continue

        print(f"hooks-sync: {name}: unknown install dialect {dialect!r} (skip)")
    print(f"hooks-sync: install complete ({changed} live target(s) changed).")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Propagate hooks.master.json to all agents.")
    p.add_argument("--check", action="store_true", help="read-only drift/ambiguity report")
    p.add_argument("--apply", action="store_true", help="write generated artifacts")
    p.add_argument("--install", action="store_true", help="deploy generated configs to live agent locations")
    p.add_argument("--resolve", action="store_true", help="interactively resolve open ambiguities")
    p.add_argument("--json", action="store_true", help="machine-readable output (with --check)")
    args = p.parse_args(argv[1:])

    master = load_master()
    lock = load_lock()

    if args.resolve and not args.apply:
        amb = detect_ambiguities(master, lock)
        resolve_interactive(master, lock, amb)
        return cmd_check(master, load_lock(), args.json)
    if args.apply:
        rc = cmd_apply(master, lock, args.resolve)
        if rc != 0:
            return rc
        if args.install:
            return cmd_install(master)
        return 0
    if args.install:
        return cmd_install(master)
    # default: check
    return cmd_check(master, lock, args.json)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
