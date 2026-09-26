"""Jot flush, bank routing and the recall leftovers of 2026-09-26.

Pins plan item 2b of DeLoContainers stacks/ai/hindsight/docs/2026-09-26-leverage-audit.md
plus two routing defects found beside it:

  * jot: a dead normalizer key exited 0 and the hub recorded `skipped`, so 46
    jots sat unflushed for seven weeks. Every failure must now be a non-zero
    exit, a `failed` receipt and an alert, and the jotfile must survive it.
  * routing: Antigravity runs its hooks from ~/.gemini/config, so bb-hook's
    $PWD sent every one of its sessions to the `general` bank. The payload's
    own working directory now wins, and `general` is a journaled last resort.
  * recall: a query with no word characters is a guaranteed 422; the default
    deadline is 4s; token budgets are 1,400 (primary) and 800 (opt-in).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hindsight  # noqa: E402
import jot_flush  # noqa: E402

HUB_DIR = Path(__file__).resolve().parents[1]
CLIENT = HUB_DIR / "client" / "bb-hook"


class FakeServers:
    """One HTTP server playing both OpenRouter (/chat) and Hindsight (/v1/...)."""

    def __init__(self):
        self.chat_replies: list[tuple[int, dict]] = []   # consumed per chat call
        self.retain_codes: dict[str, int] = {}            # bank -> HTTP code
        self.existing_banks: set[str] = set()
        self.requests: list[tuple[str, str, dict | None]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code: int, body: dict):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, body))
                if self.path == "/chat":
                    code, reply = owner.chat_replies.pop(0) if owner.chat_replies else (200, owner.echo(body))
                    self._reply(code, reply)
                    return
                bank = self.path.split("/banks/", 1)[1].split("/", 1)[0]
                code = owner.retain_codes.get(bank, 200)
                self._reply(code, {"success": True, "items_count": len(body["items"])} if code == 200
                            else {"detail": "Budget limit exceeded"})

            def do_GET(self):
                owner.requests.append(("GET", self.path, None))
                bank = self.path.split("/banks/", 1)[1].split("/", 1)[0]
                if bank in owner.existing_banks:
                    self._reply(200, {"bank_id": bank, "config": {}})
                else:
                    self._reply(404, {"detail": "Bank not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    @staticmethod
    def echo(body: dict) -> dict:
        """A well-behaved normalizer: one question-framed MEM line per jot."""
        listing = body["messages"][1]["content"].split("JOTS:\n", 1)[1].split("\n\n", 1)[0]
        lines = []
        for row in listing.splitlines():
            number, _, text = row.partition(". ")
            if number.isdigit():
                lines.append(f"MEM|{number}|debugging|Why? {text}")
        return {"model": "deepseek/deepseek-v4-flash-0731", "usage": {"cost": 0.0001},
                "choices": [{"message": {"content": "\n".join(lines)}}]}

    def retains(self) -> dict[str, list[dict]]:
        found: dict[str, list[dict]] = {}
        for method, path, body in self.requests:
            if method == "POST" and path.endswith("/memories"):
                found.setdefault(path.split("/banks/", 1)[1].split("/", 1)[0], []).extend(body["items"])
        return found

    def chats(self) -> list[dict]:
        return [body for method, path, body in self.requests if method == "POST" and path == "/chat"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class JotTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.api = FakeServers()
        self.addCleanup(self.api.close)
        self.alerts = self.root / "alerts.log"
        alert = self.root / "ntfy-alert"
        alert.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {self.alerts}\n")
        alert.chmod(0o700)
        self.jots = self.root / "journal/jots"
        self.jots.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {
            "HS_JOURNAL_DIR": str(self.root / "journal"),
            "JOTFLUSH_STATE_DIR": str(self.root / "state"),
            "JOTFLUSH_OPENROUTER_API_KEY": "sk-or-test",
            "JOTFLUSH_OPENROUTER_URL": self.api.url + "/chat",
            "JOTFLUSH_ALERT_COMMAND": str(alert),
            "HINDSIGHT_API_URL": self.api.url, "HINDSIGHT_API_KEY": "hs-test",
            "HINDSIGHT_BANK_CACHE": str(self.root / "bank-cache.json"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        # The header cwd's bank, as the hub would resolve it.
        banks = {"/code/sidepiece": "sidepiece", "/code/pjangler": "pjangler"}
        self.resolved: list[str] = []

        def fake_origin(origin, fallback_cwd=None):
            self.resolved.append(origin)
            return banks.get(origin, "general")

        mock.patch.object(jot_flush, "origin_bank", side_effect=fake_origin).start()
        self.addCleanup(mock.patch.stopall)

    def jotfile(self, name: str, origin: str | None, *lines: str) -> Path:
        path = self.jots / f"{name}.md"
        header = [f"<!-- jots for {origin} -->"] if origin else []
        path.write_text("\n".join([*header, *(f"- {line}" for line in lines)]) + "\n")
        return path

    def log(self) -> list[dict]:
        path = self.root / "journal/jot-flush.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class ParseTests(JotTestCase):
    def test_header_bank_prefix_and_continuation_lines(self):
        path = self.jotfile("a", "/code/audio", "[bank:vexa] scaled to one worker", "plain jot")
        path.write_text(path.read_text() + "  continued on the next line\n")
        parsed = jot_flush.parse(path)
        self.assertEqual(parsed.origin, "/code/audio")
        first, second = parsed.jots
        self.assertEqual((first.bank, first.routed, first.text), ("vexa", True, "scaled to one worker"))
        self.assertEqual(second.text, "plain jot\n  continued on the next line")
        self.assertFalse(second.routed)

    def test_document_id_is_stable_per_jot_text(self):
        one = jot_flush.Jot(n=1, text="GNOME   froze\non Xwayland", raw="")
        two = jot_flush.Jot(n=7, text="GNOME froze on Xwayland", raw="", bank="infra")
        self.assertEqual(one.doc_id, two.doc_id, "whitespace and position must not change the id")
        self.assertRegex(one.doc_id, r"^jot-[0-9a-f]{16}$")
        self.assertNotEqual(one.doc_id, jot_flush.Jot(n=1, text="another insight", raw="").doc_id)


class FlushTests(JotTestCase):
    def test_routes_per_file_and_per_line_and_archives(self):
        path = self.jotfile("sp", "/code/sidepiece", "Bridge runs on 8789", "[bank:vexa] one GPU worker")
        outcome = jot_flush.flush(path, agent="claude")
        self.assertEqual(outcome.status, "flushed")
        retained = self.api.retains()
        self.assertEqual(sorted(retained), ["sidepiece", "vexa"])
        [item] = retained["sidepiece"]
        self.assertEqual(item["content"], "Why? Bridge runs on 8789")
        self.assertEqual(item["document_id"], jot_flush.Jot(n=1, text="Bridge runs on 8789", raw="").doc_id)
        self.assertEqual(item["observation_scopes"], "shared")
        self.assertIn("source:jot", item["tags"])
        self.assertIn("agent:claude", item["tags"])
        self.assertEqual(item["metadata"]["jot"], "Bridge runs on 8789")
        self.assertEqual(item["metadata"]["origin_cwd"], "/code/sidepiece")
        self.assertEqual(retained["vexa"][0]["metadata"]["jot"], "one GPU worker", "the [bank:X] prefix is routing, not content")
        self.assertFalse(path.exists())
        self.assertEqual(len(list((self.jots / "flushed").iterdir())), 1)
        self.assertEqual(self.log()[-1]["status"], "flushed")
        self.assertEqual(self.resolved, ["/code/sidepiece"])
        # The normalizer runs on the retain model family, reasoning off.
        [chat] = self.api.chats()
        self.assertEqual(chat["model"], "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(chat["reasoning"], {"enabled": False})

    def test_reflushing_the_same_jot_reuses_its_document_id(self):
        first = self.jotfile("x", "/code/pjangler", "mise hooks run as separate sh -o errexit")
        jot_flush.flush(first)
        second = self.jotfile("x", "/code/pjangler", "mise hooks run as separate sh -o errexit")
        jot_flush.flush(second)
        ids = [item["document_id"] for item in self.api.retains()["pjangler"]]
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])

    def test_dead_key_fails_loudly_and_keeps_the_jots(self):
        self.api.chat_replies.append((402, {"error": {"message": "Insufficient Balance", "code": 402}}))
        path = self.jotfile("dead", "/code/sidepiece", "a jot that must not be lost")
        with mock.patch.object(sys, "stdout"), mock.patch.object(sys, "stderr"):
            code = jot_flush.main(["jot_flush.py", "--jotfile", str(path)])
        self.assertEqual(code, 1, "a failed flush must exit non-zero")
        self.assertTrue(path.exists(), "the jotfile is the only copy; it must survive a failure")
        self.assertEqual(self.api.retains(), {})
        [entry] = self.log()
        self.assertEqual((entry["status"], entry["reason"]), ("failed", "normalizer_http_402"))
        self.assertIn("Insufficient Balance", entry["detail"])
        alerts = self.alerts.read_text()
        self.assertIn("jot-flush failed: normalizer_http_402", alerts)
        self.assertIn("--priority high", alerts)

    def test_alerts_are_rate_limited_per_reason(self):
        for _ in range(3):
            self.api.chat_replies.append((403, {"error": {"message": "Key limit exceeded (daily limit)"}}))
            path = self.jotfile("dead", "/code/sidepiece", "still here")
            outcome = jot_flush.flush(path)
            jot_flush.fail_loudly(outcome, path)
        self.assertEqual(len(self.alerts.read_text().splitlines()), 1)

    def test_a_failed_bank_keeps_the_whole_file_for_an_idempotent_retry(self):
        self.api.retain_codes["vexa"] = 500
        path = self.jotfile("mixed", "/code/sidepiece", "lands fine", "[bank:vexa] this bank is down")
        outcome = jot_flush.flush(path)
        self.assertEqual((outcome.status, outcome.reason), ("failed", "retain_http_500"))
        self.assertTrue(path.exists())
        del self.api.retain_codes["vexa"]
        self.assertEqual(jot_flush.flush(path).status, "flushed")
        ids = [item["document_id"] for item in self.api.retains()["sidepiece"]]
        self.assertEqual(ids[0], ids[1], "the retry replaces the landed jot's document, it does not duplicate it")

    def test_skip_and_missing_lines(self):
        self.api.chat_replies.append((200, {"choices": [{"message": {"content":
            "MEM|1|conventions|What is it? First.\nSKIP|2|repeats jot 1\n"}}]}))
        path = self.jotfile("s", "/code/sidepiece", "first", "first again", "third, which the model forgot")
        outcome = jot_flush.flush(path)
        self.assertEqual(outcome.status, "flushed")
        items = self.api.retains()["sidepiece"]
        self.assertEqual([item["content"] for item in items], ["What is it? First.", "third, which the model forgot"])
        self.assertEqual([item["metadata"]["normalized"] for item in items], ["true", "false"])
        self.assertEqual(outcome.report["skipped"], [{"n": 2, "why": "repeats jot 1"}])

    def test_a_reply_with_no_usable_lines_is_a_failure(self):
        self.api.chat_replies.append((200, {"choices": [{"message": {"content": "Sure! Here are your memories:"}}]}))
        path = self.jotfile("e", "/code/sidepiece", "anything")
        self.assertEqual(jot_flush.flush(path).reason, "normalizer_empty")
        self.assertTrue(path.exists())

    def test_the_budget_turns_a_slow_leg_into_a_named_failure(self):
        path = self.jotfile("slow", "/code/sidepiece", "anything")
        outcome = jot_flush.flush(path, budget_s=1.0)
        self.assertEqual((outcome.status, outcome.reason), ("failed", "deadline_exceeded"))
        self.assertTrue(path.exists())


class HubDispatchTests(JotTestCase):
    def test_failure_is_a_failed_receipt_not_a_skip(self):
        self.api.chat_replies.append((402, {"error": {"message": "Insufficient Balance"}}))
        cwd = "/code/sidepiece"
        jot_flush.jotfile_for(cwd).write_text(f"<!-- jots for {cwd} -->\n- keep me\n")
        with mock.patch.dict(os.environ, {"HINDSIGHT_AGENT": ""}):
            output = hindsight.dispatch("hindsight-jot-flush", {"cwd": cwd, "session_id": "s"}, "claude", "Stop")
        self.assertEqual(output["_hook_hub"]["status"], "failed")
        self.assertEqual(output["_hook_hub"]["reason"], "jot_normalizer_http_402")
        self.assertEqual(output["_hook_hub"]["exit_code"], 1)
        self.assertTrue(jot_flush.jotfile_for(cwd).exists())
        self.assertTrue(self.alerts.exists())

    def test_success_and_nothing_pending(self):
        cwd = "/code/pjangler"
        self.assertEqual(hindsight.dispatch("hindsight-jot-flush", {"cwd": cwd}, "codex", "Stop")["_hook_hub"]["reason"],
                         "no_pending_jots")
        jot_flush.jotfile_for(cwd).write_text(f"<!-- jots for {cwd} -->\n- worth keeping\n")
        output = hindsight.dispatch("hindsight-jot-flush", {"cwd": cwd}, "codex", "Stop")
        self.assertEqual((output["_hook_hub"]["status"], output["_hook_hub"]["reason"]), ("succeeded", "jots_flushed"))
        self.assertIn("agent:codex", self.api.retains()["pjangler"][0]["tags"])


class SweepTests(JotTestCase):
    def test_idle_files_flush_fresh_ones_wait_and_failures_back_off(self):
        old = self.jotfile("old", "/code/sidepiece", "idle for a day")
        stale = time.time() - 86400
        os.utime(old, (stale, stale))
        fresh = self.jotfile("fresh", "/code/pjangler", "written a minute ago")
        report = jot_flush.sweep(idle_s=7200)
        self.assertEqual((report["flushed"], report["waiting"]), (1, 1))
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

        os.utime(fresh, (stale, stale))
        self.api.chat_replies.append((403, {"error": {"message": "Key limit exceeded"}}))
        self.assertEqual(jot_flush.sweep(idle_s=7200)["failed"], 1)
        self.assertEqual(jot_flush.sweep(idle_s=7200)["waiting"], 1, "a failed file waits out its backoff")
        self.assertEqual(jot_flush.sweep(everything=True)["flushed"], 1)


class OriginBankTests(unittest.TestCase):
    def test_a_removed_worktree_resolves_through_its_parent(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "pjangler"
            (repo / ".claude/worktrees").mkdir(parents=True)
            with mock.patch.object(hindsight, "bank", return_value="pjangler") as bank:
                self.assertEqual(jot_flush.origin_bank(str(repo / ".claude/worktrees/wf_gone")), "pjangler")
            bank.assert_called_once_with(str(repo / ".claude/worktrees"), "jot_flush")


# ------------------------------------------------------------------ routing

def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class BankResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.api = FakeServers()
        self.addCleanup(self.api.close)
        patcher = mock.patch.dict(os.environ, {
            "HS_JOURNAL_DIR": str(self.root / "journal"),
            "HINDSIGHT_API_URL": self.api.url, "HINDSIGHT_API_KEY": "hs-test",
            "HINDSIGHT_BANK_CACHE": str(self.root / "bank-cache.json"),
            "BB_HOOK_CLI": "antigravity",
            # The temp root may itself sit inside a checkout (~/.claude is one):
            # git must not discover it, and the .hindsight/bank walk stops at $HOME.
            "GIT_CEILING_DIRECTORIES": str(self.root),
            "HOME": str(self.root),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("HINDSIGHT_BANK", None)

    def fallbacks(self) -> list[dict]:
        path = self.root / "journal/bank-fallback.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_the_given_cwd_wins_over_the_process_cwd(self):
        repo = self.root / "checkout-dir"
        repo.mkdir()
        git("init", "-q", cwd=repo)
        git("remote", "add", "origin", "git@github.com:delorenj/sidepiece.git", cwd=repo)
        elsewhere = self.root / "gemini-config"
        elsewhere.mkdir()
        previous = os.getcwd()
        os.chdir(elsewhere)
        try:
            self.assertEqual(hindsight.resolve_bank(str(repo)), ("sidepiece", "remote"))
            self.assertEqual(hindsight.resolve_bank(str(repo / "missing")), ("general", "fallback"),
                             "a cwd that does not exist falls back to the process cwd, never guesses")
        finally:
            os.chdir(previous)

    def test_non_repository_directories(self):
        declared = self.root / "work/client"
        (declared / "deep").mkdir(parents=True)
        (self.root / "work/.hindsight").mkdir()
        (self.root / "work/.hindsight/bank").write_text("# comment\nclient-bank\n")
        self.assertEqual(hindsight.resolve_bank(str(declared / "deep")), ("client-bank", "declared_dir"))

        self.api.existing_banks.add("audio")
        audio = self.root / "audio"
        audio.mkdir()
        self.assertEqual(hindsight.resolve_bank(str(audio)), ("audio", "existing_dir"))

        junk = self.root / "config"
        junk.mkdir()
        self.assertEqual(hindsight.bank(str(junk), "recall"), "general")
        [entry] = self.fallbacks()
        self.assertEqual((entry["cwd"], entry["purpose"], entry["cli"]), (str(junk), "recall", "antigravity"))

    def test_bank_existence_is_cached_both_ways(self):
        self.api.existing_banks.add("audio")
        self.assertTrue(hindsight.bank_exists("audio"))
        self.assertFalse(hindsight.bank_exists("config"))
        before = len(self.api.requests)
        self.assertTrue(hindsight.bank_exists("audio"))
        self.assertFalse(hindsight.bank_exists("config"))
        self.assertEqual(len(self.api.requests), before, "both answers come from the cache")
        self.assertFalse(hindsight.bank_exists("../etc"), "not a bank name, never requested")

    def test_an_unreachable_server_is_not_cached(self):
        with mock.patch.dict(os.environ, {"HINDSIGHT_API_URL": "http://127.0.0.1:9"}):
            self.assertFalse(hindsight.bank_exists("audio"))
        self.api.existing_banks.add("audio")
        self.assertTrue(hindsight.bank_exists("audio"))


class ClientWorkingDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.ns = {"__name__": "notmain"}
        exec(compile(CLIENT.read_text(), str(CLIENT), "exec"), self.ns)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_payload_directory_wins(self):
        workspace = self.root / "33GOD"
        workspace.mkdir()
        resolve = self.ns["working_directory"]
        self.assertEqual(resolve({"cwd": str(workspace)}), str(workspace))
        self.assertEqual(resolve({"workspacePaths": [str(workspace)], "conversationId": "c"}), str(workspace),
                         "Antigravity names its workspace only in workspacePaths")

    def test_process_directory_when_the_payload_has_none(self):
        resolve = self.ns["working_directory"]
        with mock.patch.dict(os.environ, {"PWD": "/definitely/not/here"}):
            self.assertEqual(resolve({"cwd": str(self.root / "gone")}), os.getcwd(),
                             "a stale $PWD and a missing payload dir both lose to the real cwd")
            self.assertEqual(resolve({}), os.getcwd())
        with mock.patch.dict(os.environ, {"PWD": os.getcwd()}):
            self.assertEqual(resolve("not a dict"), os.getcwd())

    def test_handlers_run_in_the_payload_workspace_end_to_end(self):
        from test_hub import HubHarness
        workspace = self.root / "workspace"
        workspace.mkdir()
        config = self.root / "gemini-config"
        config.mkdir()
        registry = """
