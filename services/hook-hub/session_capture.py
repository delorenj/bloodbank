"""Session write-back: what an agent session DECIDED and DID, not the code it typed.

Replaces the old SessionEnd path, which retained "Session completed. Files
edited:" plus 400-char code excerpts, never the outcome (SessionEnd payloads
carry only `reason`), and nothing at all for most sessions and every Codex one.

Per turn, cheaply and locally:

  prompt_submit   the user's ask, cleaned with the recall path's harness-XML
                  hygiene (`hindsight.clean_query`), is buffered.
  post_tool       the PATHS of edited files are buffered. Never their content.
  turn end        the agent's final message (the native payload's
                  `last_assistant_message`, else the tail of the transcript)
                  closes the turn: ask + outcome + files. Trivial turns are
                  dropped.

The buffer is a small JSON state file per session. It is written to Hindsight
over HTTP as ONE document per session (`session-<cli>-<session>`) with
`update_mode="append"`, `observation_scopes="shared"` and `async=true`:

  * when the buffer passes HINDSIGHT_CAPTURE_FLUSH_CHARS (a long session does
    not wait for its end),
  * at session end,
  * and from the sweeper, for sessions that went idle or died without a
    SessionEnd, and for batches whose delivery is unconfirmed or failed.

Each flush is a JSON conversation array. That is deliberate: on append the
server merges the stored array with the new one and re-chunks it at turn
boundaries, which is prefix-stable, so a flush re-extracts only the stored
document's last chunk plus the new turns. The same content as plain text is
re-chunked as one joined string, finds no unchanged chunk from the third
append on, and falls back to a full re-ingest that also invalidates every
observation the document fed (measured on 0.10.1, 2026-09-26; see README).

Delivery is at-least-once with a deterministic `operation_id` per batch and
attempt, so re-sending after a lost acknowledgement never enqueues twice. A
batch is only forgotten once the server reports its operation `completed`;
failures back off and retry, and after HINDSIGHT_CAPTURE_MAX_ATTEMPTS the
exact request is written to a dead-letter file instead of being dropped.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hindsight as hs  # noqa: E402  (shared bank resolution, hygiene, journal)
from concerns import result, value  # noqa: E402

# CLIs whose turn end yields the final assistant message, directly or through a
# transcript this module can read. The registry rows narrow to the same set.
#   claude       Stop.last_assistant_message (2.1.283 schema); transcript fallback
#   codex        Stop.last_assistant_message (0.157 schema); rollout fallback
#   kimi         Stop has neither; the session's wire.jsonl is found by id
#   antigravity  Stop (bound to session_end) carries transcriptPath
#   copilot      agentStop carries transcriptPath (events.jsonl)
#   gemini       AfterAgent carries prompt_response
# Not covered: hermes (on_session_end carries no message; its own plugin
# retains every turn to the agent bank) and opencode (session.idle carries
# neither a message nor a transcript path).
CAPTURE_CLIS = frozenset({"claude", "codex", "kimi", "antigravity", "copilot", "gemini"})

NAMESPACE = uuid.UUID("5b0f7a52-3c1e-4d59-9a57-7e0c5f1d2a61")
STATE_VERSION = 1

EDIT_TOOLS = re.compile(
    r"^(?:Write|Edit|MultiEdit|NotebookEdit|apply_patch|write_file|edit_file|create_file|create|edit|write|"
    r"patch|replace|replace_in_file|insert_code_at|str_replace|str_replace_editor|str_replace_based_edit_tool|"
    r"write_to_file|replace_file_content|multi_replace_file_content|WriteFile|StrReplaceFile|EditFile)$", re.I)
PATH_KEYS = ("file_path", "path", "absolute_path", "target_file", "TargetFile", "notebook_path", "filePath")
# apply_patch file headers, raw or inside a JS/JSON string literal (Codex now
# wraps edits as `exec` code calling tools.apply_patch("*** Begin Patch\n...")).
PATCH_FILE = re.compile(r"\*\*\* (?:Add File|Update File|Delete File|Move to): ([^\n\r\"'`]+?)\s*(?=\\[nr]|[\n\r\"'`]|$)")
# A file authored through a shell heredoc (Codex often writes new files this
# way): `cat > f <<EOF`, `cat >> f <<EOF`, `cat <<'EOF' > f`, `tee [-a] f <<EOF`.
_TARGET = r"([^\s;&|<>'\"`$()]+)"
HEREDOC_WRITE = (
    re.compile(r"(?:^|[\s;&|(])(?:cat\s*>{1,2}|tee\s+(?:-a\s+)?)\s*" + _TARGET + r"\s*<<"),
    re.compile(r"(?:^|[\s;&|(])cat\s*<<-?\s*['\"]?\w+['\"]?\s*>{1,2}\s*" + _TARGET),
)
NOT_A_WORK_FILE = re.compile(r"^(?:/dev/|/proc/|/tmp/|/var/tmp/)")


def _now() -> float:
    return time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _int(name: str, default: int, low: int, high: int) -> int:
    return hs._env_int(name, default, low, high)


def limits() -> dict[str, int]:
    return {
        "flush_chars": _int("HINDSIGHT_CAPTURE_FLUSH_CHARS", 9000, 200, 100000),
        "idle_s": _int("HINDSIGHT_CAPTURE_IDLE_S", 7200, 30, 7 * 86400),
        "ask_chars": _int("HINDSIGHT_CAPTURE_ASK_CHARS", 1200, 100, 4000),
        "outcome_chars": _int("HINDSIGHT_CAPTURE_OUTCOME_CHARS", 2400, 200, 8000),
        "min_outcome": _int("HINDSIGHT_CAPTURE_MIN_OUTCOME", 80, 0, 2000),
        "max_attempts": _int("HINDSIGHT_CAPTURE_MAX_ATTEMPTS", 8, 1, 50),
        "max_batches": _int("HINDSIGHT_CAPTURE_MAX_BATCHES", 20, 1, 500),
        "max_age_s": _int("HINDSIGHT_CAPTURE_MAX_AGE_S", 3 * 86400, 3600, 30 * 86400),
    }


# Seconds before retry N (1-based). ~34h in total, which outlives a spent
# daily OpenRouter cap (it resets at 00:00 UTC) wherever in the day it runs out.
BACKOFF = (60, 300, 900, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 12 * 3600)
CONFIRM_AFTER_S = 30     # an async retain usually finishes in 10-20s
RECHECK_S = 60           # do not poll one operation more often than this
SENDING_STALE_S = 90     # a claimed send whose process died


def backoff(attempt: int) -> int:
    return BACKOFF[min(max(attempt, 1), len(BACKOFF)) - 1]


# --------------------------------------------------------------------- storage

def capture_dir() -> Path:
    configured = os.environ.get("HINDSIGHT_CAPTURE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    state = os.environ.get("XDG_STATE_HOME", "").strip() or str(Path.home() / ".local/state")
    return Path(state) / "33god/hook-hub/capture"


def state_path(cli: str, session_id: str) -> Path:
    return capture_dir() / f"{hs.safe(cli)}-{hs.safe(session_id)}.json"


@contextmanager
def locked(path: Path, blocking: bool = True) -> Iterator[bool]:
    """An exclusive lock on `path`'s sidecar. Yields False when non-blocking and busy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load(path: Path) -> dict | None:
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) and state.get("v") == STATE_VERSION else None


