#!/usr/bin/env python3
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, Any

AGENTS_DIR = Path(os.environ.get("OPENCLAW_AGENTS_DIR", str(Path.home() / ".openclaw/agents")))
API_BASE = os.environ.get("BLOODBANK_API_BASE", "http://127.0.0.1:8682").rstrip("/")
DEFAULT_CHANNEL = os.environ.get("BRIDGE_DEFAULT_CHANNEL", "telegram")
STATE_FILE = Path(os.environ.get("BRIDGE_STATE_FILE", "/tmp/bloodbank-session-bridge-state.json"))
SCAN_INTERVAL = float(os.environ.get("BRIDGE_SCAN_INTERVAL_SEC", "1.0"))

DEFAULT_AGENT_MAP = {
    "eng": "grolf",
    "infra": "lenoon",
    "main": "cack",
    "family": "tonny",
    "work": "rererere",
}


def load_agent_map() -> Dict[str, str]:
    raw = os.environ.get("BRIDGE_AGENT_NAME_MAP_JSON", "").strip()
    if not raw:
        return dict(DEFAULT_AGENT_MAP)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            out = dict(DEFAULT_AGENT_MAP)
            out.update({str(k): str(v) for k, v in parsed.items()})
            return out
    except Exception:
        pass
    return dict(DEFAULT_AGENT_MAP)


AGENT_MAP = load_agent_map()


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {"offsets": {}, "initialized": False}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"offsets": {}, "initialized": False}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def iter_session_files():
    if not AGENTS_DIR.exists():
        return
    for agent_dir in AGENTS_DIR.iterdir():
        if not agent_dir.is_dir():
            continue
        sessions_dir = agent_dir / "sessions"
        if not sessions_dir.exists():
            continue
        for p in sessions_dir.glob("*.jsonl"):
            if p.name == "sessions.jsonl":
                continue
            yield p


def extract_agent_and_session(path: Path):
    # .../agents/{agent_id}/sessions/{session_id}.jsonl
    try:
        agent_id = path.parent.parent.name
        session_id = path.stem
        return agent_id, session_id
    except Exception:
        return "unknown", path.stem


def infer_channel(content: str) -> str:
    if not content:
        return DEFAULT_CHANNEL
    low = content.lower()
    if '"channel": "telegram"' in low or "telegram:" in low:
        return "telegram"
    if '"channel": "signal"' in low or "signal:" in low:
        return "signal"
    if '"channel": "discord"' in low:
        return "discord"
    return DEFAULT_CHANNEL


def preview_text(content: str, max_len: int = 200) -> str:
    txt = re.sub(r"\s+", " ", content or "").strip()
    if len(txt) <= max_len:
        return txt
    return txt[: max_len - 1] + "…"


def post_event(agent_name: str, action: str, payload: Dict[str, Any]) -> None:
    url = f"{API_BASE}/events/agent/{agent_name}/{action}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}")


def process_line(path: Path, line: str) -> None:
    if not line.strip():
        return
    try:
        obj = json.loads(line)
    except Exception:
        return

    if obj.get("type") != "message":
        return

    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    role = msg.get("role")
    content = msg.get("content") if isinstance(msg.get("content"), str) else ""

    if role not in {"user", "assistant"}:
        return

    agent_id, session_id = extract_agent_and_session(path)
    agent_name = AGENT_MAP.get(agent_id, agent_id)
    channel = infer_channel(content)

    if role == "user":
        payload = {
            "agent_name": agent_name,
            "channel": channel,
            "sender": "user",
            "message_preview": preview_text(content),
            "message_length": len(content),
            "session_key": f"agent:{agent_name}:{session_id}",
            "message_id": obj.get("uuid"),
        }
        action = "message.received"
    else:
        payload = {
            "agent_name": agent_name,
            "channel": channel,
            "message_preview": preview_text(content),
            "message_length": len(content),
            "model": None,
            "tokens_used": None,
            "duration_ms": None,
            "message_id": obj.get("uuid"),
        }
        action = "message.sent"

    post_event(agent_name, action, payload)


def run() -> int:
    state = load_state()
    offsets: Dict[str, int] = state.get("offsets", {})

    print(f"[bridge] watching {AGENTS_DIR} -> {API_BASE}")
    print(f"[bridge] default channel: {DEFAULT_CHANNEL}")

    while True:
        changed = False

        for path in iter_session_files() or []:
            key = str(path)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue

            if key not in offsets:
                # first sight: start at EOF to avoid replay storms
                offsets[key] = size
                changed = True
                continue

            offset = int(offsets.get(key, 0))
            if offset > size:
                offset = 0

            if size <= offset:
                continue

            try:
                with path.open("r", encoding="utf-8") as f:
                    f.seek(offset)
                    for raw in f:
                        try:
                            process_line(path, raw)
                        except Exception as e:
                            print(f"[bridge] line error {path.name}: {e}", file=sys.stderr)
                    offsets[key] = f.tell()
                    changed = True
            except Exception as e:
                print(f"[bridge] file read error {path}: {e}", file=sys.stderr)

        if changed:
            state["offsets"] = offsets
            save_state(state)

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        raise SystemExit(0)
