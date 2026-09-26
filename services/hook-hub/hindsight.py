"""Supervised Hindsight concerns: recall, briefing, edit candidates, retention receipts.

Candidate writes are deliberately distinct from a successful memory retain.
Session close uses a deterministic document id, so a retry replaces the same
document if the process dies after the API accepted it but before our receipt.
"""
from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from concerns import context_output, file_edits, invoke, repository, result

# Deadlines are measured from here: the handler is a fresh process per hook, so
# import time is within milliseconds of process start. See `anchor()`.
STARTED = time.monotonic()


def anchor() -> float:
    """When this hook's budget started: process start, or now in a long-lived importer."""
    now = time.monotonic()
    return STARTED if now - STARTED < 2.0 else now


def safe(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return cleaned[:100] or hashlib.sha256(value.encode()).hexdigest()[:20]


def session_key(payload: dict, cli: str) -> str:
    # CLI and native session identity keep simultaneous clients apart. Without
    # a session id we cannot truthfully join prompt/tool/end work.
    session = str(payload.get("session_id", ""))
    return f"{safe(cli)}-{safe(session)}" if session else ""


def journal_path(payload: dict, cli: str) -> Path:
    root = Path(os.environ.get("HS_JOURNAL_DIR", Path.home() / ".agents/journal"))
    return root / "sessions" / f"{session_key(payload, cli)}.jsonl"


def journal(payload: dict, cli: str, event: dict) -> None:
    path = journal_path(payload, cli)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now(timezone.utc).isoformat(), "cli": cli,
              "session_id": payload.get("session_id"),
              "invocation_id": os.environ.get("BB_HOOK_INVOCATION_ID", ""), **event}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(record, ensure_ascii=False) + "\n").encode())
    finally:
        os.close(descriptor)


def records(payload: dict, cli: str) -> list[dict]:
    paths = [journal_path(payload, cli)]
    # Preserve candidates produced before this session's native cutover.
    old = paths[0].parent / f"{safe(str(payload.get('session_id', '')))}.jsonl"
    if old != paths[0]:
        paths.append(old)
    found: list[dict] = []
    for path in paths:
        try:
            for line in path.read_text().splitlines():
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        found.append(item)
                except ValueError:
                    continue
        except OSError:
            continue
    return found


def main_checkout() -> Path | None:
    """The primary checkout of the current repository.

    In a worktree the canonical `.hindsight/` belongs to the main checkout, so
    this resolves `--git-common-dir`. A submodule's common dir lives inside its
    superproject's `.git/modules/`, whose parent is not a working tree; there
    the submodule's own toplevel is the answer.
    """
    root = repository()
    if not root:
        return None
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=1, cwd=root)
        if common.returncode == 0:
            path = Path(common.stdout.strip())
            if path.name == ".git":
                return path.parent
    except (OSError, subprocess.SubprocessError):
        pass
    return root


def listed(path: Path) -> list[str]:
    """Non-comment entries of a one-per-line file (commas and spaces also split)."""
    try:
        text = path.read_text()
    except OSError:
        return []
    names: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        names.extend(part for part in re.split(r"[\s,]+", line) if part)
    return names