def save(path: Path, state: dict) -> None:
    """Atomic, and private like the session journal: it holds prompts and answers."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(json.dumps(state, ensure_ascii=False))
    os.replace(temporary, path)


def remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- session state

def _host() -> str:
    return hs.safe(socket.gethostname().split(".")[0])


def _repo_label(cwd: str) -> str:
    """The repository's own name (its origin's), not the checkout directory's."""
    root = hs.main_checkout()
    if root:
        try:
            remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root,
                                    capture_output=True, text=True, timeout=1)
            if remote.returncode == 0 and remote.stdout.strip():
                return remote.stdout.strip().removesuffix(".git").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        except (OSError, subprocess.SubprocessError):
            pass
        return root.name
    if cwd and Path(cwd).resolve() == Path.home().resolve():
        return "~"
    return Path(cwd).name if cwd else "unknown"


def new_state(payload: dict, cli: str) -> dict:
    """A session's first event fixes its bank, repo and document for good."""
    cwd = str(payload.get("cwd") or "")
    session = str(payload["session_id"])
    now = _now()
    # Bank resolution reads the process cwd, which for Antigravity is its hook
    # config directory; the workspace the session is about is the payload's.
    previous = os.getcwd() if os.path.isdir(".") else ""
    moved = bool(cwd) and os.path.isdir(cwd) and os.path.realpath(cwd) != os.path.realpath(previous or "/")
    try:
        if moved:
            os.chdir(cwd)
        bank, repo = hs.bank(), _repo_label(cwd)
        root = str(hs.main_checkout() or hs.repository() or "")
    finally:
        if moved and previous:
            try:
                os.chdir(previous)
            except OSError:
                pass
    return {
        "v": STATE_VERSION, "cli": cli, "session_id": session, "bank": bank,
        "repo": repo, "root": root, "cwd": cwd, "host": _host(),
        "document_id": f"session-{hs.safe(cli)}-{hs.safe(session)}",
        "created_at": now, "updated_at": now, "header_sent": False, "closed": False,
        "pending_asks": [], "prompt_seen": False, "pending_files": [],
        "turns": [], "next_turn": 1, "last_turn_key": "",
        "batches": [], "confirmed_turns": 0, "dead_lettered_turns": 0,
    }


