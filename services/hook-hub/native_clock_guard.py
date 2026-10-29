#!/usr/bin/env python3
"""Native-clock guard: Candybar is the single clock on this host.

A standalone hook-hub handler (sync, pre_tool). It denies agent tool calls that
would create a recurring clock outside Candybar: crontab writes, new *.timer
units, systemd-run --on-*, at/batch, Hermes internal cron jobs (CLI and the
cronjob_manage tool), launchd interval plists, and creation of cron/timer files
by shell redirection or the Write/Edit/apply_patch tools.

It is deliberately NOT a concerns.py handler: concerns' main() always exits 0,
so stderr would never reach the CLI. A deny here really exits 2 and writes the
reason to stderr (Claude, Codex, Kimi, Copilot, Hermes). Gemini runs in trailer
mode, which drops exit codes, and OpenCode's plugin reads stdout, so those two
get the decision as stdout JSON.

Fail-open on every error of its own: a guard bug must never wedge an agent. The
backstops are the global git pre-commit rule and `candybar doctor --live`.

Exemptions are read (never written) from candybar/deploy/scheduling-policy.json;
if that file is unreadable the AD-22 candybar-watchdog.timer is still exempt.
Manual documented rollbacks: prefix the command with
`CANDYBAR_NATIVE_CLOCK=rollback:<ledger-id> ` (id must exist in
candybar/deploy/migration/ledger.yaml). Stdlib only; run with /usr/bin/python3.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
BUILTIN_EXEMPT = {
    "systemd:user:candybar-watchdog.timer",
    "repo:deploy/watchdog/candybar-watchdog.timer",
}

SHELL_TOOLS = {"bash", "shell", "shell_command", "local_shell", "exec_command",
               "run_shell_command", "terminal"}
WRITE_TOOLS = {"write", "write_file", "create", "edit", "multiedit", "replace",
               "patch", "apply_patch", "str_replace_editor"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}

TIMER_VERBS = {"enable", "reenable", "start", "restart", "try-restart", "reload-or-restart",
               "try-reload-or-restart", "link", "unmask", "preset", "add-wants",
               "add-requires", "edit"}
SYSTEMCTL_VALUED = {"-H", "-M", "-t", "-p", "-n", "-o", "-s", "-T", "--host", "--machine",
                    "--type", "--property", "--lines", "--output", "--signal", "--root",
                    "--kill-whom", "--kill-value", "--state", "--job-mode", "--what",
                    "--check-inhibitors", "--timestamp"}
SYSTEMD_RUN_TIMER = ("--on-calendar", "--on-active", "--on-boot", "--on-startup",
                     "--on-unit-active", "--on-unit-inactive", "--on-clock-change",
                     "--on-timezone-change", "--timer-property")

CLOCK_PATHS = [
    re.compile(r"\.timer$"),
    re.compile(r"/timers\.target\.wants/[^/]+$"),
    re.compile(r"^/etc/cron\.(d|daily|hourly|weekly|monthly)/[^/]+$"),
    re.compile(r"^/var/spool/cron(/crontabs)?/[^/]+$"),
    re.compile(r"\.crontab$"),
]
PLIST_PATH = re.compile(r"/Library/Launch(Agents|Daemons)/[^/]+\.plist$")
INTERVAL_KEYS = re.compile(r"StartCalendarInterval|StartInterval")
HATCH = re.compile(r"^\s*CANDYBAR_NATIVE_CLOCK=rollback:([A-Za-z0-9_.:-]+)(?:\s|$)")
ADD_FILE = re.compile(r"^\*\*\* (?:Add File|Move to):\s*(.+?)\s*$", re.M)
ONE_SHOT = re.compile(
    r"^(in\s+\S.*|\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?)$",
    re.I)

MESSAGE = """BLOCKED by native-clock-guard: `{frag}` would create a recurring clock outside Candybar.
Candybar (~/code/33GOD/candybar) is the only scheduler on this host: no crontab, no *.timer units, no systemd-run --on-*, no at/batch, no launchd intervals, no Hermes internal cron jobs.
Do this instead (the `candybar` skill has the full recipe):
  - New recurring job: write a candybar.toml (runner kind exec|systemd|hermes|n8n), then from ~/code/33GOD/candybar run `uv run candybar register <dir>`.
  - Hermes job: `hermes cron create ... --scheduler external`, and register a candybar to fire it.
  - Move or restore an existing clock: `uv run candybar migrate plan|cutover|rollback <ledger-id>`.
  - Audit: `uv run candybar doctor --repo . --live --json`.