[[handler]]
id = "where"
mode = "sync"
on = ["invocation_start"]
command = ["/usr/bin/python3", "-c", "import os, json; print(json.dumps({'injectSteps': [{'ephemeralMessage': os.getcwd()}]}))"]
timeout_ms = 3000
order = 1
"""
        with HubHarness(self.root, registry) as hub:
            env = dict(os.environ, BB_HOOK_SOCKET=hub.sock, PWD=str(config))
            reply = subprocess.run([str(CLIENT), "--cli", "antigravity", "--native", "PreInvocation", "--trailer", "passive"],
                                   input=json.dumps({"workspacePaths": [str(workspace)], "conversationId": "c"}).encode(),
                                   capture_output=True, env=env, cwd=config, timeout=15)
        self.assertEqual(json.loads(reply.stdout)["injectSteps"][0]["ephemeralMessage"], str(workspace))


# ------------------------------------------------------------------ recall

class RecallLeftoverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.calls = self.root / "calls.jsonl"
        fake = self.root / "hindsight"
        fake.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                        "open(os.environ['CALL_LOG'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                        "print(json.dumps({'results': [{'text': 'a fact'}]}))\n")
        fake.chmod(0o700)
        patcher = mock.patch.dict(os.environ, {"HINDSIGHT_BIN": str(fake), "CALL_LOG": str(self.calls),
                                               "HS_JOURNAL_DIR": str(self.root / "journal")})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("HINDSIGHT_RECALL_TIMEOUT", "HINDSIGHT_RECALL_MAX_TOKENS", "HINDSIGHT_RECALL_EXTRA_MAX_TOKENS"):
            os.environ.pop(name, None)
        mock.patch.object(hindsight, "bank", return_value="primary").start()
        mock.patch.object(hindsight, "recall_banks", return_value=["primary", "infra"]).start()
        self.addCleanup(mock.patch.stopall)

    def test_a_query_without_word_characters_is_never_sent(self):
        for prompt in ("→" * 30, "— " * 20 + "?!", "🙂" * 30):
            output = hindsight.recall({"session_id": "s", "prompt": prompt}, "claude", "UserPromptSubmit")
            self.assertEqual(output["_hook_hub"]["reason"], "query_has_no_word_characters")
        self.assertFalse(self.calls.exists())
        output = hindsight.recall({"session_id": "s", "prompt": "é" + "!" * 30}, "claude", "UserPromptSubmit")
        self.assertEqual(output["_hook_hub"]["status"], "succeeded", "any \\w character is enough for the server")

    def test_token_budgets_and_deadline_defaults(self):
        self.assertEqual(hindsight.recall_deadline(), 4.0)
        hindsight.recall({"session_id": "s", "prompt": "how does the hub route antigravity sessions?"},
                         "claude", "UserPromptSubmit")
        budgets = {call[2]: call[call.index("--max-tokens") + 1] for call in
                   (json.loads(line) for line in self.calls.read_text().splitlines())}
        self.assertEqual(budgets, {"primary": "1400", "infra": "800"})
        with mock.patch.dict(os.environ, {"HINDSIGHT_RECALL_MAX_TOKENS": "1200", "HINDSIGHT_RECALL_EXTRA_MAX_TOKENS": "99999"}):
            self.assertEqual((hindsight.recall_max_tokens(True), hindsight.recall_max_tokens(False)), (1200, 4096))

    def test_recall_resolves_the_bank_for_the_payload_cwd(self):
        hindsight.recall({"session_id": "s", "cwd": "/code/sidepiece", "prompt": "where does the bridge listen?"},
                         "antigravity", "PreInvocation")
        hindsight.bank.assert_called_with("/code/sidepiece", "recall")


if __name__ == "__main__":
    unittest.main(verbosity=2)