@contextmanager
def session(payload: dict, cli: str) -> Iterator[tuple[Path, dict]]:
    path = state_path(cli, str(payload["session_id"]))
    with locked(path):
        state = load(path) or new_state(payload, cli)
        yield path, state
        save(path, state)


def journal(state: dict, event: dict) -> None:
    try:
        hs.journal({"session_id": state["session_id"]}, state["cli"], event)
    except OSError:
        pass


# ------------------------------------------------------------------- text shape

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def elide_code(text: str) -> str:
    """Long fenced blocks become a marker: this memory is decisions, not code."""
    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        lines = body.count("\n")
        if lines <= 6 and len(body) <= 500:
            return match.group(0)
        return f"[code block elided: {lines} lines]"
    return _FENCE.sub(replace, text)


def tidy(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clip(text: str, limit: int, head_share: float = 0.6) -> str:
    """`text` whole when it fits, else its head and tail around an elision."""
    if len(text) <= limit:
        return text
    marker = " […] "
    budget = max(2, limit - len(marker))
    head_n = max(1, int(budget * head_share))
    tail_n = max(1, budget - head_n)
    head, tail = text[:head_n], text[-tail_n:]
    if " " in head[head_n // 2:]:
        head = head.rsplit(" ", 1)[0]
    if " " in tail[:tail_n // 2]:
        tail = tail.split(" ", 1)[1]
    return head.rstrip() + marker + tail.lstrip()


def clean_ask(prompt: Any, limit: int) -> str:
    return clip(hs.clean_query(prompt if isinstance(prompt, str) else ""), limit)


def clean_outcome(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    return clip(tidy(elide_code(text)), limit)


def relative(path: str, state: dict) -> str:
    raw = str(path).strip().strip("'\"")
    if not raw:
        return ""
    candidate = Path(raw).expanduser()
    for root in (state.get("root"), state.get("cwd")):
        if root and candidate.is_absolute():
            try:
                return str(candidate.resolve().relative_to(Path(root).resolve()))
            except (ValueError, OSError):
                continue
    home = str(Path.home())
    return "~" + raw[len(home):] if raw.startswith(home + "/") else raw


def _strings(node: Any, budget: list[int]) -> Iterator[str]:
    if budget[0] <= 0:
        return
    if isinstance(node, str):
        budget[0] -= 1
        yield node
    elif isinstance(node, dict):
        for item in node.values():
            yield from _strings(item, budget)
    elif isinstance(node, list):
        for item in node[:50]:
            yield from _strings(item, budget)


def edit_paths(payload: dict) -> list[str]:
    """Files a tool call edited, from the call's input. Never their content."""
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    name = str(payload.get("tool_name") or "")
    found: list[str] = []
    if EDIT_TOOLS.match(name):
        path = value(args, *PATH_KEYS)
        if isinstance(path, str) and path:
            found.append(path)
    for text in _strings(args, [200]):
        if "*** " in text and "File:" in text or "*** Move to:" in text:
            found.extend(match.strip() for match in PATCH_FILE.findall(text))
        if "<<" in text:
            for pattern in HEREDOC_WRITE:
                found.extend(pattern.findall(text))
    return list(dict.fromkeys(item for item in found if item and not NOT_A_WORK_FILE.match(item)))


# ------------------------------------------------------------ final messages

def _tail_lines(path: Path, limit: int = 2 << 20) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if size > limit else lines


def _text_blocks(content: Any, kinds: tuple[str, ...] = ("text", "output_text", "input_text")) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(block.get("text", "")) for block in content
                         if isinstance(block, dict) and block.get("type") in kinds and block.get("text"))
    return ""


_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)


def read_transcript(path: Path) -> tuple[str, str, str]:
    """(last ask, final assistant message, dedupe key) from a transcript tail.

    Understands Claude and Codex session JSONL, Copilot events.jsonl,
    Antigravity transcript.jsonl and Kimi wire.jsonl. Best effort: unknown
    lines are ignored and ("", "", "") means nothing usable was found.
    """
    ask = outcome = key = ""
    kimi_parts: list[tuple[str, str, str]] = []
    for line in _tail_lines(path):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        if kind == "assistant" and message:                                   # Claude
            text = _text_blocks(message.get("content"), ("text",))
            if text.strip() and not row.get("isApiErrorMessage"):
                outcome, key = text, str(row.get("uuid") or "")
        elif kind == "user" and message and not row.get("isMeta"):
            content = message.get("content")
            if isinstance(content, str) or (isinstance(content, list) and not any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content)):
                ask = _text_blocks(content, ("text",))
        elif kind == "response_item" and payload.get("type") == "message":   # Codex
            text = _text_blocks(payload.get("content"))
            if payload.get("role") == "assistant" and text.strip():
                outcome, key = text, str(payload.get("id") or "")
            elif payload.get("role") == "user" and text.strip():
                ask = text
        elif kind == "event_msg" and payload.get("type") == "agent_message":
            if str(payload.get("message") or "").strip():
                outcome = str(payload["message"])
        elif kind == "assistant.message" and data:                            # Copilot
            if str(data.get("content") or "").strip():
                outcome, key = str(data["content"]), str(data.get("messageId") or "")
        elif kind == "user.message" and data:
            ask = str(data.get("content") or "")
        elif kind == "PLANNER_RESPONSE":                                      # Antigravity
            if str(row.get("content") or "").strip() and not row.get("tool_calls"):
                outcome, key = str(row["content"]), f"step:{row.get('step_index')}"
        elif kind == "USER_INPUT":
            text = str(row.get("content") or "")
            match = _USER_REQUEST.search(text)
            ask = match.group(1) if match else text
        elif kind == "context.append_loop_event":                             # Kimi
            event = row.get("event") if isinstance(row.get("event"), dict) else {}
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            if event.get("type") == "content.part" and part.get("type") == "text" and part.get("text"):
                kimi_parts.append((str(event.get("turnId")), str(event.get("stepUuid")), str(part["text"])))
        elif kind == "turn.prompt":
            ask = _text_blocks(row.get("input"), ("text",))
    if kimi_parts:
        turn, step, _ = kimi_parts[-1]
        outcome = "\n".join(text for t, s, text in kimi_parts if t == turn and s == step)
        key = f"turn:{turn}:{step}"
    return ask, outcome, key


def kimi_wire(session_id: str) -> Path | None:
    root = Path(os.environ.get("KIMI_CODE_HOME", Path.home() / ".kimi-code")).expanduser() / "sessions"
    for match in root.glob(f"*/session_{hs.safe(session_id)}/agents/main/wire.jsonl"):
        return match
    return None


def final_message(payload: dict, cli: str) -> tuple[str, str, str, str]:
    """(outcome, transcript ask, dedupe key, source)."""
    turn = str(payload.get("turn_id") or "")
    text = payload.get("last_assistant_message")
    if isinstance(text, str) and text.strip():
        return text, "", (f"turn:{turn}" if turn else ""), "payload"
    candidate = payload.get("transcript_path") or ""
    path = Path(str(candidate)).expanduser() if candidate else None
    if cli == "kimi" and not (path and path.is_file()):
        path = kimi_wire(str(payload.get("session_id", "")))
    if path and path.is_file():
        ask, outcome, key = read_transcript(path)
        return outcome, ask, (f"turn:{turn}" if turn else key), "transcript"
    return "", "", "", "none"


# ------------------------------------------------------------------ handlers

def enabled(cli: str) -> str:
    if os.environ.get("HINDSIGHT_CAPTURE", "1") == "0":
        return "capture_disabled"
    if cli not in CAPTURE_CLIS:
        return "cli_not_captured"
    return ""


def record_ask(payload: dict, cli: str) -> dict:
    skip = enabled(cli)
    if skip:
        return result("skipped", skip)
    if not payload.get("session_id"):
        return result("skipped", "native_session_id_missing")
    ask = clean_ask(payload.get("prompt", ""), limits()["ask_chars"])
    with session(payload, cli) as (_, state):
        state["prompt_seen"] = True
        state["closed"] = False
        state["updated_at"] = _now()
        if not ask:
            return result("skipped", "ask_is_harness_only")
        # A queued follow-up joins the ask it followed; three is plenty.
        state["pending_asks"] = [*state["pending_asks"], ask][-3:]
    return result("succeeded", "ask_buffered")


def record_edit(payload: dict, cli: str) -> dict:
    skip = enabled(cli)
    if skip:
        return result("skipped", skip)
    if not payload.get("session_id"):
        return result("skipped", "native_session_id_missing")
    response = payload.get("tool_response", payload.get("tool_result", payload.get("result", {})))
    failed = isinstance(response, dict) and (response.get("error") or response.get("is_error")
                                             or response.get("success") is False)
    if payload.get("error") or payload.get("is_error") or failed or \
            str(payload.get("hook_event_name", "")).lower().endswith("failure"):
        return result("skipped", "tool_failed")
    paths = edit_paths(payload)
    if not paths:
        return result("skipped", "no_file_edit")
    with session(payload, cli) as (_, state):
        files = [relative(path, state) for path in paths]
        state["pending_files"] = list(dict.fromkeys([*state["pending_files"], *filter(None, files)]))[-200:]
        state["updated_at"] = _now()
    return result("succeeded", "edit_paths_recorded")


def _files_line(files: list[str], budget: int = 500) -> str:
    shown: list[str] = []
    used = 0
    for index, name in enumerate(files):
        if used + len(name) + 2 > budget:
            return "Files edited: " + ", ".join(shown) + f", and {len(files) - index} more"
        shown.append(name)
        used += len(name) + 2
    return "Files edited: " + ", ".join(shown)


def record_turn(payload: dict, cli: str, native: str) -> dict:
    skip = enabled(cli)
    if skip:
        return result("skipped", skip)
    if not payload.get("session_id"):
        return result("skipped", "native_session_id_missing")
    if native == "Interrupt":
        return result("skipped", "turn_interrupted")
    if payload.get("fullyIdle") is False:  # Antigravity: not the end of the turn yet
        return result("skipped", "turn_not_idle")
    caps = limits()
    raw, transcript_ask, key, source = final_message(payload, cli)
    outcome = clean_outcome(raw, caps["outcome_chars"])
    flush_due = False
    with session(payload, cli) as (path, state):
        now = _now()
        state["updated_at"] = now
        state["closed"] = False
        asks = list(state["pending_asks"])
        if not asks and not state.get("prompt_seen") and transcript_ask:
            # No prompt hook (Antigravity) or it was missed: take the transcript's.
            fallback = clean_ask(transcript_ask, caps["ask_chars"])
            asks = [fallback] if fallback else []
        files = list(state["pending_files"])
        key = key or ("sha:" + hashlib.sha256(outcome.encode()).hexdigest()[:16])
        reason = ""
        if not outcome:
            reason = "no_final_message"
        elif key == state.get("last_turn_key"):
            reason = "turn_already_captured"
        elif len(outcome) < caps["min_outcome"] and not files:
            reason = "turn_trivial"
        if reason:
            if reason != "turn_already_captured":
                state["pending_asks"], state["prompt_seen"] = [], False
            journal(state, {"event": "turn_skipped", "reason": reason, "source": source,
                            "outcome_chars": len(outcome)})
            return result("skipped", reason)
        turn = {"n": state["next_turn"], "ts": _iso(now), "ask": " / ".join(asks),
                "outcome": outcome, "files": files}
        state["turns"].append(turn)
        state["next_turn"] += 1
        state["last_turn_key"] = key
        state["pending_asks"], state["pending_files"], state["prompt_seen"] = [], [], False
        buffered = len(json.dumps(conversation(state, state["turns"], header=False), ensure_ascii=False))
        journal(state, {"event": "turn_captured", "turn": turn["n"], "source": source,
                        "ask_chars": len(turn["ask"]), "outcome_chars": len(outcome),
                        "files": len(files), "buffered_chars": buffered})
        # Past the size threshold, or this session has its own batch to retry
        # or confirm (the sweep below skips the session it runs in).
        flush_due = buffered >= caps["flush_chars"] or any(_due(state, b, now) for b in state["batches"])
    outcome_reason = "turn_buffered"
    if flush_due:
        summary = flush(path, trigger="size")
        if summary.get("result") not in {None, "none"}:
            outcome_reason = "turn_buffered_flush_" + summary["result"]
    sweep(exclude=path)
    return result("succeeded", outcome_reason)


def end_session(payload: dict, cli: str) -> dict:
    skip = enabled(cli)
    if skip:
        return result("skipped", skip)
    if not payload.get("session_id"):
        return result("skipped", "native_session_id_missing")
    path = state_path(cli, str(payload["session_id"]))
    with locked(path):
        state = load(path)
        if state is None:
            return result("skipped", "no_captured_turns")
        state["closed"] = True
        state["updated_at"] = _now()
        save(path, state)
    summary = flush(path, trigger="session_end", force=True)
    sweep(exclude=path)
    status = summary.get("result", "none")
    if status == "failed":
        return result("failed", "session_flush_deferred_for_retry", exit_code=1)
    if status == "none":
        return result("skipped", "nothing_to_flush")
    return result("succeeded", f"session_flush_{status}")


# ----------------------------------------------------------------- delivery

def conversation(state: dict, turns: list[dict], header: bool) -> list[dict]:
    messages: list[dict] = []
    if header:
        started = datetime.fromtimestamp(state["created_at"]).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        messages.append({"role": "system", "content":
                         f"Session in {state['repo']} ({state['cli']}) on {state['host']}, started {started}."})
    for turn in turns:
        if turn.get("ask"):
            messages.append({"role": "user", "content": turn["ask"]})
        body = turn["outcome"]
        if turn.get("files"):
            body = f"{body}\n\n{_files_line(turn['files'])}"
        messages.append({"role": "assistant", "content": body})
    return messages


def strategy() -> str:
    return os.environ.get("HINDSIGHT_SESSION_STRATEGY", "conversation").strip()


def bank_strategy(base: str, key: str, bank: str) -> str:
    """The configured strategy name, if this bank defines it; else "".

    The server logs a WARNING for every retain naming a strategy the bank does
    not have, and almost no bank defines one yet. So the name is only sent
    once a bank template adds it, with no change here.
    """
    wanted = strategy()
    if not wanted:
        return ""
    code, response = _http("GET", _bank_url(base, bank) + "/config", key, timeout=3.0)
    if code != 200 or not isinstance(response, dict):
        return ""
    config = response.get("config") if isinstance(response.get("config"), dict) else response
    defined = config.get("retain_strategies") or {}
    return wanted if isinstance(defined, dict) and wanted in defined else ""


def request_body(state: dict, batch: dict) -> dict:
    """The exact retain request for one batch. Deterministic per batch and attempt."""
    item: dict[str, Any] = {
        "content": json.dumps(conversation(state, batch["turns"], batch.get("header", False)), ensure_ascii=False),
        "context": (f"Agent session in {state['repo']} ({state['cli']}): each user message is a request, "
                    "each assistant message is the agent's final answer for that turn, "
                    "with the paths of files it edited"),
        "timestamp": batch["turns"][0]["ts"],
        "document_id": state["document_id"],
        "update_mode": "append",
        # One untagged scope per bank (= per repo): what claude and codex learn
        # about a repo consolidates together instead of forking by provenance.
        "observation_scopes": "shared",
        "tags": [f"agent:{hs.safe(state['cli'])}", f"host:{state['host']}"],
        "metadata": {"source": "hook-hub/session-capture", "cli": state["cli"],
                     "session_id": state["session_id"], "repo": state["repo"], "host": state["host"]},
    }
    if batch.get("strategy"):
        item["strategy"] = batch["strategy"]
    return {"items": [item], "async": True, "operation_id": operation_id(state, batch)}


def operation_id(state: dict, batch: dict) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{state['bank']}/{state['document_id']}/"
                                     f"{batch['turns'][0]['n']}-{batch['turns'][-1]['n']}/{batch['attempt']}"))


