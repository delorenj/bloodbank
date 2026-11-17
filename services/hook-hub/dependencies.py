"""Side-effect-free dependency checks for hook-hub handler startup and status."""
from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

# These are external commands used by concerns.py after its own interpreter starts.
# Conditional concerns can legitimately skip at runtime (for example unindexed repos).
WRAPPED = {
    "skill-reminder": ("~/.agents/hooks/reminder-for-skill-check.sh",),
    "skill-lint": ("~/.agents/hooks/lint-skills.py",),
    "candystore-context": ("~/.local/bin/candystore",),
    "codegraph-prompt": ("codegraph",),
    "code-review-graph-status": ("code-review-graph",),
    "code-review-graph-update": ("code-review-graph",),
    "code-review-graph-before-commit": ("code-review-graph",),
    "claude-notify": ("claude-notify",),
    "hindsight-recall": ("hindsight",),
    "hindsight-retain": ("hindsight",),
    "hindsight-session-end": ("hindsight",),
    "hindsight-journal": ("hindsight",),
    "hindsight-briefing": ("hindsight",),
    "hindsight-jot-flush": ("op",),
    "nanoleaf": ("~/.local/bin/nlp",),
    "zellij-notify": ("~/.config/zellij/scripts/zellij-notify",),
    "project-notebook-start": ("~/.agents/skills/project-notebook/hooks/session-start.sh",),
    "project-notebook-end": ("~/.agents/skills/project-notebook/hooks/session-end.sh",),
}


def git_checkpoint_command(path: str | None = None) -> str:
    """Select the same GitMark executable for preflight and dispatch.

    Keep the last candidate even when absent: invoke() then reports the
    existing handler_binary_missing result, while preflight can name the
    actual attempted path.
    """
    candidates = ("git-checkpoint", "~/.local/bin/git-checkpoint",
                  "~/GitMark/bin/git-checkpoint", "~/code/GitMark/bin/git-checkpoint")
    for name in candidates[:-1]:
        target = Path.home() / name[2:] if name.startswith("~/") else Path(name)
        if "/" not in name:
            found = shutil.which(name, path=path)
            if found:
                return found
        elif target.is_file() and os.access(target, os.X_OK):
            return str(target)
    return str(Path.home() / candidates[-1][2:])



def _missing(command: str, path: str, *, script: bool = False, depth: int = 0) -> bool:
    """Resolve a command and its shebang without executing it."""
    if depth > 3:
        return True
    candidate = Path(command).expanduser()
    if "/" not in command:
        found = shutil.which(command, path=path)
        if not found:
            return True
        candidate = Path(found)
    if not candidate.is_file() or (not script and not os.access(candidate, os.X_OK)):
        return True
    try:
        with candidate.open("rb") as stream:
            line = stream.readline(256)
    except OSError:
        return True
    if not line.startswith(b"#!"):
        return False
    try:
        tokens = shlex.split(line[2:].decode("utf-8", "replace").strip())
    except ValueError:
        return True
    if not tokens:
        return True
    interpreter = tokens[0]
    if Path(interpreter).name == "env":
        # /usr/bin/env -S supports splitting the remainder; env options precede
        # the actual executable. The hook registry uses only simple shebangs.
        arguments = [token for token in tokens[1:] if token not in {"-S"}]
        interpreter = next((token for token in arguments if not token.startswith("-")), "")
    return not interpreter or _missing(interpreter, path, depth=depth + 1)


def missing_dependencies(handler, path: str) -> list[str]:
    """Return command identifiers only; never execute or expose hook payloads."""
    if not handler.enabled:
        return []
    commands = [handler.command[0]]
    if len(handler.command) > 1 and handler.command[1].endswith(".py"):
        commands.append(handler.command[1])
    if len(handler.command) > 2 and Path(handler.command[1]).name == "concerns.py":
        concern = handler.command[2]
        if concern == "git-checkpoint":
            commands.append(git_checkpoint_command(path))
        else:
            commands.extend(WRAPPED.get(concern, ()))
    return [command for command in dict.fromkeys(commands)
            if _missing(command, path, script=command.endswith(".py"))]
