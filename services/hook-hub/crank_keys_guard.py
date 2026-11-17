#!/usr/bin/env python3
"""Crank-keys guard: an autonomous Hermes agent types into a zellij pane only through zcrank-send.

A standalone hook-hub handler (sync, pre_tool, Hermes only). Crank, the 33god-pm crank beat,
and the PMs that drive Workspace panes used to improvise `zellij action write-chars` batches.
Their text reached pane agents untagged, indistinguishable from the operator's typing; on
2026-11-11 one such send (`name 707163 Laura`) renamed the operator's wife across 70
transcripts. `~/.local/bin/zcrank-send` tags every message `[crank]` and checks the screen
before and after typing. This guard denies the bypass: a terminal or code tool call whose text
drives zellij's `write-chars`, `write` or `paste` actions. zcrank-send's own zellij calls are
subprocesses of that tool, not tool calls, so they never reach this guard.

Read-only zellij use (dump-screen, list-tabs, send-keys) is untouched. Claude Code, Codex and
the other CLIs are not guarded: there the operator is at the keyboard and drives panes himself.

Like native_clock_guard: a deny exits 2 with the reason on stderr, and the guard fails open on
any error of its own. Stdlib only; run with /usr/bin/python3.
"""
from __future__ import annotations

import json
import os
import re
import sys

ZELLIJ = re.compile(r"\bzellij\b")
# `action write-chars`, `'write-chars'` in an argv list, `b + 'write-chars ...'`, `action paste`,
# and zellij's raw-bytes `write` action (only when it follows `action`, the word is too common).
TYPING = re.compile(r"(?<![\w-])write-chars(?![\w-])"
                    r"|\baction['\",\s]+(?:paste|write)(?![\w-])"
                    r"|['\"](?:paste)['\"]")
MESSAGE = (
    "Denied: autonomous agents never type into a zellij pane with write-chars/write/paste. "
    "Use /home/delorenj/.local/bin/zcrank-send --pane-id terminal_N --tab <tab> -- '<message>' "
    "(it tags the text [crank] so the pane agent can tell your words from the operator's, and "
    "checks it landed before pressing Enter). An operator draft already in the box goes through "
    "`zcrank-send --relay-draft`; /compact and /exit through `zcrank-send --control`. "
    "Plain send-keys (Esc, \"Ctrl c\") stays allowed."
)


def strings(value) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return strings(parsed) if isinstance(parsed, (dict, list)) else [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


def tool_text(payload: dict) -> str:
    for key in ("tool_input", "toolInput", "toolArgs", "args", "arguments"):
        if key in payload:
            return "\n".join(strings(payload[key]))
    return ""


def typing_into_pane(text: str) -> bool:
    return bool(ZELLIJ.search(text) and TYPING.search(text))


def result(status: str, reason: str, exit_code: int | None = None) -> str:
    meta = {"status": status, "reason": reason}
    if exit_code is not None:
        meta["exit_code"] = exit_code
    return json.dumps({"_hook_hub": meta, "stdout": ""})


def decide(payload: dict, cli: str) -> tuple[int, str, str]:
    if cli != "hermes":
        return 0, result("skipped", "not_hermes"), ""
    if not typing_into_pane(tool_text(payload)):
        return 0, result("skipped", "no_pane_typing"), ""
    return 2, result("succeeded", "raw_pane_typing_denied", 2), MESSAGE


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("payload")
        code, out, err = decide(payload, os.environ.get("BB_HOOK_CLI", ""))
    except Exception:
        code, out, err = 0, result("skipped", "guard_error"), ""
    sys.stdout.write(out)
    if err:
        sys.stderr.write(err + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