def _http(method: str, url: str, key: str, body: dict | None = None,
          timeout: float = 8.0) -> tuple[int | None, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method),
                                    timeout=timeout) as response:
            raw = response.read(1 << 20)
            try:
                return response.status, json.loads(raw or b"null")
            except ValueError:
                return response.status, raw.decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(2000).decode("utf-8", "replace")
        except OSError:
            detail = ""
        return exc.code, detail[:300]
    except urllib.error.URLError as exc:
        refused = isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror))
        return (0 if refused else None), type(exc.reason).__name__
    except (OSError, ValueError) as exc:
        return None, type(exc).__name__


def _bank_url(base: str, bank: str) -> str:
    return f"{base}/v1/default/banks/{urllib.parse.quote(bank, safe='')}"


def send(base: str, key: str, state: dict, batch: dict) -> tuple[str, str]:
    """('submitted' | 'retry' | 'unknown', detail) for one POST."""
    code, response = _http("POST", _bank_url(base, state["bank"]) + "/memories", key, request_body(state, batch))
    if code and 200 <= code < 300 and isinstance(response, dict) and response.get("success") is not False:
        return "submitted", str(response.get("operation_id") or "")
    if code is None:
        # The request may have landed; the next pass asks the server first.
        return "unknown", str(response)
    return "retry", f"http_{code}: {str(response)[:200]}" if code else f"unreachable: {response}"