def bank() -> str:
    root = main_checkout()
    if root:
        try:
            override = root / ".hindsight/bank"
            if override.is_file():
                for line in override.read_text().splitlines():
                    if line.strip() and not line.lstrip().startswith("#"):
                        return line.strip()
        except OSError:
            pass
    if os.environ.get("HINDSIGHT_BANK"):
        return os.environ["HINDSIGHT_BANK"]
    try:
        remote = subprocess.run(["git", "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=1)
        if remote.returncode == 0:
            return remote.stdout.strip().removesuffix(".git").rsplit("/", 1)[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    return root.name if root else "general"


# The invoking agent's identity. Same signal agent-hooks/core/asm.py uses at its
# rung 2: HERMES_HOME is present in every Hermes process and names the profile
# exactly, which beats any pid. The exclusion set is asm.py's too -- the
# fleet-shared command router presents exactly like a profile but represents
# every PM at once, and it answers to two different names depending on source.
IDENTITY_ENV_FOR_CLI = {"hermes": "HERMES_HOME"}
NOT_AN_AGENT_PROFILE = frozenset({"fleet-bloodbank-gateway", "fleet-bloodbank"})


def agent_profile(cli: str) -> str:
    """The invoking agent's profile name, or "" when the caller is not an agent."""
    env_var = IDENTITY_ENV_FOR_CLI.get(cli)
    if not env_var:
        return ""
    raw = os.environ.get(env_var, "").rstrip("/")
    profile = os.path.basename(raw) if raw else ""
    return profile if profile and profile not in NOT_AN_AGENT_PROFILE else ""


def registry_row(profile: str) -> dict:
    """The agent's registry row, or {}. Never raises -- a recall must not fail a prompt."""
    if not profile:
        return {}
    path = Path(os.environ.get("HERMES_AGENTS_REGISTRY", Path.home() / ".hermes/agents-registry.yaml"))
    try:
        import yaml
        agents = (yaml.safe_load(path.read_text()) or {}).get("agents") or {}
        for key, row in agents.items():
            # The map key and profile_name are identical for all 24 rows today,
            # but the schema does not require it, so match either.
            if isinstance(row, dict) and (key == profile or row.get("profile_name") == profile):
                return row
    except Exception:
        return {}
    return {}


def declared_banks(cli: str) -> tuple[str, list[str]]:
    """`(personal, recall)` as the agent's registry row DECLARES them.

    Both fields existed in the registry and were read by nothing until now:
    `delonet-company-reporter` has declared `write_bank: delonet-company` and
    `recall_banks: [delonet-company, exec-office]` for months while its rendered
    profile config carried only `bank_id_template: agent-{profile}`, so it read
    and wrote the post-keyed bank and its declaration reached nowhere.
    """
    hindsight = registry_row(agent_profile(cli)).get("hindsight") or {}
    personal = str(hindsight.get("write_bank") or "").strip()
    declared = [str(item).strip() for item in (hindsight.get("recall_banks") or []) if str(item).strip()]
    return personal, declared


def bank_at(root: Path) -> str:
    """The bank a given checkout resolves to, by the same rules as `bank()`."""
    try:
        override = root / ".hindsight/bank"
        if override.is_file():
            for line in override.read_text().splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    return line.strip()
        remote = subprocess.run(["git", "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=1, cwd=root)
        if remote.returncode == 0 and remote.stdout.strip():
            return remote.stdout.strip().removesuffix(".git").rsplit("/", 1)[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    return root.name


def bank_is_declared() -> bool:
    """True when the primary bank came from an explicit operator declaration."""
    if os.environ.get("HINDSIGHT_BANK"):
        return True
    root = main_checkout()
    if not root:
        return False
    try:
        return (Path(root) / ".hindsight/bank").is_file()
    except OSError:
        return False


def ancestor_banks(limit: int = 3) -> list[str]:
    """Banks of the superprojects above this checkout, nearest first.

    A submodule's work is also its parent's work -- an hour spent in
    33GOD/flume is an hour of 33GOD. Git has always known the parent
    (`--show-superproject-working-tree` returns it) and nothing has ever asked.
    Bounded, and OPT-IN (`HINDSIGHT_ANCESTRY=1`) since 2026-09-26: every
    extra bank on the synchronous prompt path is another contended rerank,
    and the audit that day found fan-out banks behind 250 of 321 abandoned
    recalls.

    Walks from the root `repository()` resolved, NOT from the process cwd. The
    two diverge whenever the caller is not standing in the repo the recall is
    about -- under test most obviously, but also for any hook invoked with a
    different working directory than the agent's. `bank()` answers for that
    root, so its ancestors must be that root's.
    """
    if os.environ.get("HINDSIGHT_ANCESTRY", "0") != "1":
        return []
    # An explicit bank is a declaration, and ancestry is an inference from git
    # topology. When the operator has named the bank -- via `.hindsight/bank` or
    # $HINDSIGHT_BANK -- they have already said where this work belongs, and
    # walking up to contradict them is exactly the wrong move.
    if bank_is_declared():
        return []
    start = repository()
    if not start:
        return []
    found: list[str] = []
    cwd: str | None = str(start)
    for _ in range(max(0, limit)):
        try:
            walked = subprocess.run(["git", "rev-parse", "--show-superproject-working-tree"],
                                    capture_output=True, text=True, timeout=1, cwd=cwd)
        except (OSError, subprocess.SubprocessError):
            break
        parent = walked.stdout.strip() if walked.returncode == 0 else ""
        if not parent:
            break
        found.append(bank_at(Path(parent)))
        cwd = parent
    return [name for name in found if name]


def repo_recall_banks() -> list[str]:
    """Extra banks this repository opts into, from `<main checkout>/.hindsight/recall-banks`.

    One bank per line, `#` comments allowed. It sits beside the
    `.hindsight/bank` override `bank()` honors, so an infra-flavored repo can
    add `infra` without every other repo paying for it.
    """
    root = main_checkout()
    return listed(root / ".hindsight/recall-banks") if root else []


def recall_banks(cli: str, primary: str) -> list[str]:
    """Every bank the synchronous prompt recall reads, in priority order.

    By default that is ONLY the personal bank (when the agent registry declares
    one) and the primary bank. Everything else is an explicit opt-in, because
    each extra bank is another rerank the prompt waits on:

      * the agent row's `recall_banks` and this repo's `.hindsight/recall-banks`
      * `HINDSIGHT_RECALL_GENERAL=1` for `general`
      * `HINDSIGHT_GLOBAL_BANKS` (default empty; was `infra` until 2026-09-26)
      * `HINDSIGHT_ANCESTRY=1` for superproject banks
      * `HINDSIGHT_FANOUT=1` for dream-graph neighbours

    The personal bank leads and is never dropped by the cap: an agent that
    cannot remember what it has done is the defect this ordering exists to
    prevent.
    """
    cap = max(2, min(12, int(os.environ.get("HINDSIGHT_RECALL_MAX_BANKS", "8") or 8)))
    personal, declared = declared_banks(cli)
    ordered = [
        *([personal] if personal else []),
        primary,
        *ancestor_banks(),
        *declared,
        *repo_recall_banks(),
        *(["general"] if os.environ.get("HINDSIGHT_RECALL_GENERAL") == "1" else []),
        *os.environ.get("HINDSIGHT_GLOBAL_BANKS", "").split(),
        *linked_banks(primary),
    ]
    return list(dict.fromkeys([name for name in ordered if name]))[:cap]


def retain_targets(cli: str, primary: str) -> list[str]:
    """Where a session summary is written: the person, then the project.

    NOT a copy. The same event yields a different memory in each bank because a
    bank's MISSION drives extraction. Verified against `dry-run-extract` with
    one identical session summary:

        agent-33god-pm  ->  "Agent built buildOrgChart ... | Involving: agent"
                            "Agent initially anchored ancestry on the process cwd"
        33GOD           ->  "Built an org chart renderer in tree.ts that reconciles ..."
                            "Inferred edges in the org chart are marked with a tilde"

    Episodic on one side, semantic on the other, from the same bytes. That is
    the whole of "EXPERIENCE to the person, WORLD to the project" -- the API
    exposes no type on write (there is no `--type` on retain and no type field
    on the item schema), so the mission is the only router there is, and it
    turns out to be the right one.

    The person leads so that a partial failure loses the project's copy, which
    a later session can rebuild from the repo, rather than the agent's memory of
    having been there, which nothing can.
    """
    personal, _ = declared_banks(cli)
    return list(dict.fromkeys([name for name in (personal, primary) if name]))


def binary() -> str | None:
    candidate = os.environ.get("HINDSIGHT_BIN") or str(Path.home() / ".local/bin/hindsight")
    return candidate if os.access(candidate, os.X_OK) else shutil.which("hindsight")


def cli_environment(cli: str) -> dict[str, str]:
    return {**os.environ, "HINDSIGHT_AGENT": cli, "NO_COLOR": "1", "FORCE_COLOR": "0",
            "TERM": "dumb", "HINDSIGHT_NO_SPINNER": "1", "HINDSIGHT_DISABLE_SPINNER": "1"}


def linked_banks(primary: str) -> list[str]:
    # Opt-in since 2026-09-26, for the same reason as ancestry.
    if os.environ.get("HINDSIGHT_FANOUT", "0") != "1":
        return []
    path = Path(os.environ.get("HINDSIGHT_DREAM_GRAPH", Path.home() / ".hindsight/dream/bank-graph.json"))
    try:
        items = json.loads(path.read_text()).get("graph", {}).get(primary, [])
        minimum = float(os.environ.get("HINDSIGHT_FANOUT_MIN_SCORE", "0.1"))
        maximum = max(0, min(4, int(os.environ.get("HINDSIGHT_FANOUT_MAX", "2"))))
        return [str(item["bank"]) for item in sorted(items, key=lambda item: float(item.get("score", 0)), reverse=True)
                if item.get("bank") and float(item.get("score", 0)) >= minimum][:maximum]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def recall_text(response: dict) -> list[str]:
    values = response.get("results", response.get("memories", response.get("items", [])))
    if not isinstance(values, list):
        return []
    return [str(item.get("text", item.get("content", ""))).strip()
            for item in values if isinstance(item, dict) and (item.get("text") or item.get("content"))]


# ------------------------------------------------------------ query hygiene
#
# The hub used to send the raw prompt as the recall query. On 2026-09-26, 62%
# of those queries were harness XML or slash commands, and 111 were rejected
# outright ("Query too long: N tokens exceeds maximum of 500"). The query is now
# the user's own words, capped well under the server limit.

MIN_QUERY_CHARS = 24

# Wrappers a harness injects around or instead of what the user typed. The
# whole block is dropped: none of its content is the user's intent.
HARNESS_BLOCKS = frozenset({
    # Claude Code
    "system-reminder", "task-notification", "teammate-message", "agent-message",
    "local-command-caveat", "local-command-stdout", "local-command-stderr",
    "command-name", "command-message", "command-contents",
    "bash-stdout", "bash-stderr", "user-prompt-submit-hook",
    "ide_opened_file", "ide_selection", "ide_diagnostics",
    # Codex
    "send_user_message_question_reply", "environment_context", "user_instructions",
    "recommended_plugins", "in-app-browser-context", "skill", "user_shell_command",
    "hook_prompt", "turn_aborted", "subagent_notification", "codex_internal_context",
    "realtime_delegation",
    # Others
    "environment_details", "system",
})
# Wrappers whose CONTENT is the user's own words: drop the tags, keep the text.
USER_BLOCKS = frozenset({"command-args", "bash-input", "pasted_content"})
# A prompt that is nothing but a harness frame. Nothing in it is a question.
HARNESS_PROMPTS = (
    "This session is being continued from a previous conversation",
    "[Request interrupted by user",
)
HARNESS_LINES = re.compile(r"(?im)^\s*(?:Another Claude session sent a message:"
                           r"|Read the output file to retrieve the result:.*)\s*$")


def _blocks(names: frozenset[str]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    return re.compile(rf"<({alternatives})(?:\s[^>]*)?>(.*?)</\1\s*>", re.S | re.I)


_HARNESS_BLOCK = _blocks(HARNESS_BLOCKS)
_USER_BLOCK = _blocks(USER_BLOCKS)
_ORPHAN_TAG = re.compile(
    r"</?(?:%s)(?:\s[^>]*)?/?>" % "|".join(re.escape(n) for n in sorted(HARNESS_BLOCKS | USER_BLOCKS, key=len, reverse=True)),
    re.I)
# Any other kebab- or snake-case wrapper that OPENS A LINE is a harness frame
# too (`<foo-bar>`, `<foo_bar>`). HTML a user types inline is left alone.
_GENERIC_BLOCK = re.compile(r"^[ \t]*<([a-z][a-z0-9]*(?:[-_][a-z0-9]+)+)(?:\s[^>]*)?>(.*?)</\1\s*>", re.S | re.M)
# `/bmad-build-auto 4-1 ...` -> `4-1 ...`. A path such as `/home/x/y` is not a
# command: the token must end at whitespace, so a second `/` disqualifies it.
_SLASH_COMMAND = re.compile(r"^/[A-Za-z][\w:.-]*(?=\s|$)")


def clean_query(prompt: str) -> str:
    """The user's own words from a native prompt payload."""
    if not isinstance(prompt, str):
        return ""
    text = prompt
    for _ in range(4):  # nested wrappers peel one layer per pass
        stripped = _HARNESS_BLOCK.sub(" ", text)
        stripped = _GENERIC_BLOCK.sub(lambda m: m.group(0) if m.group(1).lower() in USER_BLOCKS else " ", stripped)
        if stripped == text:
            break
        text = stripped
    text = _USER_BLOCK.sub(lambda m: f" {m.group(2)} ", text)
    text = _ORPHAN_TAG.sub(" ", text)
    text = HARNESS_LINES.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _SLASH_COMMAND.sub("", text).strip()
    if any(text.startswith(frame) for frame in HARNESS_PROMPTS):
        return ""
    return text


# ------------------------------------------------------------ query length cap
#
# The server counts cl100k_base tokens and rejects anything over 500. Do NOT
# raise that limit server-side: the reranker caps input at 512 tokens, so long
# queries only buy the most expensive rerank. Cap client-side instead.

_ENCODER: Any = False  # False = not tried yet; None = unavailable


def encoder() -> Any:
    """cl100k_base when tiktoken is importable here, else None (char cap).

    The hub's /usr/bin/python3 has no tiktoken as of 2026-09-26, so the char
    cap is the live path there.
    """
    global _ENCODER
    if _ENCODER is False:
        try:
            import tiktoken
            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODER = None
    return _ENCODER


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, "") or default)))
    except ValueError:
        return default


def query_caps() -> tuple[int, int]:
    """(max tokens, max chars). ~2.5 chars/token covers code, paths and JSON."""
    return (_env_int("HINDSIGHT_RECALL_QUERY_MAX_TOKENS", 400, 16, 480),
            _env_int("HINDSIGHT_RECALL_QUERY_MAX_CHARS", 1000, 64, 1200))


HEAD_SHARE = 0.6  # the ask is usually at the start or the end; keep both
ELISION = " … "


def cap_query(text: str, max_tokens: int | None = None, max_chars: int | None = None) -> str:
    """`text` kept whole when it fits, else its head and tail joined by an elision."""
    tokens, chars = query_caps()
    tokens = max_tokens or tokens
    chars = max_chars or chars
    enc = encoder()
    if enc is not None:
        try:
            ids = enc.encode(text, disallowed_special=())
            if len(ids) <= tokens:
                return text
            budget = max(2, tokens - 2)  # the elision costs a token or two
            head = max(1, int(budget * HEAD_SHARE))
            tail = max(1, budget - head)
            return (enc.decode(ids[:head]).rstrip() + ELISION + enc.decode(ids[-tail:]).lstrip()).strip()
        except Exception:
            pass
    if len(text) <= chars:
        return text
    budget = max(2, chars - len(ELISION))
    head_n = max(1, int(budget * HEAD_SHARE))
    tail_n = max(1, budget - head_n)
    head, tail = text[:head_n], text[-tail_n:]
    # Cut at word boundaries when one is near, never mid-word when avoidable.
    if " " in head[head_n // 2:]:
        head = head.rsplit(" ", 1)[0]
    if " " in tail[:tail_n // 2]:
        tail = tail.split(" ", 1)[1]
    return (head.rstrip() + ELISION + tail.lstrip()).strip()


def halve_query(text: str) -> str:
    """Half of `text`'s current size, for the one retry after a 400."""
    enc = encoder()
    if enc is not None:
        try:
            return cap_query(text, max_tokens=max(8, len(enc.encode(text, disallowed_special=())) // 2))
        except Exception:
            pass
    return cap_query(text, max_chars=max(32, len(text) // 2))


# ------------------------------------------------------------ bounded recall

RECALL_STATUSES = ("ok", "empty", "timeout", "http_400", "not_found", "error")
FAILURE_REASONS = {"timeout": "recall_deadline_exceeded", "http_400": "recall_query_rejected",
                   "error": "recall_command_failed"}
_LIVE: set[subprocess.Popen] = set()
_LIVE_LOCK = threading.Lock()


def recall_deadline() -> float:
    """Seconds, from handler start, for the WHOLE recall.

    The registry kills this handler at 11s (`timeout_ms`), so the default of 8s
    leaves 3s for interpreter start, bank resolution, rendering and the journal.
    """
    try:
        value = float(os.environ.get("HINDSIGHT_RECALL_TIMEOUT", "") or 8.0)
    except ValueError:
        value = 8.0
    return max(0.5, min(10.0, value))


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def run_bounded(args: list[str], env: dict[str, str], deadline_at: float) -> tuple[int | None, str, str]:
    """(returncode, stdout, stderr), or (None, "", "") once `deadline_at` passes.

    The child is killed AT the deadline, so a stalled server can never hold the
    prompt past its budget.
    """
    remaining = deadline_at - time.monotonic()
    if remaining <= 0:
        return None, "", ""
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    except OSError as exc:
        return -1, "", f"spawn failed: {type(exc).__name__}"
    with _LIVE_LOCK:
        _LIVE.add(proc)
    try:
        out, err = proc.communicate(timeout=remaining)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=0.5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return None, "", ""
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(proc)


def classify(code: int | None, out: str, err: str) -> tuple[str, list[str]]:
    if code is None:
        return "timeout", []
    if code == 0:
        try:
            decoded = json.loads(out)
        except ValueError:
            return "error", []
        texts = recall_text(decoded) if isinstance(decoded, dict) else []
        return ("ok" if texts else "empty"), texts
    text = f"{out}\n{err}"
    if re.search(r"\(400\)|\b400 Bad Request", text):
        return "http_400", []
    if re.search(r"\(404\)|\b404 Not Found|\bnot found\b", text, re.I):
        return "not_found", []
    return "error", []


def _detail(out: str, err: str) -> str:
    lines = [line.strip() for line in f"{err}\n{out}".splitlines() if line.strip()]
    # The CLI prints "Server response:" then the body; the body is the reason.
    for index, line in enumerate(lines):
        if line.lower().startswith("server response") and index + 1 < len(lines):
            return lines[index + 1][:200]
    return (lines[-1] if lines else "")[:200]


def recall_one(command: str, target: str, query: str, deep: bool,
               env: dict[str, str], deadline_at: float) -> tuple[dict, list[str]]:
    started = time.monotonic()
    prefer = os.environ.get("HINDSIGHT_RECALL_PREFER_OBSERVATIONS", "1") != "0"

    def args(q: str) -> list[str]:
        # --prefer-observations drops raw facts an observation already covers.
        # Verified on a bank with zero observations: facts still come back.
        return [command, "memory", "recall", target, q, "--output", "json",
                "--budget", "mid" if deep else "low", "--max-tokens", "2048" if deep else "1024",
                *(["--prefer-observations"] if prefer else [])]

    code, out, err = run_bounded(args(query), env, deadline_at)
    status, texts = classify(code, out, err)
    retried = False
    if status == "http_400" and "query too long" in f"{out}\n{err}".lower():
        shorter = halve_query(query)
        if shorter and len(shorter) < len(query):
            retried = True
            code, out, err = run_bounded(args(shorter), env, deadline_at)
            status, texts = classify(code, out, err)
    outcome: dict[str, Any] = {"bank": target, "status": status, "results": len(texts),
                               "latency_ms": round((time.monotonic() - started) * 1000)}
    if retried:
        outcome["retried"] = True
    if status in {"http_400", "not_found", "error"}:
        outcome["detail"] = _detail(out, err)
    return outcome, texts


def recall_many(command: str, banks: list[str], query: str, deep: set[str],
                env: dict[str, str], deadline_at: float) -> list[tuple[dict, list[str]]]:
    """Recall every bank in parallel; return whatever finished by the deadline.

    One thread per bank, each killing its own child at the deadline. Nothing
    here waits on a straggler: a thread still alive after a short grace is
    recorded as a timeout and its process group is killed.
    """
    found: dict[str, tuple[dict, list[str]]] = {}
    began = time.monotonic()

    def work(target: str) -> None:
        try:
            found[target] = recall_one(command, target, query, target in deep, env, deadline_at)
        except Exception:
            found[target] = ({"bank": target, "status": "error", "results": 0,
                              "latency_ms": round((time.monotonic() - began) * 1000),
                              "detail": "recall worker raised"}, [])

    workers = [threading.Thread(target=work, args=(target,), daemon=True) for target in banks]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(max(0.0, deadline_at + 0.25 - time.monotonic()))
    with _LIVE_LOCK:
        stragglers = list(_LIVE)
    for proc in stragglers:
        _kill_tree(proc)
    return [found.get(target) or ({"bank": target, "status": "timeout", "results": 0,
                                   "latency_ms": round((time.monotonic() - began) * 1000)}, [])
            for target in banks]


def recall(payload: dict, cli: str, native: str) -> dict:
    prompt = payload.get("prompt", "") or ""
    query = clean_query(prompt)
    sizes = {"query_len_raw": len(prompt), "query_len_clean": len(query)}
    if len(query) < MIN_QUERY_CHARS:
        reason = ("prompt_under_min_length" if len(prompt.strip()) < MIN_QUERY_CHARS
                  else "query_under_min_length_after_cleanup")
        if payload.get("session_id"):
            journal(payload, cli, {"event": "recall_skipped", "reason": reason, **sizes})
        return result("skipped", reason)
    query = cap_query(query)
    sizes["query_len_sent"] = len(query)
    command = binary()
    if not command:
        return result("skipped", "hindsight_binary_missing")
    primary = bank()
    personal, _ = declared_banks(cli)
    banks = recall_banks(cli, primary)
    budget = recall_deadline()
    # The agent's own memory is worth as much as the project's, so it gets the
    # same budget. Everything else is context, not identity.
    deep = {primary, personal} - {""}
    started = anchor()
    responses = recall_many(command, banks, query, deep, cli_environment(cli), started + budget)
    context: list[str] = []
    for outcome, texts in responses:
        if texts:
            context.append(f"<!-- hindsight:recall bank={outcome['bank']} -->\n" + "\n".join(texts)[:10000] + "\n<!-- /hindsight:recall -->")
    rendered = "\n\n".join(context)[:20000]
    failures = [outcome["status"] for outcome, _ in responses if outcome["status"] in FAILURE_REASONS]
    if payload.get("session_id"):
        journal(payload, cli, {"event": "recall", "bank": primary, "banks": banks,
                              "prompt_len": len(prompt), **sizes,
                              "total_chars": len(rendered), "returned_anything": bool(rendered),
                              "failed_banks": len(failures), "deadline_s": budget,
                              "elapsed_ms": round((time.monotonic() - started) * 1000),
                              "per_bank": [outcome for outcome, _ in responses]})
    if not rendered and failures:
        return result("failed", FAILURE_REASONS[failures[0]], exit_code=1)
    return result("succeeded", "recall_partial" if failures else "recall_completed", context_output(rendered, cli, native))


# ------------------------------------------------------------ session briefing
#
# Mental models are curated, pre-synthesized pages; a GET costs ~20-40ms where
# a recall costs seconds. The bank templates in DeLoContainers
# stacks/ai/hindsight/templates/ seed these ids on every templated bank.

BRIEFING_MODELS = ("briefing", "pitfalls", "rules")


def api_endpoint() -> tuple[str, str]:
    """(base URL, key) from the environment, else ~/.hindsight/config -- as the CLI does."""
    url = os.environ.get("HINDSIGHT_API_URL", "").strip()
    key = os.environ.get("HINDSIGHT_API_KEY", "").strip()
    if not (url and key):
        path = Path(os.environ.get("HINDSIGHT_CONFIG", Path.home() / ".hindsight/config"))
        try:
            config = tomllib.loads(path.read_text())
        except (OSError, ValueError):
            config = {}
        url = url or str(config.get("api_url") or "").strip()
        key = key or str(config.get("api_key") or "").strip()
    return url.rstrip("/"), key


def fetch_model(base: str, key: str, target: str, model: str, timeout: float) -> tuple[str, str, str]:
    """(status, title, content) for one mental model. Never raises."""
    url = (f"{base}/v1/default/banks/{urllib.parse.quote(target, safe='')}"
           f"/mental-models/{urllib.parse.quote(model, safe='')}")
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            data = json.loads(response.read(1 << 20))
    except urllib.error.HTTPError as exc:
        return ("not_found" if exc.code == 404 else f"http_{exc.code}"), "", ""
    except (OSError, ValueError):
        return "error", "", ""
    content = str(data.get("content") or "").strip() if isinstance(data, dict) else ""
    if not content:
        return "empty", "", ""
    # A model that has never finished its first refresh says so in its content.
    if re.match(r"^(?:[^\n:]{0,120}:\s*)?generating content\.*$", content, re.I):
        return "pending", "", ""
    return "ok", str(data.get("name") or model).strip(), content


def briefing(payload: dict, cli: str, native: str) -> dict:
    if os.environ.get("HINDSIGHT_BRIEFING", "1") == "0":
        return result("skipped", "briefing_disabled")
    # A resumed session already carries the briefing it started with.
    if str(payload.get("source", "")).lower() == "resume":
        return result("skipped", "session_resumed")
    base, key = api_endpoint()
    if not base:
        return result("skipped", "hindsight_api_unconfigured")
    try:
        budget = max(0.1, min(2.0, float(os.environ.get("HINDSIGHT_BRIEFING_TIMEOUT", "") or 0.5)))
    except ValueError:
        budget = 0.5
    total_cap = _env_int("HINDSIGHT_BRIEFING_MAX_CHARS", 6000, 500, 20000)
    primary = bank()
    fetched: dict[str, tuple[str, str, str]] = {}
    started = time.monotonic()
    deadline_at = started + budget

    def work(model: str) -> None:
        fetched[model] = fetch_model(base, key, primary, model, max(0.05, deadline_at - time.monotonic()))

    workers = [threading.Thread(target=work, args=(model,), daemon=True) for model in BRIEFING_MODELS]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(max(0.0, deadline_at - time.monotonic()))
    statuses = {model: fetched.get(model, ("timeout", "", ""))[0] for model in BRIEFING_MODELS}
    ready = [(model, *fetched[model][1:]) for model in BRIEFING_MODELS if statuses[model] == "ok"]
    rendered = ""
    if ready:
        share = max(400, total_cap // len(ready))
        sections = []
        for model, title, content in ready:
            body = content if len(content) <= share else content[:share].rsplit("\n", 1)[0] + "\n…(truncated)"
            sections.append(f"## {title}\n\n{body}")
        rendered = (f"<!-- hindsight:briefing bank={primary} -->\n"
                    f"# Hindsight briefing: bank `{primary}`\n"
                    "Standing mental models for this project. Per-prompt recall adds detail.\n\n"
                    + "\n\n".join(sections))[:total_cap + 400] + "\n<!-- /hindsight:briefing -->"
    if payload.get("session_id"):
        journal(payload, cli, {"event": "briefing", "bank": primary, "models": statuses,
                              "total_chars": len(rendered),
                              "elapsed_ms": round((time.monotonic() - started) * 1000)})
    if not rendered:
        return result("skipped", "no_briefing_models")
    return result("succeeded", "briefing_injected", context_output(rendered, cli, native))


def candidate(payload: dict, cli: str) -> dict:
    if not payload.get("session_id"):
        return result("skipped", "native_session_id_missing")
    tool_response = payload.get("tool_response", payload.get("tool_result", payload.get("result", {})))
    failed_response = isinstance(tool_response, dict) and (
        tool_response.get("error") or tool_response.get("is_error") or tool_response.get("success") is False
    )
    if payload.get("error") or payload.get("is_error") or failed_response or str(payload.get("hook_event_name", "")).lower().endswith("failure"):
        return result("skipped", "tool_failed")
    edits = [(path, content) for path, content in file_edits(payload) if len(content) >= 50]
    if not edits:
        return result("skipped", "no_substantial_file_edit")
    primary = bank()
    # Each native invocation becomes one candidate row; several file paths in
    # apply_patch are retained together without losing the full edit identity.
    journal(payload, cli, {"event": "retain_candidate", "bank": primary,
                          "files": [path for path, _ in edits],
                          "edits": [{"file": path, "snippet": " ".join(content.split())[:400]} for path, content in edits],
                          "source": "auto_posttooluse"})
    return result("succeeded", "edit_candidate_recorded")


def end(payload: dict, cli: str) -> dict:
    if not payload.get("session_id"):
        return result("skipped", "native_session_id_missing")
    command = binary()
    if not command:
        return result("skipped", "hindsight_binary_missing")
    path = journal_path(payload, cli)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".retain.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        history = records(payload, cli)
        edits: list[str] = []
        for item in history:
            if item.get("event") == "retain_candidate":
                edits.extend(f"- {edit.get('file')}: {edit.get('snippet', '')}" for edit in item.get("edits", []))
            elif item.get("event") == "retain" and item.get("source") == "auto_posttooluse":
                edits.append(f"- {item.get('file')}: {item.get('snippet', '')}")
        summary = "Session completed. Files edited:\n" + "\n".join(dict.fromkeys(edits)) if edits else ""
        assistant = payload.get("last_assistant_message", "")
        if isinstance(assistant, str) and len(assistant.strip()) >= 50:
            summary += "\nSession outcome:\n" + assistant[-8000:]
        summary = summary.strip()[:20000]
        if not summary:
            return result("skipped", "no_retention_candidates")
        fingerprint = hashlib.sha256(summary.encode()).hexdigest()
        primary = bank()
        targets = retain_targets(cli, primary)
        # Per BANK, not per fingerprint. The same summary is retained once into
        # each target, and having landed in one says nothing about the other --
        # a single shared check would let a partial failure look complete.
        settled = {item.get("bank") for item in history
                   if item.get("event") == "retain_receipt" and item.get("retained")
                   and item.get("fingerprint") == fingerprint}
        pending = [target for target in targets if target not in settled]
        if not pending:
            return result("skipped", "session_summary_already_retained")
        doc_id = "hook-session-" + hashlib.sha256(f"{cli}:{payload['session_id']}".encode()).hexdigest()[:32]
        tags = f"user:{safe(os.environ.get('HINDSIGHT_USER', os.environ.get('USER', 'unknown')))},agent:{safe(cli)},host:{safe(socket.gethostname().split('.')[0])}"

        def store(target: str) -> tuple[str, bool, str, dict]:
            try:
                process = subprocess.run([command, "memory", "retain", target, summary, "--context", "session-summary",
                                          "--doc-id", doc_id, "--output", "json", "--document-tags", tags],
                                         capture_output=True, text=True, timeout=45, env=cli_environment(cli))
                response = json.loads(process.stdout) if process.returncode == 0 else {}
            except subprocess.TimeoutExpired:
                return target, False, "retain_deadline_exceeded", {}
            except (OSError, ValueError):
                return target, False, "retain_response_invalid", {}
            accepted = (process.returncode == 0 and isinstance(response, dict) and bool(response)
                        and not response.get("error") and response.get("success") is not False)
            return target, accepted, "" if accepted else "retain_command_failed", response

        # Concurrent for the same reason recall is: two sequential retains put a
        # 90s ceiling on a session-end hook, and the banks are independent.
        if len(pending) == 1:
            outcomes = [store(pending[0])]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(pending)) as pool:
                outcomes = list(pool.map(store, pending))

        for target, accepted, reason, response in outcomes:
            journal(payload, cli, {"event": "retain_receipt", "bank": target, "retained": accepted,
                                  "fingerprint": fingerprint, "document_id": doc_id,
                                  **({"reason": reason} if reason else {}),
                                  "response_keys": sorted(response) if isinstance(response, dict) else []})
        stored = [target for target, accepted, _, _ in outcomes if accepted]
        failures = [(target, reason) for target, accepted, reason, _ in outcomes if not accepted]
        if not stored:
            return result("failed", failures[0][1] or "retain_command_failed", exit_code=1)
        # A partial write is a SUCCESS with a named gap, not a failure: the
        # session ended and what landed is real. Reporting it as failed would
        # invite a retry that re-retains the bank that already accepted.
        return result("succeeded",
                      "session_summary_retained" if not failures else "session_summary_retained_partially",
                      exit_code=0)


def dispatch(concern: str, payload: dict, cli: str, native: str) -> dict:
    if os.environ.get("DISABLE_HINDSIGHT_HOOKS") == "1":
        return result("skipped", "hindsight_disabled")
    if concern == "hindsight-recall":
        return recall(payload, cli, native)
    if concern == "hindsight-briefing":
        return briefing(payload, cli, native)
    if concern == "hindsight-retain":
        return candidate(payload, cli)
    if concern == "hindsight-session-end":
        return end(payload, cli)
    if concern == "hindsight-journal":
        if not payload.get("session_id") or not journal_path(payload, cli).exists():
            return result("skipped", "session_has_no_memory_journal")
        normalized = dict(payload, session_id=session_key(payload, cli))
        return invoke(["~/.agents/hooks/hindsight/hindsight-journal-write.sh"], normalized, timeout=5)
    if concern == "hindsight-jot-flush":
        root = Path(os.environ.get("HS_JOURNAL_DIR", Path.home() / ".agents/journal"))
        key = hashlib.sha1(str(payload.get("cwd", os.getcwd())).encode()).hexdigest()[:12]
        jotfile = root / "jots" / f"{key}.md"
        jotfile.parent.mkdir(parents=True, exist_ok=True)
        with jotfile.with_suffix(".flush.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not jotfile.is_file() or not jotfile.stat().st_size:
                return result("skipped", "no_pending_jots")
            (jotfile.parent / "flushed").mkdir(exist_ok=True)
            output = invoke(["~/.agents/hooks/hindsight/hindsight-jot-flush.sh", "--jotfile", str(jotfile)], payload,
                            timeout=180, environment=cli_environment(cli))
            if output["_hook_hub"]["status"] == "succeeded" and jotfile.exists():
                return result("skipped", "jots_remain_pending")
            return output
    return result("failed", "unknown_hindsight_concern", exit_code=1)
