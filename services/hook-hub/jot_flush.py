#!/usr/bin/env python3
"""Jot flush: turn a session's jots into question-framed Hindsight memories.

`jot` (~/.local/bin/jot) is the write-ahead half of the pattern: the strong
in-session model appends one line per insight to
`~/.agents/journal/jots/<sha1(cwd)[:12]>.md`, whose first line records where it
was written (`<!-- jots for <cwd> -->`). A line may route itself with a
`[bank:X]` prefix. This module is the cheap half:

  1. parse the jotfile, resolve the bank of every jot (per-line `[bank:X]`,
     else the header cwd's bank by the hub's rules, else the caller's cwd),
  2. ask a cheap model on OpenRouter to question-frame each jot (or say why it
     is a duplicate),
  3. retain one document per jot, `jot-<sha1(text)[:16]>`, so a re-flush of the
     same jot replaces its document instead of duplicating it,
  4. archive the jotfile only when every bank accepted its jots.

Until 2026-09-26 the normalizer called DeepSeek directly with a key that
answered "Insufficient Balance"; the script exited 0, the hub recorded
`skipped`, and 46 jots sat unflushed for seven weeks. Every failure is now a
non-zero exit, a `failed` hub receipt, a journal line and an ntfy alert (topic
infra, one per reason per hour).

    jot_flush.py flush [--jotfile PATH] [--bank NAME] [--already FILE] [--agent CLI] [--dry-run]
    jot_flush.py sweep [--idle SECONDS] [--all] [--dry-run]

The hub runs `dispatch()` at session end; `hindsight-capture-sweep.service`
runs `sweep` every 10 minutes for jotfiles no session end will ever reach (a
jot written from a subdirectory or worktree is keyed by THAT directory).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hindsight as hs  # noqa: E402
from concerns import result  # noqa: E402

# The per-consumer inference key minted for this job (OpenRouter key name
# "jot-flush", $1/day, AutomaticAI workspace). Referenced by 1Password item
# UUID: the vault has duplicate OpenRouter titles.
KEY_REF = "op://DeLoSecrets/qm3m5hvuqq4rykd2q2axhrlnpu/credential"
# Same family and legs as `hindsight-retain` in DeLoContainers
# stacks/ai/hindsight/litellm-config.yaml.
MODEL = "deepseek/deepseek-v4-flash-0731"
FALLBACK_MODELS = ("z-ai/glm-5.3-flash", "deepseek/deepseek-v4.1-flash")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CONTEXTS = ("deployment", "debugging", "architecture", "conventions", "preferences", "dependencies")

HEADER = re.compile(r"^<!--\s*jots for (.+?)\s*-->\s*$")
BANK_PREFIX = re.compile(r"^\[bank:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,99})\s*\]\s*", re.I)
MEM_LINE = re.compile(r"^\s*(?:[-*]\s*)?MEM\s*\|\s*(\d+)\s*\|\s*([A-Za-z-]+)\s*\|\s*(.+?)\s*$")
SKIP_LINE = re.compile(r"^\s*(?:[-*]\s*)?SKIP\s*\|\s*(\d+)\s*\|\s*(.*?)\s*$")

SYSTEM = """You are the memory normalizer for an engineering team's shared knowledge base.
The input is a numbered list of "jots": insights a strong coding agent flagged mid-session as
worth remembering. Each was already judged worth keeping, so keep every one unless it repeats an
earlier jot in this list or an ALREADY-CAPTURED item.

For each jot you keep, emit exactly ONE line:
MEM|<n>|<context>|<memory>
  <n>        the jot's number
  <context>  one of: deployment, debugging, architecture, conventions, preferences, dependencies
  <memory>   OPENS with the natural question a future engineer would ask, then answers it, so
             semantic recall matches how questions are actually asked. Keep every specific:
             exact names, paths, ports, flags, commands, versions, numbers and dates. Do not
             generalize, do not add facts that are not in the jot, one line, no markdown.
For each jot you drop, emit exactly ONE line:
SKIP|<n>|<which jot or ALREADY-CAPTURED item it repeats>