def check(base: str, key: str, state: dict, batch: dict) -> tuple[str, str]:
    """The server's view of a batch's operation."""
    code, response = _http("GET", _bank_url(base, state["bank"]) + f"/operations/{operation_id(state, batch)}",
                           key, timeout=5.0)
    if code == 404:
        return "not_found", ""
    if code == 200 and isinstance(response, dict):
        return str(response.get("status") or "unknown"), str(response.get("error_message") or "")[:300]
    return "error", f"http_{code}" if code else str(response)


def dead_letter(state: dict, batch: dict, why: str) -> str:
    folder = capture_dir() / "dead-letter"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f"{state['document_id']}-{batch['turns'][0]['n']}-{batch['turns'][-1]['n']}.json"
    target = folder / name
    base, _ = hs.api_endpoint()
    target.touch(mode=0o600)
    target.write_text(json.dumps({
        "abandoned_at": _iso(_now()), "reason": why, "attempts": batch["attempt"],
        "bank": state["bank"], "url": _bank_url(base, state["bank"]) + "/memories",
        "request": request_body(state, batch)}, ensure_ascii=False, indent=1))
    return str(target)


def _due(state: dict, batch: dict, now: float) -> str:
    status = batch["status"]
    if status == "retry" and now >= batch.get("next_at", 0):
        return "send"
    if status == "unknown" and now - batch.get("checked_at", 0) >= RECHECK_S / 2:
        return "check"
    if status == "sending" and now - batch.get("claimed_at", 0) >= SENDING_STALE_S:
        return "check"
    if status == "submitted" and now - batch.get("submitted_at", 0) >= CONFIRM_AFTER_S \
            and now - batch.get("checked_at", 0) >= RECHECK_S:
        return "check"
    return ""