Exempt: only exact entries in candybar/deploy/scheduling-policy.json (AD-22: candybar-watchdog.timer).
Manual documented ledger rollback: prefix the command with CANDYBAR_NATIVE_CLOCK=rollback:<ledger-id>."""


# ---------------------------------------------------------------- policy


def candybar_root() -> Path:
    return Path(os.environ.get("CANDYBAR_REPO") or HOME / "code/33GOD/candybar")


def load_exemptions() -> set[str]:
    try:
        data = json.loads((candybar_root() / "deploy/scheduling-policy.json").read_text())
        ids = {e["id"] for e in data["exceptions"] if isinstance(e, dict) and isinstance(e.get("id"), str)}
        return ids | BUILTIN_EXEMPT
    except Exception:
        return set(BUILTIN_EXEMPT)


def ledger_has(ledger_id: str) -> bool:
    try:
        text = (candybar_root() / "deploy/migration/ledger.yaml").read_text()
    except OSError:
        return False
    return re.search(r"^\s*-\s*id:\s*['\"]?" + re.escape(ledger_id) + r"['\"]?\s*$", text, re.M) is not None


def exempt_unit(unit: str, user: bool, ex: set[str]) -> bool:
    return f"systemd:{'user' if user else 'system'}:{os.path.basename(unit)}" in ex


# ---------------------------------------------------------------- shell lexer


class Cmd:
    __slots__ = ("argv", "redirs")

    def __init__(self, argv, redirs):
        self.argv, self.redirs = argv, redirs


def _match_paren(s: str, i: int) -> int:
    """Index of the ')' closing the '(' just before i (i is first inner char)."""
    depth, n = 1, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i += 1
            while i < n and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def parse_shell(s: str, depth: int = 0) -> tuple[list[Cmd], list[str]]:
    """Segment into simple commands. Returns (commands, nested shell strings)."""
    cmds: list[Cmd] = []
    subs: list[str] = []
    if depth > 6:
        return cmds, subs
    argv: list[str] = []
    redirs: list[tuple[str, str]] = []
    buf: list[str] = []
    state = {"inword": False, "pending": None, "last": None}
    heredocs: list[tuple[str, bool]] = []
    i, n = 0, len(s)

    def endword():
        if not state["inword"]:
            return
        w = "".join(buf)
        buf.clear()
        state["inword"] = False
        p = state["pending"]
        if p is None:
            argv.append(w)
        else:
            state["pending"] = None
            if p.startswith("<<") and p != "<<<":
                heredocs.append((w, p == "<<-"))
            elif p[0] in ">&":
                redirs.append((p, w))

    def endcmd():
        endword()
        state["pending"] = None
        if argv or redirs:
            cmd = Cmd(list(argv), list(redirs))
            cmds.append(cmd)
            state["last"] = cmd
        argv.clear()
        redirs.clear()

    while i < n:
        c = s[i]
        if c in " \t":
            endword()
            i += 1
        elif c == "\n":
            endcmd()
            i += 1
            for delim, strip in heredocs:
                while i < n:
                    j = s.find("\n", i)
                    line = s[i:n if j < 0 else j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
            heredocs.clear()
        elif c == "\\":
            if i + 1 < n and s[i + 1] == "\n":
                i += 2
                continue
            buf.append(s[i + 1] if i + 1 < n else "")
            state["inword"] = True
            i += 2
        elif c == "'":
            j = s.find("'", i + 1)
            j = n if j < 0 else j
            buf.append(s[i + 1:j])
            state["inword"] = True
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and s[i] != '"':
                if s[i] == "\\" and i + 1 < n:
                    buf.append(s[i + 1])
                    i += 2
                elif s.startswith("$(", i):
                    j = _match_paren(s, i + 2)
                    subs.append(s[i + 2:j])
                    buf.append("$(...)")
                    i = j + 1
                elif s[i] == "`":
                    j = s.find("`", i + 1)
                    j = n if j < 0 else j
                    subs.append(s[i + 1:j])
                    buf.append("`...`")
                    i = j + 1
                else:
                    buf.append(s[i])
                    i += 1
            state["inword"] = True
            i += 1
        elif c == "`":
            j = s.find("`", i + 1)
            j = n if j < 0 else j
            subs.append(s[i + 1:j])
            buf.append("`...`")
            state["inword"] = True
            i = j + 1
        elif c == "$" and s.startswith("$(", i):
            j = _match_paren(s, i + 2)
            subs.append(s[i + 2:j])
            buf.append("$(...)")
            state["inword"] = True
            i = j + 1
        elif c in "<>" and s.startswith("(", i + 1):
            j = _match_paren(s, i + 2)
            subs.append(s[i + 2:j])
            buf.append("<(...)")
            state["inword"] = True
            i = j + 1
        elif c in "<>&" and not (c == "&" and not s.startswith("&>", i)):
            # redirection (a bare & is handled below as a separator)
            if state["inword"] and "".join(buf).isdigit():
                buf.clear()
                state["inword"] = False
            else:
                endword()
            j = i + 1
            while j < n and s[j] in "<>|&-" and j - i < 3 and not (s[j] == "-" and s[i:j] != "<<"):
                j += 1
            state["pending"] = s[i:j]
            i = j
        elif c in ";|&()":
            endcmd()
            i += 1
        elif c == "#" and not state["inword"]:
            j = s.find("\n", i)
            i = n if j < 0 else j
        else:
            buf.append(c)
            state["inword"] = True
            i += 1
    endcmd()
    return cmds, subs


# ---------------------------------------------------------------- analysis


class Ctx:
    def __init__(self, cwd: str, text: str):
        self.cwd = cwd
        self.text = text
        self.ex = load_exemptions()
        self.hits: list[str] = []

    def hit(self, frag: str) -> None:
        self.hits.append(frag.strip()[:140])

    def resolve(self, p: str) -> str:
        p = os.path.expandvars(os.path.expanduser(p))
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(self.cwd, p))

    def new_clock_file(self, raw: str, content: str = "") -> str | None:
        """Return a reason if creating raw (that does not exist yet) is a native clock."""
        if not raw or raw.startswith("&") or raw in ("/dev/null", "-"):
            return None
        path = self.resolve(raw)
        if os.path.lexists(path):
            return None
        clock = any(rx.search(path) for rx in CLOCK_PATHS)
        if not clock and PLIST_PATH.search(path):
            clock = INTERVAL_KEYS.search(content or self.text) is not None
        if not clock:
            return None
        if path.endswith(".timer") and any(
                e.startswith("systemd:") and e.rsplit(":", 1)[-1] == os.path.basename(path)
                for e in self.ex):
            return None
        try:
            rel = os.path.relpath(path, candybar_root())
            if not rel.startswith("..") and f"repo:{rel}" in self.ex:
                return None
        except ValueError:
            pass
        return path


def strip_prefix(argv: list[str]) -> list[str]:
    a = list(argv)
    while a:
        w = a[0]
        name = os.path.basename(w)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w) or name in {
                "{", "!", "then", "do", "else", "elif", "if", "while", "until", "time",
                "command", "exec", "nohup", "setsid", "builtin", "stdbuf"}:
            a.pop(0)
        elif name in {"sudo", "doas"}:
            a.pop(0)
            while a and a[0].startswith("-"):
                o = a.pop(0)
                if o == "--":
                    break
                if o in {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"} and a:
                    a.pop(0)
        elif name == "env":
            a.pop(0)
            while a and (a[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", a[0])):
                o = a.pop(0)
                if o in {"-u", "-C", "-S"} and a:
                    a.pop(0)
        elif name in {"nice", "ionice"}:
            a.pop(0)
            while a and a[0].startswith("-"):
                o = a.pop(0)
                if o in {"-n", "-c", "-p"} and a:
                    a.pop(0)
        elif name == "timeout":
            a.pop(0)
            while a and a[0].startswith("-"):
                o = a.pop(0)
                if o in {"-s", "-k"} and a:
                    a.pop(0)
            if a:
                a.pop(0)
        else:
            break
    return a


def short_flags(args: list[str]) -> set[str]:
    out: set[str] = set()
    for w in args:
        if w.startswith("-") and not w.startswith("--"):
            out.update(w[1:])
    return out


def positional(args: list[str], valued: set[str]) -> tuple[list[str], str | None]:
    """Non-option args, plus a -t/--target-directory value if given."""
    pos: list[str] = []
    tdir = None
    i = 0
    while i < len(args):
        w = args[i]
        if w == "--":
            pos.extend(args[i + 1:])
            break
        if w in ("-t", "--target-directory") and i + 1 < len(args):
            tdir = args[i + 1]
            i += 2
        elif w.startswith("--target-directory="):
            tdir = w.split("=", 1)[1]
            i += 1
        elif w in valued and i + 1 < len(args):
            i += 2
        elif w.startswith("-") and w != "-":
            i += 1
        else:
            pos.append(w)
            i += 1
    return pos, tdir


def copy_targets(ctx: Ctx, name: str, args: list[str]) -> list[tuple[str, str]]:
    valued = {"cp": {"-S"}, "mv": {"-S"}, "ln": {"-S"},
              "install": {"-m", "-o", "-g", "-S", "--mode", "--owner", "--group"}}[name]
    if name == "install" and ("-d" in args or "--directory" in args):
        return []
    pos, tdir = positional(args, valued)
    if tdir is not None:
        srcs, dests = pos, [os.path.join(tdir, os.path.basename(p)) for p in pos]
    elif len(pos) >= 2:
        last = pos[-1]
        if os.path.isdir(ctx.resolve(last)):
            srcs, dests = pos[:-1], [os.path.join(last, os.path.basename(p)) for p in pos[:-1]]
        else:
            srcs, dests = pos[:-1][:1], [last]
    else:
        return []
    return list(zip(dests, srcs))


def read_text(ctx: Ctx, raw: str) -> str:
    try:
        return Path(ctx.resolve(raw)).read_text(errors="replace")[:65536]
    except OSError:
        return ""


def analyze_command(ctx: Ctx, cmd: Cmd, depth: int) -> None:
    argv = strip_prefix(cmd.argv)
    for op, target in cmd.redirs:
        if op.startswith("<") or target.isdigit() or target == "-":
            continue
        if ctx.new_clock_file(target):
            ctx.hit(f"{op} {target}")
    if not argv:
        return
    name = os.path.basename(argv[0])
    args = argv[1:]
    frag = " ".join(argv)

    if name in SHELLS or name == "su":
        for k, w in enumerate(args):
            if re.match(r"^-[A-Za-z]*c[A-Za-z]*$", w) and k + 1 < len(args):
                analyze_string(ctx, args[k + 1], depth + 1)
                break
        return
    if name == "eval":
        analyze_string(ctx, " ".join(args), depth + 1)
        return

    if name == "crontab":
        flags: set[str] = set()
        i = 0
        while i < len(args):
            if args[i] == "-u":
                i += 2
                continue
            if args[i].startswith("-") and not args[i].startswith("--"):
                flags.update(args[i][1:])
            i += 1
        if not flags & {"l", "r", "V"}:
            ctx.hit(frag)
    elif name == "systemctl":
        user = "--user" in args
        pos: list[str] = []
        i = 0
        while i < len(args):
            w = args[i]
            if w in SYSTEMCTL_VALUED and i + 1 < len(args):
                i += 2
            elif w.startswith("-"):
                i += 1
            else:
                pos.append(w)
                i += 1
        if pos and pos[0] in TIMER_VERBS:
            for unit in pos[1:]:
                if unit.endswith(".timer") and not exempt_unit(unit, user, ctx.ex):
                    ctx.hit(frag)
                    break
    elif name == "systemd-run":
        if any(w.split("=", 1)[0] in SYSTEMD_RUN_TIMER for w in args):
            ctx.hit(frag)
    elif name in {"at", "batch"}:
        if not short_flags(args) & {"l", "c", "d", "r", "V"}:
            ctx.hit(frag)
    elif name == "hermes":
        if "cron" in args:
            rest = args[args.index("cron") + 1:]
            sub = rest[0] if rest else ""
            sched = None
            for k, w in enumerate(rest):
                if w == "--scheduler" and k + 1 < len(rest):
                    sched = rest[k + 1]
                elif w.startswith("--scheduler="):
                    sched = w.split("=", 1)[1]
            if sub in {"create", "add"} and sched != "external":
                ctx.hit(frag)
            elif sub == "edit" and sched == "internal":
                ctx.hit(frag)
    elif name == "launchctl":
        sub = args[0] if args else ""
        if sub == "submit":
            ctx.hit(frag)
        elif sub in {"load", "bootstrap"}:
            for w in args[1:]:
                if w.endswith(".plist") and INTERVAL_KEYS.search(read_text(ctx, w)):
                    ctx.hit(frag)
                    break
    elif name == "tee":
        pos, _ = positional(args, set())
        for p in pos:
            if ctx.new_clock_file(p):
                ctx.hit(f"tee {p}")
    elif name in {"cp", "mv", "install", "ln"}:
        for dest, src in copy_targets(ctx, name, args):
            if ctx.new_clock_file(dest, read_text(ctx, src)):
                ctx.hit(f"{name} {src} {dest}")


def analyze_string(ctx: Ctx, s: str, depth: int = 0) -> None:
    cmds, subs = parse_shell(s, depth)
    for cmd in cmds:
        analyze_command(ctx, cmd, depth)
    for sub in subs:
        if depth < 6:
            analyze_string(ctx, sub, depth + 1)


# ---------------------------------------------------------------- tool shapes


def tool_input(payload: dict) -> dict:
    for key in ("tool_input", "toolInput", "toolArgs", "args"):
        v = payload.get(key)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                v = {"command": v} if "*** Begin Patch" not in v else {"patch": v}
        if isinstance(v, dict):
            return v
    return {}


def shell_command(inp: dict) -> str:
    for key in ("command", "cmd", "script"):
        v = inp.get(key)
        if isinstance(v, list) and v:
            strs = [str(x) for x in v]
            if len(strs) >= 3 and os.path.basename(strs[0]) in SHELLS and "c" in strs[1].lstrip("-"):
                return strs[2]
            return shlex.join(strs)
        if isinstance(v, str) and v:
            return v
    return ""


def check_write(ctx: Ctx, inp: dict) -> None:
    content = "\n".join(str(inp[k]) for k in ("content", "file_text", "new_string", "text", "new_str")
                        if isinstance(inp.get(k), str))
    for key in ("file_path", "path", "filepath", "file", "filename", "target_file", "TargetFile"):
        v = inp.get(key)
        if isinstance(v, str) and v:
            if ctx.new_clock_file(v, content):
                ctx.hit(f"write {v}")
            break
    for v in inp.values():
        if isinstance(v, str) and "*** Begin Patch" in v:
            for m in ADD_FILE.finditer(v):
                if ctx.new_clock_file(m.group(1), v):
                    ctx.hit(f"apply_patch {m.group(1)}")


def check_cronjob(ctx: Ctx, inp: dict) -> None:
    action = str(inp.get("action", "")).lower()
    schedule = inp.get("schedule")
    if action not in {"create", "update"}:
        return
    if action == "update" and not schedule:
        return
    if isinstance(schedule, str) and ONE_SHOT.match(schedule.strip()):
        return
    ctx.hit(f"cronjob_manage action={action} schedule={schedule!r}")


def evaluate(payload: dict, cwd: str) -> tuple[Ctx, str] | None:
    tool = str(payload.get("tool_name") or payload.get("toolName") or "").lower()
    inp = tool_input(payload)
    if tool in SHELL_TOOLS:
        text = shell_command(inp)
        if not text:
            return None
        ctx = Ctx(cwd, text)
        analyze_string(ctx, text)
        return ctx, text
    ctx = Ctx(cwd, "")
    if tool == "cronjob_manage":
        check_cronjob(ctx, inp)
    elif tool in WRITE_TOOLS:
        check_write(ctx, inp)
    else:
        return None
    return ctx, ""


# ---------------------------------------------------------------- output


def result(status: str, reason: str, stdout: str = "", exit_code: int | None = None) -> str:
    meta = {"status": status, "reason": reason}
    if exit_code is not None:
        meta["exit_code"] = exit_code
    return json.dumps({"_hook_hub": meta, "stdout": stdout})


def deny_stdout(cli: str, message: str) -> str:
    if cli == "gemini":
        return json.dumps({"decision": "deny", "reason": message})
    if cli == "opencode":
        return json.dumps({"permissionDecision": "deny", "permissionDecisionReason": message})
    return ""


def decide(payload: dict, cwd: str, cli: str) -> tuple[int, str, str]:
    """(exit_code, stdout, stderr) for one pre_tool payload."""
    verdict = evaluate(payload, cwd)
    if verdict is None or not verdict[0].hits:
        return 0, result("skipped", "no_native_clock"), ""
    ctx, text = verdict
    note = ""
    hatch = HATCH.match(text) if text else None
    if hatch:
        if ledger_has(hatch.group(1)):
            return 0, result("succeeded", "native_clock_escape_hatch"), ""
        note = (f"\nEscape hatch rejected: ledger id `{hatch.group(1)}` is not in "
                "candybar/deploy/migration/ledger.yaml.")
    message = MESSAGE.format(frag=ctx.hits[0]) + note
    return 2, result("succeeded", "native_clock_denied", deny_stdout(cli, message), 2), message


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("payload")
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else os.getcwd()
        code, out, err = decide(payload, cwd, os.environ.get("BB_HOOK_CLI", ""))
    except Exception:
        code, out, err = 0, result("skipped", "guard_error"), ""
    sys.stdout.write(out)
    if err:
        sys.stderr.write(err + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