Output ONLY MEM| and SKIP| lines, one per jot, no preamble."""


def _now() -> float:
    return time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def journal_dir() -> Path:
    return Path(os.environ.get("HS_JOURNAL_DIR", Path.home() / ".agents/journal"))


def jot_dir() -> Path:
    return journal_dir() / "jots"


def state_dir() -> Path:
    return Path(os.environ.get("JOTFLUSH_STATE_DIR", Path.home() / ".local/state/33god/hook-hub/jot-flush"))


def jotfile_for(cwd: str) -> Path:
    """The jotfile `jot` writes from `cwd` (same keying as ~/.local/bin/jot)."""
    key = hashlib.sha1(cwd.encode()).hexdigest()[:12] if cwd else "nocwd"
    return jot_dir() / f"{key}.md"


def log(event: dict) -> None:
    """One line per flush outcome in <journal>/jot-flush.jsonl."""
    record = {"ts": _iso(_now()), **event}
    try:
        journal_dir().mkdir(parents=True, exist_ok=True)
        descriptor = os.open(journal_dir() / "jot-flush.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, (json.dumps(record, ensure_ascii=False) + "\n").encode())
        finally:
            os.close(descriptor)
    except OSError:
        pass


# ------------------------------------------------------------------ parsing

@dataclass
class Jot:
    n: int
    text: str
    raw: str
    bank: str = ""
    routed: bool = False  # True when the line's own [bank:X] chose the bank

    @property
    def doc_id(self) -> str:
        return "jot-" + hashlib.sha1(" ".join(self.text.split()).encode()).hexdigest()[:16]


@dataclass
class JotFile:
    path: Path
    origin: str
    jots: list[Jot] = field(default_factory=list)


def parse(path: Path) -> JotFile:
    """Header cwd plus one Jot per `- ` line (continuation lines join the jot above)."""
    origin = ""
    jots: list[Jot] = []
    for line in path.read_text(errors="replace").splitlines():
        header = HEADER.match(line)
        if header and not jots and not origin:
            origin = header.group(1).strip()
            continue
        if line.startswith("- "):
            jots.append(Jot(n=len(jots) + 1, text="", raw=line[2:]))
        elif jots and line.strip():
            jots[-1].raw += "\n" + line
    for jot in jots:
        raw = jot.raw.strip()
        routed = BANK_PREFIX.match(raw)
        if routed:
            jot.bank, jot.routed = routed.group(1), True
            raw = raw[routed.end():].strip()
        jot.text = raw
    return JotFile(path=path, origin=origin, jots=[jot for jot in jots if jot.text])


def origin_bank(origin: str, fallback_cwd: str | None = None) -> str:
    """The bank of the directory a jotfile was written in.

    A worktree that has since been removed resolves through its nearest
    surviving parent (`repo/.claude/worktrees/x` -> `repo`). No header at all
    means the caller's cwd.
    """
    where = origin or fallback_cwd or ""
    if where:
        path = Path(where)
        for _ in range(16):
            if path.is_dir() or path.parent == path:
                break
            path = path.parent
        if path.is_dir() and path != Path("/"):
            return hs.bank(str(path), "jot_flush")
    return hs.bank(None, "jot_flush")


# --------------------------------------------------------------- normalizer

class Budget:
    """One wall-clock budget shared by the key read, the normalizer and every retain."""

    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds

    def left(self, cap: float, floor: float = 5.0) -> float:
        remaining = min(cap, self.deadline - time.monotonic())
        if remaining < floor:
            raise FlushError("deadline_exceeded", f"{max(0.0, remaining):.1f}s left")
        return remaining


class FlushError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail


def api_key(budget: Budget) -> str:
    key = os.environ.get("JOTFLUSH_OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    op = shutil.which("op") or str(Path.home() / ".local/bin/op")
    ref = os.environ.get("JOTFLUSH_KEY_REF", KEY_REF)
    try:
        read = subprocess.run([op, "read", ref], capture_output=True, text=True, timeout=budget.left(20))
    except (OSError, subprocess.SubprocessError) as exc:
        raise FlushError("key_unavailable", type(exc).__name__) from exc
    if read.returncode or not read.stdout.strip():
        raise FlushError("key_unavailable", (read.stderr or "op read returned nothing").strip()[:200])
    return read.stdout.strip()


def normalizer_request(jots: list[Jot], already: str) -> dict:
    listing = "\n".join(f"{jot.n}. {jot.text}" for jot in jots)
    prompt = f"JOTS:\n{listing}\n"
    if already.strip():
        prompt += f"\nALREADY-CAPTURED (skip repeats of these):\n{already.strip()[:4000]}\n"
    return {
        "model": os.environ.get("JOTFLUSH_MODEL", MODEL),
        "models": [os.environ.get("JOTFLUSH_MODEL", MODEL), *FALLBACK_MODELS],
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        "max_tokens": min(8000, 400 + 350 * len(jots)),
        "temperature": 0.2,
        "reasoning": {"enabled": False},
        "provider": {"sort": "price", "allow_fallbacks": True, "data_collection": "deny",
                     "ignore": ["sail-research", "relace"]},
    }


def normalize(jots: list[Jot], already: str, key: str,
              budget: Budget) -> tuple[dict[int, tuple[str, str]], dict[int, str], dict]:
    """({n: (context, memory)}, {n: skip reason}, usage) from one OpenRouter call."""
    url = os.environ.get("JOTFLUSH_OPENROUTER_URL", OPENROUTER_URL)
    body = json.dumps(normalizer_request(jots, already), ensure_ascii=False).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "HTTP-Referer": "https://delo.sh/hook-hub/jot-flush", "X-Title": "jot-flush"})
    try:
        with urllib.request.urlopen(request, timeout=budget.left(float(os.environ.get("JOTFLUSH_LLM_TIMEOUT", "") or 120))) as response:
            data = json.loads(response.read(4 << 20))
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read(4000)).get("error", {}).get("message", "")
        except (OSError, ValueError, AttributeError):
            message = ""
        raise FlushError(f"normalizer_http_{exc.code}", str(message)[:300]) from exc
    except (OSError, ValueError) as exc:
        raise FlushError("normalizer_unreachable", type(exc).__name__) from exc
    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        raise FlushError("normalizer_error", str(error.get("message") if isinstance(error, dict) else error)[:300])
    try:
        content = str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise FlushError("normalizer_malformed", "no choices[0].message.content") from exc
    numbers = {jot.n for jot in jots}
    memories: dict[int, tuple[str, str]] = {}
    skipped: dict[int, str] = {}
    for line in content.splitlines():
        mem = MEM_LINE.match(line)
        if mem and int(mem.group(1)) in numbers and int(mem.group(1)) not in memories:
            context = mem.group(2).lower()
            memories[int(mem.group(1))] = (context if context in CONTEXTS else "conventions", mem.group(3).strip())
            continue
        skip = SKIP_LINE.match(line)
        if skip and int(skip.group(1)) in numbers:
            skipped.setdefault(int(skip.group(1)), skip.group(2).strip()[:200] or "duplicate")
    if not memories and not skipped:
        raise FlushError("normalizer_empty", content.strip()[:200] or "no MEM| or SKIP| lines")
    usage = data.get("usage") or {}
    return memories, {n: why for n, why in skipped.items() if n not in memories}, {
        "model": data.get("model", ""), "cost": usage.get("cost"),
        "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")}


# ------------------------------------------------------------------- retain

def retain_items(jf: JotFile, jots: list[Jot], memories: dict[int, tuple[str, str]], agent: str) -> list[dict]:
    try:
        stamp = _iso(jf.path.stat().st_mtime)
    except OSError:
        stamp = _iso(_now())
    host = hs.safe(socket.gethostname().split(".")[0])
    where = jf.origin or "an unrecorded directory"
    items = []
    for jot in jots:
        normalized = jot.n in memories
        context, content = memories[jot.n] if normalized else ("conventions", jot.text)
        items.append({
            "content": content,
            "context": (f"{context}: a durable engineering insight a coding agent recorded with `jot` "
                        f"while working in {where}"),
            "timestamp": stamp,
            "document_id": jot.doc_id,
            # One untagged consolidation scope per bank, as session write-back
            # does: provenance tags must not fork what a repo has learned.
            "observation_scopes": "shared",
            "tags": ["source:jot", f"host:{host}", *([f"agent:{hs.safe(agent)}"] if agent else [])],
            "metadata": {"source": "hook-hub/jot-flush", "jot": jot.text[:2000], "origin_cwd": jf.origin,
                         "jotfile": jf.path.name, "normalized": "true" if normalized else "false"},
        })
    return items


def retain(bank: str, items: list[dict], budget: Budget) -> dict:
    """Synchronous retain of one bank's items; raises FlushError unless every item landed.

    Synchronous on purpose: extraction failures (an exhausted LLM key) surface
    here as an HTTP error instead of an async operation nobody reads. A client
    timeout after the server accepted the work is harmless: document ids are
    stable, so the retry replaces rather than duplicates.
    """
    base, key = hs.api_endpoint()
    if not base:
        raise FlushError("hindsight_api_unconfigured")
    url = f"{base}/v1/default/banks/{urllib.parse.quote(bank, safe='')}/memories"
    body = json.dumps({"items": items, "async": False}, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               **({"Authorization": f"Bearer {key}"} if key else {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method="POST"),
                                    timeout=budget.left(float(os.environ.get("JOTFLUSH_RETAIN_TIMEOUT", "") or 150))) as response:
            data = json.loads(response.read(1 << 20) or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(2000).decode("utf-8", "replace")
        except OSError:
            detail = ""
        raise FlushError(f"retain_http_{exc.code}", f"{bank}: {detail[:250]}") from exc
    except (OSError, ValueError) as exc:
        raise FlushError("retain_unreachable", f"{bank}: {type(exc).__name__}") from exc
    if not isinstance(data, dict) or data.get("success") is False:
        raise FlushError("retain_rejected", f"{bank}: {str(data)[:250]}")
    return data


# -------------------------------------------------------------------- alert

ALERT_COOLDOWN_S = 3600


def alert(reason: str, message: str) -> None:
    """ntfy (topic infra) through ~/.local/bin/ntfy-alert, once per reason per hour."""
    if os.environ.get("JOTFLUSH_ALERT", "1") == "0":
        return
    path = state_dir() / "alerts.json"
    try:
        sent = json.loads(path.read_text())
    except (OSError, ValueError):
        sent = {}
    if not isinstance(sent, dict):
        sent = {}
    now = _now()
    if now - float(sent.get(reason, 0) or 0) < ALERT_COOLDOWN_S:
        return
    command = os.environ.get("JOTFLUSH_ALERT_COMMAND", str(Path.home() / ".local/bin/ntfy-alert"))
    try:
        subprocess.run([command, "--priority", "high", "--tags", "floppy_disk,warning",
                        "--title", f"jot-flush failed: {reason}", message],
                       capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return
    sent[reason] = now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sent))
    except OSError:
        pass


# -------------------------------------------------------------------- flush

@dataclass
class Outcome:
    status: str  # flushed | failed | empty | busy | dry_run
    reason: str
    detail: str = ""
    report: dict = field(default_factory=dict)


def _lock_path(jotfile: Path) -> Path:
    return state_dir() / "locks" / f"{jotfile.name}.lock"


def flush(jotfile: Path, *, bank: str = "", already: str = "", agent: str = "", dry_run: bool = False,
          fallback_cwd: str | None = None, blocking: bool = True, trigger: str = "cli",
          budget_s: float = 600.0) -> Outcome:
    """Flush one jotfile. Archives it only when every jot landed."""
    lock = _lock_path(jotfile)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            return Outcome("busy", "flush_in_progress")
        return _flush_locked(jotfile, bank=bank, already=already, agent=agent, dry_run=dry_run,
                             fallback_cwd=fallback_cwd, trigger=trigger, budget=Budget(budget_s))


def _flush_locked(jotfile: Path, *, bank: str, already: str, agent: str, dry_run: bool,
                  fallback_cwd: str | None, trigger: str, budget: Budget) -> Outcome:
    if not jotfile.is_file() or not jotfile.stat().st_size:
        return Outcome("empty", "no_pending_jots")
    jf = parse(jotfile)
    if not jf.jots:
        return Outcome("empty", "jotfile_has_no_jots")
    home = bank or origin_bank(jf.origin, fallback_cwd)
    for jot in jf.jots:
        if not jot.routed:
            jot.bank = home
    report: dict[str, Any] = {"jotfile": jotfile.name, "origin": jf.origin, "trigger": trigger,
                              "jots": len(jf.jots), "banks": {}}
    try:
        memories, skipped, usage = normalize(jf.jots, already, api_key(budget), budget)
        report["llm"] = usage
        by_bank: dict[str, list[Jot]] = {}
        for jot in jf.jots:
            if jot.n not in skipped:
                by_bank.setdefault(jot.bank, []).append(jot)
        report["skipped"] = [{"n": n, "why": why} for n, why in sorted(skipped.items())]
        report["raw_fallback"] = [jot.n for jot in jf.jots if jot.n not in memories and jot.n not in skipped]
        for target, jots in sorted(by_bank.items()):
            items = retain_items(jf, jots, memories, agent)
            report["banks"][target] = [item["document_id"] for item in items]
            if dry_run:
                for jot, item in zip(jots, items):
                    print(f"  [dry-run] {target} {item['document_id']} ({item['context'].split(':')[0]}) "
                          f"{item['content']}")
                continue
            retain(target, items, budget)
    except FlushError as exc:
        report.update(status="failed", reason=exc.reason, detail=exc.detail)
        log(report)
        return Outcome("failed", exc.reason, exc.detail, report)
    if dry_run:
        report.update(status="dry_run")
        return Outcome("dry_run", "dry_run", report=report)
    archive = jot_dir() / "flushed" / f"{jotfile.name}.{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    archive.parent.mkdir(parents=True, exist_ok=True)
    os.replace(jotfile, archive)
    report.update(status="flushed", archive=archive.name)
    log(report)
    return Outcome("flushed", "jots_flushed", report=report)


def fail_loudly(outcome: Outcome, jotfile: Path) -> None:
    alert(outcome.reason, f"{jotfile.name} ({outcome.report.get('origin') or 'no origin'}) kept for retry. "
                          f"{outcome.detail or ''} Journal: ~/.agents/journal/jot-flush.jsonl".strip())


# -------------------------------------------------------------------- sweep

BACKOFF_S = (600, 1800, 3600, 3 * 3600, 6 * 3600)


def _backoff() -> dict:
    try:
        data = json.loads((state_dir() / "backoff.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_backoff(data: dict) -> None:
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        (state_dir() / "backoff.json").write_text(json.dumps(data))
    except OSError:
        pass


def sweep(idle_s: float = 7200, everything: bool = False, dry_run: bool = False) -> dict:
    """Flush every jotfile idle for `idle_s` (all of them with `everything`)."""
    report: dict[str, Any] = {"examined": 0, "flushed": 0, "failed": 0, "waiting": 0, "results": []}
    folder = jot_dir()
    if not folder.is_dir():
        return report
    backoff = _backoff()
    now = _now()
    for path in sorted(folder.glob("*.md")):
        report["examined"] += 1
        try:
            idle = now - path.stat().st_mtime
        except OSError:
            continue
        state = backoff.get(path.name) or {}
        if not everything and (idle < idle_s or now < float(state.get("next_at", 0) or 0)):
            report["waiting"] += 1
            continue
        outcome = flush(path, dry_run=dry_run, blocking=False, trigger="sweep", budget_s=300)
        report["results"].append({"jotfile": path.name, "status": outcome.status, "reason": outcome.reason,
                                  "banks": {bank: len(ids) for bank, ids in outcome.report.get("banks", {}).items()}})
        if outcome.status == "failed":
            report["failed"] += 1
            attempts = int(state.get("attempts", 0) or 0) + 1
            backoff[path.name] = {"attempts": attempts, "reason": outcome.reason,
                                  "next_at": now + BACKOFF_S[min(attempts, len(BACKOFF_S)) - 1]}
            fail_loudly(outcome, path)
        elif outcome.status == "flushed":
            report["flushed"] += 1
            backoff.pop(path.name, None)
    if not dry_run:
        _save_backoff({name: row for name, row in backoff.items() if (folder / name).exists()})
    return report


# ------------------------------------------------------------------ hub path

def dispatch(payload: dict, cli: str) -> dict:
    """Session end: flush the jotfile of the session's cwd."""
    cwd = str(payload.get("cwd") or os.getcwd())
    jotfile = jotfile_for(cwd)
    if not jotfile.is_file() or not jotfile.stat().st_size:
        return result("skipped", "no_pending_jots")
    # The registry kills this handler at 185s; stay inside it so a slow leg
    # fails here, loudly, instead of as an anonymous hub timeout.
    outcome = flush(jotfile, agent=cli, fallback_cwd=cwd, trigger="session_end",
                    budget_s=float(os.environ.get("JOTFLUSH_HUB_BUDGET_S", "") or 170))
    if outcome.status == "flushed":
        return result("succeeded", "jots_flushed")
    if outcome.status == "busy":
        return result("skipped", "flush_in_progress")
    if outcome.status == "empty":
        return result("skipped", outcome.reason)
    fail_loudly(outcome, jotfile)
    return result("failed", f"jot_{outcome.reason}"[:96], exit_code=1)