def flush(path: Path, *, trigger: str, force: bool = False, blocking: bool = True) -> dict:
    """Advance every batch of one session; returns {"result": ...} for its sends.

    Three phases so no network call ever runs under the session lock (tool and
    prompt hooks of a live session need it within their own short budgets):
    claim work under the lock, talk to the server without it, apply the
    outcomes under the lock again.
    """
    caps = limits()
    base, key = hs.api_endpoint()
    if not base:
        return {"result": "unconfigured"}
    summary: dict[str, Any] = {"result": "none"}
    work: list[tuple[str, dict]] = []
    with locked(path, blocking) as held:
        if not held:
            return {"result": "busy"}
        state = load(path)
        if state is None:
            return summary
        now = _now()
        for batch in state["batches"]:
            action = _due(state, batch, now)
            if action == "send":
                batch["status"], batch["claimed_at"] = "sending", now
            if action:
                work.append((action, batch))
        idle = now - state.get("updated_at", now) >= caps["idle_s"]
        size = len(json.dumps(conversation(state, state["turns"], False), ensure_ascii=False)) if state["turns"] else 0
        moved = bool(state["turns"]) and (force or idle or size >= caps["flush_chars"])
        if moved:
            turns, state["turns"] = state["turns"], []
            last = state["batches"][-1] if state["batches"] else None
            if last is not None and (last["status"] == "retry" or
                                     any(action == "send" and item is last for action, item in work)):
                # Known not delivered (a failed send, or one this pass is about
                # to retry): extend it, so turns stay in order and a failing
                # server gets one request, not one per flush. Its range changes,
                # so its operation id does too.
                last["turns"].extend(turns)
            else:
                batch = {"id": uuid.uuid4().hex[:12], "turns": turns, "header": not state["header_sent"],
                         "attempt": 0, "status": "sending", "created_at": now, "claimed_at": now,
                         "trigger": "idle" if idle and not force else trigger}
                state["header_sent"] = True
                state["batches"].append(batch)
                work.append(("send", batch))
        if work or moved:
            save(path, state)
        snapshot = json.loads(json.dumps(state))
        work = [(action, json.loads(json.dumps(batch))) for action, batch in work]

    outcomes = []
    chosen = bank_strategy(base, key, snapshot["bank"]) if any(a == "send" for a, _ in work) else ""
    for action, batch in work:  # oldest first, so appends keep their order
        if action == "send":
            batch["strategy"] = chosen
        call = send if action == "send" else check
        outcomes.append((action, batch, *call(base, key, snapshot, batch)))

    with locked(path):
        state = load(path)
        if state is None:
            return summary
        now = _now()
        by_id = {batch["id"]: batch for batch in state["batches"]}
        for action, sent, status, detail in outcomes:
            batch = by_id.get(sent["id"])
            if batch is None:
                continue
            op, turns = operation_id(state, batch), [batch["turns"][0]["n"], batch["turns"][-1]["n"]]
            if action == "send":
                batch["strategy"] = sent.get("strategy", "")
                if status == "submitted":
                    batch.update(status="submitted", submitted_at=now, checked_at=0)
                elif status == "unknown":
                    batch.update(status="unknown", checked_at=now)
                else:
                    _fail(state, batch, detail, now, caps)
                summary["result"] = {"submitted": "submitted", "unknown": "unknown"}.get(status, "failed")
                journal(state, {"event": "session_flush", "bank": state["bank"],
                                "document_id": state["document_id"], "operation_id": op, "turns": turns,
                                "attempt": batch["attempt"], "trigger": sent.get("trigger", trigger),
                                "status": status, **({"detail": detail} if status != "submitted" else {})})
            elif status == "completed":
                batch["status"] = "done"
                state["confirmed_turns"] = state.get("confirmed_turns", 0) + len(batch["turns"])
                journal(state, {"event": "session_flush_confirmed", "operation_id": op, "turns": turns})
            elif status in {"failed", "cancelled"}:
                journal(state, {"event": "session_flush_failed", "operation_id": op, "turns": turns,
                                "status": status, "detail": detail})
                _fail(state, batch, f"operation {status}: {detail}", now, caps)
            elif status in {"pending", "processing"}:
                batch.update(status="submitted", checked_at=now, submitted_at=batch.get("submitted_at") or now)
            elif status == "not_found" and batch["status"] == "submitted" and \
                    now - batch.get("submitted_at", now) > 6 * 3600:
                # Accepted long ago and since purged: assume it ran, rather than
                # appending the same turns twice.
                batch["status"] = "done"
                journal(state, {"event": "session_flush_unverifiable", "operation_id": op, "turns": turns})
            elif status == "not_found":
                # Never reached the server: the same operation id is safe to
                # send again, and the server deduplicates it if it did arrive.
                batch.update(status="retry", next_at=now)
            else:  # the server could not be asked; ask again later
                batch["checked_at"] = now
                if now - batch.get("created_at", now) >= caps["max_age_s"]:
                    _abandon(state, batch, f"unconfirmed after {caps['max_age_s']}s: {detail}")
        state["batches"] = [batch for batch in state["batches"] if batch["status"] not in {"done", "abandoned"}]
        while len(state["batches"]) > caps["max_batches"]:
            _abandon(state, state["batches"][0], "too many undelivered batches")
            state["batches"] = state["batches"][1:]
        if not state["turns"] and not state["batches"] and (
                state.get("closed") or now - state.get("updated_at", now) >= 86400):
            remove(path)
        else:
            save(path, state)
    return summary


