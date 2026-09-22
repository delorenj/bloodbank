"""Supervised Hindsight concerns: recall, edit candidates, and retention receipts.

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
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from concerns import context_output, file_edits, invoke, repository, result


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


def bank() -> str:
    root = repository()
    if root:
        # In worktrees the canonical override belongs to the primary checkout.
        try:
            common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                    capture_output=True, text=True, timeout=1)
            if common.returncode == 0:
                root = Path(common.stdout.strip()).parent
            override = root / ".hindsight/bank"
            if override.is_file():
                for line in override.read_text().splitlines():
                    if line.strip() and not line.lstrip().startswith("#"):
                        return line.strip()
        except (OSError, subprocess.SubprocessError):
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
    root = repository()
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
    Bounded and opt-out because it costs one git call per level.

    Walks from the root `repository()` resolved, NOT from the process cwd. The
    two diverge whenever the caller is not standing in the repo the recall is
    about -- under test most obviously, but also for any hook invoked with a
    different working directory than the agent's. `bank()` answers for that
    root, so its ancestors must be that root's.
    """
    if os.environ.get("HINDSIGHT_ANCESTRY", "1") == "0":
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


def recall_banks(cli: str, primary: str) -> list[str]:
    """Every bank this recall should read, in priority order.

    The personal bank leads and is never dropped by the cap: an agent that
    cannot remember what it has done is the defect this ordering exists to
    prevent. Project banks follow -- the repo, then its superprojects -- then
    whatever the row declares, then the global fan-out.
    """
    cap = max(2, min(12, int(os.environ.get("HINDSIGHT_RECALL_MAX_BANKS", "8") or 8)))
    personal, declared = declared_banks(cli)
    ordered = [
        *([personal] if personal else []),
        primary,
        *ancestor_banks(),
        *declared,
        "general",
        *os.environ.get("HINDSIGHT_GLOBAL_BANKS", "infra").split(),
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
    if os.environ.get("HINDSIGHT_FANOUT", "1") == "0":
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


def recall(payload: dict, cli: str, native: str) -> dict:
    prompt = payload.get("prompt", "")
    if len(prompt.strip()) < 24:
        return result("skipped", "prompt_under_min_length")
    command = binary()
    if not command:
        return result("skipped", "hindsight_binary_missing")
    primary = bank()
    personal, _ = declared_banks(cli)
    banks = recall_banks(cli, primary)
    deadline = max(0.2, min(10.0, float(os.environ.get("HINDSIGHT_RECALL_TIMEOUT", "9"))))
    # The agent's own memory is worth as much as the project's, so it gets the
    # same budget. Everything else is context, not identity.
    deep = {primary, personal} - {""}

    def fetch(target: str) -> tuple[str, list[str], str]:
        args = [command, "memory", "recall", target, prompt, "--output", "json",
                "--budget", "mid" if target in deep else "low", "--max-tokens", "2048" if target in deep else "1024"]
        try:
            completed = subprocess.run(args, capture_output=True, text=True, timeout=deadline, env=cli_environment(cli))
            if completed.returncode:
                return target, [], "recall_command_failed"
            decoded = json.loads(completed.stdout)
            return target, recall_text(decoded) if isinstance(decoded, dict) else [], ""
        except subprocess.TimeoutExpired:
            return target, [], "recall_deadline_exceeded"
        except (OSError, ValueError):
            return target, [], "recall_response_invalid"

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(banks)) as pool:
        responses = list(pool.map(fetch, banks))
    context: list[str] = []
    failures = [error for _, _, error in responses if error]
    for target, texts, error in responses:
        if texts:
            context.append(f"<!-- hindsight:recall bank={target} -->\n" + "\n".join(texts)[:10000] + "\n<!-- /hindsight:recall -->")
    rendered = "\n\n".join(context)[:20000]
    if payload.get("session_id"):
        journal(payload, cli, {"event": "recall", "bank": primary, "banks": banks,
                              "prompt_len": len(prompt), "total_chars": len(rendered),
                              "returned_anything": bool(rendered), "failed_banks": len(failures)})
    if not rendered and failures:
        return result("failed", failures[0], exit_code=1)
    return result("succeeded", "recall_partial" if failures else "recall_completed", context_output(rendered, cli, native))


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