# ----------------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jot_flush.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command")
    one = sub.add_parser("flush", help="flush one jotfile (default: the one for $PWD)")
    one.add_argument("--jotfile")
    one.add_argument("--bank", default="")
    one.add_argument("--already", default="")
    one.add_argument("--agent", default=os.environ.get("HINDSIGHT_AGENT", ""))
    one.add_argument("--dry-run", action="store_true")
    many = sub.add_parser("sweep", help="flush every idle jotfile")
    many.add_argument("--idle", type=float, default=float(os.environ.get("JOTFLUSH_IDLE_S", "") or 7200))
    many.add_argument("--all", action="store_true", help="ignore idle time and backoff")
    many.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv[1:] if len(argv) > 1 and argv[1] in {"flush", "sweep"} else ["flush", *argv[1:]])

    if args.command == "sweep":
        report = sweep(args.idle, everything=args.all, dry_run=args.dry_run)
        print(json.dumps(report, ensure_ascii=False))
        return 1 if report["failed"] else 0

    jotfile = Path(args.jotfile) if args.jotfile else jotfile_for(os.environ.get("PWD") or os.getcwd())
    already = ""
    if args.already:
        try:
            already = Path(args.already).read_text(errors="replace")[:4000]
        except OSError:
            already = ""
    outcome = flush(jotfile, bank=args.bank, already=already, agent=args.agent, dry_run=args.dry_run)
    if outcome.status == "empty":
        print(f"jot-flush: {outcome.reason} ({jotfile})")
        return 0
    if outcome.status == "busy":
        print(f"jot-flush: another flush holds {jotfile.name}", file=sys.stderr)
        return 75
    summary = {bank: len(ids) for bank, ids in outcome.report.get("banks", {}).items()}
    if outcome.status == "failed":
        print(f"jot-flush: FAILED {outcome.reason}: {outcome.detail} -- kept {jotfile.name}", file=sys.stderr)
        fail_loudly(outcome, jotfile)
        return 1
    print(f"jot-flush: {outcome.status} {jotfile.name} -> {summary}"
          f" (skipped {len(outcome.report.get('skipped', []))}, raw {len(outcome.report.get('raw_fallback', []))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