def _fail(state: dict, batch: dict, detail: str, now: float, caps: dict) -> None:
    """A delivery that definitely did not land: back off, or give up to the dead letters."""
    batch["attempt"] += 1
    batch["last_error"] = detail[:300]
    if batch["attempt"] >= caps["max_attempts"] or now - batch.get("created_at", now) >= caps["max_age_s"]:
        _abandon(state, batch, detail)
        return
    batch.update(status="retry", next_at=now + backoff(batch["attempt"]))


def _abandon(state: dict, batch: dict, why: str) -> None:
    where = dead_letter(state, batch, why)
    batch["status"] = "abandoned"
    state["dead_lettered_turns"] = state.get("dead_lettered_turns", 0) + len(batch["turns"])
    journal(state, {"event": "session_flush_abandoned", "operation_id": operation_id(state, batch),
                    "turns": [batch["turns"][0]["n"], batch["turns"][-1]["n"]], "attempts": batch["attempt"],
                    "reason": why[:300], "dead_letter": where})


def _needs_work(state: dict, now: float, caps: dict) -> bool:
    if any(_due(state, batch, now) for batch in state.get("batches", [])):
        return True
    idle = now - state.get("updated_at", now)
    if state.get("turns") and idle >= caps["idle_s"]:
        return True
    return not state.get("turns") and not state.get("batches") and (state.get("closed") or idle >= 86400)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def sweep(exclude: Path | None = None, budget_s: float | None = None, limit: int = 8) -> dict:
    """Flush idle buffers and advance undelivered batches of OTHER sessions.

    Runs at the end of every captured turn and session end, and from the
    `hindsight-capture-sweep` timer, so a session that died without a
    SessionEnd is still written, and a failed batch is still retried, even
    when that session never fires another hook.
    """
    if budget_s is None:
        try:
            budget_s = float(os.environ.get("HINDSIGHT_CAPTURE_SWEEP_S", "") or 3.0)
        except ValueError:
            budget_s = 3.0
    caps = limits()
    started = time.monotonic()
    report = {"examined": 0, "flushed": 0, "busy": 0}
    folder = capture_dir()
    if not folder.is_dir():
        return report
    now = _now()
    for path in sorted(folder.glob("*.json"), key=_mtime):
        if report["flushed"] >= limit or time.monotonic() - started >= budget_s:
            break
        if exclude is not None and path == exclude:
            continue
        report["examined"] += 1
        state = load(path)
        if state is None or not _needs_work(state, now, caps):
            continue
        summary = flush(path, trigger="sweep", blocking=False)
        report["busy" if summary.get("result") == "busy" else "flushed"] += 1
    # Lock files of sessions that are gone, once nothing can be waiting on them.
    for lock in folder.glob("*.lock"):
        try:
            if not lock.with_suffix(".json").exists() and now - lock.stat().st_mtime > 86400:
                lock.unlink()
        except OSError:
            continue
    return report


