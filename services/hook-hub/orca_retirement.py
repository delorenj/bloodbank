#!/usr/bin/env python3
"""Retire proven redundant Orca registrations only; default is read-only JSON.

Unmapped events, permission owners, unknown command revisions and missing
replacements are retained. No profiles, source generators or services mutate.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tomllib

SERVICE = Path(__file__).resolve().parent
PROTECTED = {"claude": {"PreToolUse"}, "antigravity": {"PreToolUse"}}
# Native Copilot spelling -> the event name previously supplied by Orca's
# registration. This is protocol translation, not a second lifecycle catalog.
COPILOT_NATIVE: dict[str, str] = dict(zip(
    ("sessionStart", "sessionEnd", "userPromptSubmitted", "preToolUse", "postToolUse",
     "postToolUseFailure", "subagentStart", "subagentStop", "agentStop", "errorOccurred",
     "permissionRequest", "notification"),
    ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
     "PostToolUseFailure", "subagentStart", "SubagentStop", "Stop", "ErrorOccurred",
     "PermissionRequest", "Notification")))
# Reviewed native command fingerprints, with the actual home replaced by {home}.
# Unknown revisions are inventoried but NEVER removed by substring matching.
KNOWN = {
    "claude": {"717d3f74c8f03b7d48da8a24da71aa7283b52cfe5c8ca901c89b4a467449bc47", "ea2ada15b7b273a20b5815bbd7339469e9e8109383a6f4b6f06b24e253afdc8e"},
    "codex": {"3da389906382108f31af765cd0d0f5af2b493341bbca806c7694eec03bc4ab82"},
    "gemini": {"d8a27d59b1ca3a47be2ceb40a0fb4324a97703789d003b8bef1978b0a96051d8"},
    "kimi": {"a224503bd05c9d4a691ad6bb1430207985bfa328f9807025534743b337bb61dd"},
    "copilot": {
        "17c9d6c4c0e4bad7969b96dc98376f2fdf9f40cc8f920e70f09d6ffad0934cd8", "3b6afa25f0fe068bffc95c4264564590fa36f6be4fc99599320d66b4ab3bd6a7",
        "4f284b15a0cb0fdba49bd1a6aae208a30c6498dcbf8230eabe60cbd74a9e76bc", "55350629f3dcd41f57f62e0ae91ada41fff69c85e47fa22f93fb4441676acd87",
        "5d8dd2bfb6775a4e6ecc7e412f4b0b6442a08ca042160186e47de59fb11573ad", "7e93e2cd0cde720f34e2e02f093dd08c0a0d7313e4249be1cd290b21343b6340",
        "803a58266d54df738fffca2ba419ccfab09b3cffe6084939811529698e652261", "994a6601bf2d26ca640fc5de9f8197370c5c50ffb4bcc02a701b99ba64277239",
        "9ad71fd5ff22678df50f3d6c2bf68e117bc5655643793516e81570ab8ffe89e1", "a5964e6bd220f669cb0a8c816bc7a2aebcd8cb05ee800d1df8671fae763d1527",
        "bdcaee1fe80c58be7635e2d310574fa9370d39710c9ce126918263b352abab82", "df0701e3a1e7efab008317f1865ab82567a8387b94facce33c2364d5a4a604f4",
        "e7a740fd4f73a7711f90d544f3071de2c5a9e3949f827ff3fa13bbfe58072fb1"},
    "antigravity": {
        "4f3befc44c4e462eda4144814000a9a7514ffd95a65b04559d825a43293699fd", "8980f9612d35afe8662a413ab9dc15efc2674f2eff772c8cd2a963f8a7851c5d",
        "b2782ba96cff38e5dab36e7be619fb13c30203a6d49fbb83cf609068d4608e6e", "d551594ea795ed213c1cd4fc455bfc465aa40243200b0cc3631bab09bf9f5eb5",
        "e488e621b5470c0adfa5a1134927ba996739b16dea39ac9d797880a5b716fd71"},
}
TARGETS = {
    "claude": (".claude/settings.json",), "codex": (".codex/hooks.json",),
    "copilot": (".copilot/hooks/orca.json", ".copilot/hooks/bloodbank.json"),
    "gemini": (".gemini/settings.json",), "antigravity": (".gemini/config/hooks.json",),
    "kimi": (".kimi-code/config.toml",),
}


def fingerprint(command: str, home: Path) -> str:
    return hashlib.sha256(command.replace(str(home), "{home}").encode()).hexdigest()


def leaves(value, native="", matchers=()):
    """Yield inner commands plus inherited matcher scope, without flattening it."""
    if isinstance(value, list):
        for item in value:
            yield from leaves(item, native, matchers)
    elif isinstance(value, dict):
        scopes = matchers + ((value["matcher"],) if "matcher" in value else ())
        if "command" in value or "bash" in value:
            yield native, value, scopes
        for key, item in value.items():
            if isinstance(item, (list, dict)):
                yield from leaves(item, native if key == "hooks" else key, scopes)


def prune(value, remove_ids):
    if isinstance(value, list):
        result = []
        for item in value:
            if id(item) in remove_ids:
                continue
            updated = prune(item, remove_ids)
            if isinstance(item, dict) and item.get("hooks") and isinstance(updated, dict) and updated.get("hooks") == []:
                continue
            result.append(updated)
        return result
    if isinstance(value, dict):
        return {key: prune(item, remove_ids) for key, item in value.items()}
    return copy.deepcopy(value)


def strip_kimi(raw: str, indexes: set[int]) -> str:
    # Only accept a layout whose parsed hook rows map one-to-one to text spans.
    # Indented headers are legal TOML; never index a narrower regex by parsed row.
    doc = tomllib.loads(raw)
    headers = list(re.finditer(r"(?m)^[ \t]*\[\[hooks\]\][ \t]*(?:\#[^\n]*)?(?:\n|$)", raw))
    if len(headers) != len(doc.get("hooks", [])) or any(i < 0 or i >= len(headers) for i in indexes):
        raise ValueError("unsupported_kimi_hook_layout")
    # Match only literal table headers: a TOML string can contain [[hooks]].
    tables = list(re.finditer(r"(?m)^[ \t]*\[(?:\[)?[A-Za-z0-9_.-]+\](?:\])?[ \t]*(?:\#[^\n]*)?(?:\n|$)", raw))
    edits = []
    for index in indexes:
        start = headers[index].start()
        following = next((table for table in tables if table.start() > start), None)
        end = following.start() if following else len(raw)
        if following and re.match(r"[ \t]*\[hooks\.", following.group()):
            raise ValueError("nested_kimi_hook_table")
        edits.append((start, end))
    for start, end in sorted(edits, reverse=True):
        raw = raw[:start] + raw[end:]
    updated = tomllib.loads(raw)
    expected = dict(doc)
    expected["hooks"] = [row for i, row in enumerate(doc.get("hooks", [])) if i not in indexes]
    if updated != expected:
        raise ValueError("unsupported_kimi_hook_layout")
    return raw


def load_config(path: Path):
    raw = path.read_text()
    return raw, tomllib.loads(raw) if path.suffix == ".toml" else json.loads(raw)


def plan(home: Path, config, codex_before=None):
    """Return payload-free inventory and a separately held in-memory write set."""
    report, changes = [], {}
    for cli, relatives in TARGETS.items():
        docs = {home / rel: load_config(home / rel) for rel in relatives if (home / rel).is_file()}
        all_leaves = []
        for path, (_, doc) in docs.items():
            if cli == "kimi":
                all_leaves.extend((path, row.get("event", ""), row,
                                   (row["matcher"],) if "matcher" in row else ()) for row in doc.get("hooks", []))
            else:
                all_leaves.extend((path, native, row, scopes) for native, row, scopes in leaves(doc.get("hooks", doc)))
        for path, (raw, doc) in docs.items():
            removals, indexes = set(), set()
            for source, native, row, _ in all_leaves:
                command = row.get("command", row.get("bash", ""))
                if source != path or not isinstance(command, str) or ".orca/agent-hooks/" not in command:
                    continue
                target = next((key for key, value in COPILOT_NATIVE.items() if value == native), native) if cli == "copilot" else native
                binding = config.bindings.get((cli, target))
                reason = None
                if fingerprint(command, home) not in KNOWN[cli]:
                    reason = "unknown_command_revision"
                elif native in PROTECTED.get(cli, set()):
                    reason = "native_permission_owner"
                elif not binding:
                    reason = "no_master_binding"
                elif not any(h.id == "orca-status" for h in config.select(binding.get("role"), target, {}, {}, cli=cli)):
                    reason = "handler_not_selected"
                replacements = [(p, r) for p, n, r, scope in all_leaves
                                if n == target and all(s in ("", "*", ".*") for s in scope)
                                and r.get("enabled", True) is not False
                                and is_central(r.get("command", r.get("bash", "")), cli, target, home)]
                if not reason and not replacements:
                    reason = "no_unconditional_native_replacement"
                if not reason and cli == "codex":
                    trusted = codex_before and any(
                        h.get("sourcePath") == str(path)
                        and h.get("command") in [r.get("command") for _, r in replacements]
                        and h.get("enabled") is True and h.get("trustStatus") == "trusted"
                        for h in codex_before["hooks"])
                    if not trusted:
                        reason = "codex_native_trust_unproved"
                adapter = home / ".orca/agent-hooks" / f"{cli}-hook.sh"
                if not reason and not (adapter.is_file() and os.access(adapter, os.X_OK)):
                    reason = "orca_adapter_missing"
                if not reason and doc.get("disableAllHooks") is True:
                    reason = "native_hooks_disabled"
                report.append({"path": str(path), "cli": cli, "native": native, "replacement_native": target,
                               "role": binding.get("role") if binding else None,
                               "event_type": binding.get("event_type") if binding else None,
                               "action": "retain" if reason else "retire", "reason": reason or "central_adapter_coverage"})
                if not reason:
                    removals.add(id(row))
                    if cli == "kimi":
                        indexes.add(next(i for i, item in enumerate(doc["hooks"]) if item is row))
            if removals:
                if path.is_symlink():
                    raise ValueError("refuse_symlink_write:" + str(path))
                updated = strip_kimi(raw, indexes) if cli == "kimi" else json.dumps(prune(doc, removals), indent=2, ensure_ascii=False) + "\n"
                changes[path] = (raw, updated)
    return report, changes


def is_central(command, cli, native, home):
    if not isinstance(command, str):
        return False
    # Match the generated command, not a substring in an arbitrary shell chain.
    command = command.replace(str(home), "$HOME")
    expected = f"$HOME/.agents/hooks/bb-hook --cli {cli} --native {native}"
    return bool(re.fullmatch(r"(?:exec )?" + re.escape(expected) + r"(?: --deadline 15)?(?: --trailer (?:passive|stop))?", command))


def effective_leaves(value, native="", scopes=(), enabled=True):
    if isinstance(value, list):
        for item in value:
            yield from effective_leaves(item, native, scopes, enabled)
    elif isinstance(value, dict):
        enabled = enabled and value.get("enabled", True) is not False
        scopes = scopes + ((value["matcher"],) if "matcher" in value else ())
        if "command" in value or "bash" in value:
            yield native, value, scopes, enabled
        for key, item in value.items():
            if isinstance(item, (list, dict)):
                yield from effective_leaves(item, native if key == "hooks" else key, scopes, enabled)


def direct_owner_present(home: Path, cli: str, native: str, codex_snapshot=None) -> bool:
    """Check effective direct ownership at native intake, not at async execution."""
    legacy_native = COPILOT_NATIVE.get(native, native) if cli == "copilot" else native
    # The native loader, not raw command text, decides whether Codex fires.
    # Loader errors fail closed, retaining a possible direct owner.
    try:
        if cli == "codex":
            if codex_snapshot is None:
                sys.path.insert(0, str(SERVICE.parent / "agent-hooks"))
                from codex_native import capture_trust
                codex_snapshot = capture_trust(home / ".codex/config.toml")
            source = str(home / ".codex/hooks.json")
            return any(h.get("sourcePath") == source and isinstance(h.get("eventName"), str)
                       and h["eventName"].lower() == native.lower()
                       and isinstance(h.get("command"), str) and ".orca/agent-hooks/" in h["command"]
                       and h.get("enabled") is True and h.get("trustStatus") == "trusted"
                       for h in codex_snapshot["hooks"])
        for rel in TARGETS.get(cli, ()):
            path = home / rel
            if not path.exists():
                continue
            _, doc = load_config(path)
            if doc.get("disableAllHooks") is True:
                continue
            rows = ((r.get("event"), r, (r.get("matcher", ""),), r.get("enabled", True) is not False) for r in doc.get("hooks", [])) if cli == "kimi" else effective_leaves(doc.get("hooks", doc))
            for event, row, scopes, enabled in rows:
                command = row.get("command", row.get("bash", ""))
                if (event in {native, legacy_native} and enabled
                        and all(s in ("", "*", ".*") for s in scopes)
                        and isinstance(command, str) and ".orca/agent-hooks/" in command):
                    return True
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, TimeoutError):
        return True  # Unknown native state is not permission to add a new owner.
    return False


def source_ready(home: Path):
    if os.environ.get("HOOK_HUB_REGISTRY") and Path(os.environ["HOOK_HUB_REGISTRY"]).resolve() != (SERVICE / "handlers.toml").resolve():
        raise ValueError("noncanonical_registry")
    if (home / ".agents/hooks/bloodbank/hooks.master.json").read_bytes() != (SERVICE.parent / "agent-hooks/hooks.master.json").read_bytes():
        raise ValueError("installed_master_differs")
    for rel in ("hub.py", "concerns.py", "handlers.toml", "orca_retirement.py"):
        if (home / ".agents/hooks/hub" / rel).read_bytes() != (SERVICE / rel).read_bytes():
            raise ValueError("installed_source_differs:" + rel)
    client = home / ".agents/hooks/bb-hook"
    if client.read_bytes() != (SERVICE / "client/bb-hook").read_bytes():
        raise ValueError("installed_client_differs")
    if not client.is_file() or not os.access(client, os.X_OK):
        raise ValueError("installed_client_not_executable")
    first_line = client.open("rb").readline().decode("utf-8", "replace").strip()
    if not first_line.startswith("#!"):
        raise ValueError("installed_client_interpreter_missing")
    interpreter = first_line[2:].split()[0]
    if interpreter == "/usr/bin/env":
        args = first_line[2:].split()[1:]
        interpreter = shutil.which(args[0]) if args else None
    if not interpreter or not Path(interpreter).is_file() or not os.access(interpreter, os.X_OK):
        raise ValueError("installed_client_interpreter_missing")


def assert_quiescent(home: Path, changes: dict):
    """Cutover cannot atomically remove hooks from already-running native CLIs.

    Require the operator to drain every affected client and accepted hub work;
    a fresh preview is needed after that boundary. A process check is a guard,
    not a substitute for stopping launches across the apply window.
    """
    if not changes:
        return
    affected = {cli for cli, relatives in TARGETS.items() for rel in relatives
                if home / rel in changes}
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    state_root = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
    sockets = {runtime / "33god/hook-hub.sock", state_root / "33god/hook-hub.sock",
               Path(f"/run/user/{os.getuid()}/33god/hook-hub.sock")}
    state = state_root / "33god/hook-hub/receipts.sqlite3"
    state = Path(os.environ.get("HOOK_HUB_RECEIPTS", state))
    if any(path.exists() for path in sockets):
        # Stopping just the service leaves the socket-activated listener alive.
        raise ValueError("hub_socket_must_be_stopped_for_cutover")
    if state.exists():
        try:
            with sqlite3.connect(state) as db:
                pending = db.execute("SELECT COUNT(*) FROM executions WHERE status IN ('selected','started') AND handler_id='orca-status'").fetchone()[0]
        except sqlite3.Error as exc:
            raise ValueError("orca_receipts_unreadable") from exc
        if pending:
            raise ValueError("orca_work_not_drained")
    # Native hooks can be cached by processes whose command line does not
    # reveal the CLI; the explicit operator drain attestation remains required.
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.joinpath("status").read_text().split("Uid:\t", 1)[1].split()[0] != str(os.getuid()):
                continue
            argv = entry.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except (OSError, IndexError):
            continue
        if re.search(r"(?:^|[ /])(?:claude|codex|copilot|kimi|gemini|antigravity)(?:\s|$)", argv) and any(cli in argv for cli in affected):
            raise ValueError("native_clients_must_be_drained")


def apply_changes(changes, backup_dir, codex_before=None):
    """Preflight all files, back up outside config roots, then roll back on error."""
    from cutover import write
    for path, (raw, _) in changes.items():
        if path.is_symlink() or path.read_text() != raw:
            raise ValueError("config_changed_since_preview:" + str(path))
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    originals = {path: raw for path, (raw, _) in changes.items()}
    codex_path = next((p for p in changes if p.parent.name == ".codex"), None)
    trust_path = codex_path.parent / "config.toml" if codex_path else None
    if trust_path and trust_path.is_symlink():
        raise ValueError("refuse_symlink_trust_write")
    if codex_path and not codex_before:
        raise ValueError("codex_trust_snapshot_required")
    if trust_path and trust_path.exists():
        originals[trust_path] = trust_path.read_text()
    for index, (path, raw) in enumerate(originals.items()):
        backup = backup_dir / f"{index}-{path.name}"
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(raw)
    written = []
    try:
        for path, (raw, updated) in changes.items():
            if path.is_symlink() or path.read_text() != raw:
                raise ValueError("config_changed_during_apply:" + str(path))
            write(path, updated)
            written.append(path)
        if codex_path:
            from codex_native import reconcile_trust
            if not codex_before:
                raise ValueError("codex_trust_snapshot_required")
            reconcile_trust(codex_before, codex_path)
    except Exception:
        for path in written:
            write(path, originals[path])
        if trust_path and trust_path in originals:
            write(trust_path, originals[trust_path])
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home(), help="preview another home; apply is current-user only")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--drained-clients", action="store_true", help="operator attests affected native CLI sessions are closed and launches paused")
    parser.add_argument("--backup-dir", type=Path, help="new private directory under TMPDIR, required for apply")
    args = parser.parse_args()
    try:
        from hub import Config
        config = Config()
        config._load()
        before = None
        if args.home.resolve() == Path.home().resolve():
            sys.path.insert(0, str(SERVICE.parent / "agent-hooks"))
            from codex_native import capture_trust
            if (args.home / ".codex/hooks.json").exists():
                before = capture_trust(args.home / ".codex/config.toml")
        report, changes = plan(args.home, config, before)
        ready_error = None
        try:
            source_ready(args.home)
        except (OSError, ValueError) as exc:
            ready_error = str(exc) if isinstance(exc, ValueError) else "installed_source_missing"
        if args.apply:
            if args.home.resolve() != Path.home().resolve():
                raise ValueError("apply_requires_current_user_home")
            if ready_error:
                raise ValueError(ready_error)
            if changes and not args.drained_clients:
                raise ValueError("explicit_drained_clients_required")
            assert_quiescent(args.home, changes)
            scratch = Path(os.environ["TMPDIR"]).resolve()
            if not args.backup_dir or not args.backup_dir.resolve().is_relative_to(scratch):
                raise ValueError("backup_dir_must_be_new_and_under_TMPDIR")
            if changes:
                apply_changes(changes, args.backup_dir, before)
        print(json.dumps({"applied": args.apply, "activation_blocker": ready_error,
                          "retire": sum(r["action"] == "retire" for r in report),
                          "retained": sum(r["action"] == "retain" for r in report),
                          "inventory": report}, indent=2))
        return 0
    except Exception as exc:
        # Do not echo raw native RPC errors, shell commands or config values.
        print(json.dumps({"error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