def dispatch(concern: str, payload: dict, cli: str, native: str) -> dict:
    role = os.environ.get("BB_HOOK_ROLE", "")
    if concern == "hindsight-turn":
        if role == "prompt_submit":
            return record_ask(payload, cli)
        return record_turn(payload, cli, native)
    if concern == "hindsight-retain":
        return record_edit(payload, cli)
    if concern == "hindsight-session-end":
        return end_session(payload, cli)
    return result("failed", "unknown_capture_concern", exit_code=1)


def main(argv: list[str]) -> int:
    """`session_capture.py sweep` for the timer; `status` prints buffered sessions."""
    command = argv[1] if len(argv) > 1 else "sweep"
    if command == "sweep":
        print(json.dumps(sweep(budget_s=float(os.environ.get("HINDSIGHT_CAPTURE_SWEEP_S", "") or 60), limit=50)))
        return 0
    if command == "status":
        rows = []
        for path in sorted(capture_dir().glob("*.json")):
            state = load(path)
            if state:
                rows.append({"session": path.stem, "bank": state["bank"], "turns_buffered": len(state["turns"]),
                             "batches": [(b["status"], b["attempt"], len(b["turns"])) for b in state["batches"]],
                             "idle_s": round(_now() - state["updated_at"]), "closed": state["closed"],
                             "confirmed_turns": state.get("confirmed_turns", 0)})
        print(json.dumps(rows, indent=1))
        return 0
    print("usage: session_capture.py [sweep|status]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
