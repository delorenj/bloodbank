"""native-clock-guard: Candybar is the single clock.

Table-driven. The guard must deny every way an agent could install a native
recurring clock and must never touch read-only or unrelated commands (a false
deny on `crontab -l` or `systemctl list-timers` would break routine diagnosis).

Run: python3 -m pytest services/hook-hub/tests/test_native_clock_guard.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SERVICE = Path(__file__).resolve().parents[1]
GUARD = SERVICE / "native_clock_guard.py"
sys.path.insert(0, str(SERVICE))
import native_clock_guard as g  # noqa: E402

POLICY = {"version": 1, "exceptions": [
    {"id": "repo:deploy/watchdog/candybar-watchdog.timer", "owner": "c", "reason": "AD-22"},
    {"id": "systemd:user:candybar-watchdog.timer", "owner": "c", "reason": "AD-22"},
    {"id": "systemd:user:infra-doctor.timer", "owner": "c", "reason": "exclusion"},
    {"id": "systemd:system:chrony-check.timer", "owner": "c", "reason": "exclusion"},
]}
LEDGER = "entries:\n- id: systemd:newapi-allowances\n  kind: systemd\n- id: crontab:backup\n  kind: crontab\n"


def bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


class GuardCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        repo = self.tmp / "candybar"
        (repo / "deploy/migration").mkdir(parents=True)
        (repo / "deploy/scheduling-policy.json").write_text(json.dumps(POLICY))
        (repo / "deploy/migration/ledger.yaml").write_text(LEDGER)
        self.repo = repo
        self.env = mock.patch.dict(os.environ, {"CANDYBAR_REPO": str(repo)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self._td.cleanup()

    def verdict(self, payload: dict, cli: str = "claude"):
        return g.decide(payload, str(self.tmp), cli)

    def denied(self, payload: dict, cli: str = "claude") -> bool:
        return self.verdict(payload, cli)[0] == 2


class ShellDeny(GuardCase):
    def test_deny_table(self):
        t = self.tmp
        cases = [
            "crontab -e", "crontab f", "crontab", "crontab -",
            "(crontab -l; echo '* * * * * x') | crontab -",
            "sudo crontab -e", "sudo -u bob crontab -e", "bash -c 'crontab -e'",
            "echo hi && crontab -e", "env FOO=1 crontab f", "timeout 5 crontab f",
            "crontab -u delorenj /tmp/f", "$(crontab -e)", "x=$(crontab f)",
            "systemctl --user enable --now x.timer",
            "systemctl --user start x.timer",
            "systemctl --user restart x.timer",
            "systemctl --user link /p/x.timer",
            "systemctl enable foo.service bar.timer",
            "sudo systemctl enable --now /etc/systemd/system/x.timer",
            "systemctl --user unmask x.timer",
            "systemctl --user edit x.timer",
            "systemctl --system restart candybar-watchdog.timer",  # user-scope exemption only
            "systemd-run --user --on-calendar=daily true",
            "systemd-run --on-active=30 --timer-property=AccuracySec=1s x",
            "echo t | at 10:00", "batch", "at -f job.sh now+1min", "at now",
            "hermes -p a cron create '0 9 * * *' x",
            "hermes cron add '30m' 'prompt'",
            "hermes cron create 'x' --scheduler internal",
            "hermes cron edit j --scheduler internal",
            "hermes cron edit j --scheduler=internal",
            "launchctl submit -l x -- /bin/true",
            f"cat > {t}/x.timer <<EOF\n[Timer]\nOnCalendar=daily\nEOF",
            f"echo x >> {t}/new.crontab",
            f"printf x | tee {t}/y.timer",
            f"tee -a {t}/y.timer < /dev/null",
            f"cp /etc/hostname {t}/z.timer",
            f"install -m 644 /etc/hostname {t}/z.timer",
            f"ln -s /etc/hostname {t}/timers.target.wants/z.timer",
            "echo x > /etc/cron.d/job", "sudo tee /etc/cron.daily/job < /dev/null",
            f"bash -lc \"cat > {t}/x.timer\"",
            "{ crontab -e; }",
        ]
        for c in cases:
            with self.subTest(c=c):
                self.assertTrue(self.denied(bash(c)), c)

    def test_allow_table(self):
        t = self.tmp
        (t / "existing.timer").write_text("x")
        cases = [
            "crontab -l", "crontab -u delorenj -l", "crontab -r", "crontab -V",
            "crontab -l | grep foo", "crontab -l > backup.txt",
            "systemctl --user list-timers", "systemctl --user list-timers --all",
            "systemctl --user status x.timer", "systemctl --user cat x.timer",
            "systemctl --user show x.timer", "systemctl --user is-active x.timer",
            "systemctl --user disable --now x.timer", "systemctl --user stop x.timer",
            "systemctl --user mask x.timer", "systemctl --user daemon-reload",
            "systemctl --user restart candybar-watchdog.timer",
            "systemctl --user enable --now candybar-watchdog.timer",
            "systemctl --user restart infra-doctor.timer",
            "systemctl --system restart chrony-check.timer",
            "systemctl --user restart hermes-gateway.service",
            "systemctl --user enable foo.service",
            "systemd-run --user --unit=job true", "systemd-run --scope sleep 1",
            "atq", "atrm 3", "at -l", "at -c 3", "at -d 3",
            "hermes cron create '0 9 * * *' x --scheduler external",
            "hermes cron create x --scheduler=external",
            "hermes cron edit j --scheduler external",
            "hermes cron list", "hermes cron run j", "hermes cron edit j --name x",
            "hermes -p a chat",
            "uv run candybar migrate rollback systemd:newapi-allowances",
            "uv run candybar register .",
            "echo crontab -e", "echo 'systemctl enable x.timer'", "rg '\\.timer'",
            "rg crontab", "git log --format=%at", "grep -r OnCalendar .",
            "cat x.timer", "cat ~/.config/systemd/user/x.timer", "ls *.timer",
            "git commit -m 'drop crontab -e from docs'", "man at", "which batch",
            f"cat > {t}/notes.txt <<EOF\ncrontab -e\nsystemctl enable x.timer\nEOF",
            f"echo x >> {t}/existing.timer",
            f"cp /etc/hostname {t}/other.service",
            f"echo x > {t}/out.log", "echo x > /dev/null", "ls 2>&1 | grep timer",
            "git add foo.timer", "mv a.service b.service",
            "launchctl list", "launchctl load /nonexistent/x.plist",
            "ps aux | grep cron", "journalctl --user -u x.timer",
            "echo $(date) && ls",
        ]
        for c in cases:
            with self.subTest(c=c):
                code, out, err = self.verdict(bash(c))
                self.assertEqual(code, 0, f"{c}\n{err}")

    def test_cwd_relative_new_timer_denied_existing_allowed(self):
        (self.tmp / "have.timer").write_text("x")
        self.assertTrue(self.denied(bash("echo x > fresh.timer")))
        self.assertFalse(self.denied(bash("echo x > have.timer")))

    def test_interval_plist_needs_interval_keys(self):
        d = self.tmp / "Library/LaunchAgents"
        d.mkdir(parents=True)
        self.assertTrue(self.denied(bash(
            f"cat > {d}/a.plist <<EOF\n<key>StartCalendarInterval</key>\nEOF")))
        self.assertFalse(self.denied(bash(
            f"cat > {d}/b.plist <<EOF\n<key>RunAtLoad</key>\nEOF")))
        p = self.tmp / "iv.plist"
        p.write_text("<key>StartInterval</key>")
        self.assertTrue(self.denied(bash(f"launchctl load {p}")))
        self.assertTrue(self.denied(bash(f"cp {p} {d}/c.plist")))

    def test_shell_tool_name_and_argument_variants(self):
        cmd = "crontab -e"
        shapes = [
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            {"toolName": "bash", "toolArgs": json.dumps({"command": cmd})},
            {"toolName": "Bash", "toolInput": {"command": cmd}},
            {"tool_name": "run_shell_command", "tool_input": {"command": cmd}},
            {"tool_name": "terminal", "tool_input": {"command": cmd}},
            {"tool_name": "exec_command", "tool_input": {"cmd": cmd}},
            {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", cmd]}},
            {"tool_name": "shell_command", "tool_input": {"command": ["crontab", "-e"]}},
        ]
        for p in shapes:
            with self.subTest(p=p):
                self.assertTrue(self.denied(p))

    def test_non_matching_tools_and_garbage_pass(self):
        for p in ({}, {"tool_name": "Read", "tool_input": {"file_path": "x.timer"}},
                  {"tool_name": "Bash"}, {"tool_name": "Bash", "tool_input": {"command": ""}},
                  {"tool_name": "Bash", "tool_input": "weird"}, {"tool_name": None}):
            with self.subTest(p=p):
                self.assertEqual(self.verdict(p)[0], 0)


class WriteTools(GuardCase):
    def test_new_timer_denied_edit_of_existing_allowed(self):
        new, old = self.tmp / "x.timer", self.tmp / "old.timer"
        old.write_text("x")
        for tool in ("Write", "write_file", "create", "Edit", "MultiEdit", "replace"):
            with self.subTest(tool=tool):
                self.assertTrue(self.denied({"tool_name": tool, "tool_input": {"file_path": str(new), "content": "x"}}))
                self.assertFalse(self.denied({"tool_name": tool, "tool_input": {"file_path": str(old), "content": "x"}}))
        self.assertTrue(self.denied({"toolName": "write_file", "toolArgs": json.dumps({"path": str(new)})}))

    def test_other_clock_files(self):
        for path in ("/etc/cron.d/job", f"{self.tmp}/jobs.crontab",
                     f"{self.tmp}/.config/systemd/user/timers.target.wants/a.timer"):
            with self.subTest(path=path):
                self.assertTrue(self.denied({"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}}))
        for path in (f"{self.tmp}/a.service", f"{self.tmp}/a.py", f"{self.tmp}/crontab-notes.md"):
            with self.subTest(path=path):
                self.assertFalse(self.denied({"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}}))

    def test_apply_patch(self):
        add = f"*** Begin Patch\n*** Add File: {self.tmp}/x.timer\n+[Timer]\n*** End Patch"
        upd = f"*** Begin Patch\n*** Update File: {self.tmp}/a.py\n+x\n*** End Patch"
        self.assertTrue(self.denied({"tool_name": "apply_patch", "tool_input": {"command": add}}))
        self.assertTrue(self.denied({"tool_name": "apply_patch", "tool_input": {"patch": add}}))
        self.assertTrue(self.denied({"tool_name": "apply_patch", "tool_input": add}))
        self.assertFalse(self.denied({"tool_name": "apply_patch", "tool_input": {"command": upd}}))

    def test_exempt_unit_and_candybar_repo_entry_may_be_created(self):
        self.assertFalse(self.denied({"tool_name": "Write", "tool_input": {
            "file_path": str(self.repo / "deploy/watchdog/candybar-watchdog.timer"), "content": "x"}}))
        self.assertFalse(self.denied({"tool_name": "Write", "tool_input": {
            "file_path": str(self.tmp / "u/candybar-watchdog.timer"), "content": "x"}}))
        self.assertTrue(self.denied({"tool_name": "Write", "tool_input": {
            "file_path": str(self.repo / "deploy/other.timer"), "content": "x"}}))

    def test_plist_interval_content(self):
        p = f"{self.tmp}/Library/LaunchAgents/a.plist"
        self.assertTrue(self.denied({"tool_name": "Write", "tool_input": {"file_path": p, "content": "<key>StartInterval</key>"}}))
        self.assertFalse(self.denied({"tool_name": "Write", "tool_input": {"file_path": p, "content": "<key>RunAtLoad</key>"}}))


class CronjobTool(GuardCase):
    def call(self, **inp):
        return {"tool_name": "cronjob_manage", "tool_input": inp}

    def test_recurring_create_denied(self):
        for sched in ("every 2h", "30m", "0 9 * * *", "every monday 9am", "weekdays at 9am", None, ""):
            with self.subTest(sched=sched):
                self.assertTrue(self.denied(self.call(action="create", schedule=sched, prompt="x")))

    def test_one_shot_create_allowed(self):
        for sched in ("in 30m", "in 2h", "IN 5m", "2026-11-01T09:00:00", "2026-11-01", "2026-11-01 09:00",
                      "2026-11-01T09:00:00+00:00", "2026-11-01T09:00:00Z"):
            with self.subTest(sched=sched):
                self.assertFalse(self.denied(self.call(action="create", schedule=sched, prompt="x")))

    def test_update_only_denied_with_recurring_schedule(self):
        self.assertTrue(self.denied(self.call(action="update", job_id="j", schedule="every 1h")))
        self.assertFalse(self.denied(self.call(action="update", job_id="j", schedule="in 10m")))
        self.assertFalse(self.denied(self.call(action="update", job_id="j", name="renamed")))

    def test_other_actions_allowed(self):
        for action in ("list", "pause", "resume", "remove", "run"):
            with self.subTest(action=action):
                self.assertFalse(self.denied(self.call(action=action, job_id="j")))


class EscapeHatch(GuardCase):
    def test_valid_hatch_allows_and_is_auditable(self):
        code, out, err = self.verdict(bash("CANDYBAR_NATIVE_CLOCK=rollback:crontab:backup crontab /tmp/f"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["_hook_hub"]["reason"], "native_clock_escape_hatch")
        self.assertEqual(self.verdict(bash(
            "CANDYBAR_NATIVE_CLOCK=rollback:systemd:newapi-allowances systemctl --user enable x.timer"))[0], 0)

    def test_unknown_or_missing_ledger_id_denied_with_reason(self):
        code, out, err = self.verdict(bash("CANDYBAR_NATIVE_CLOCK=rollback:nope crontab f"))
        self.assertEqual(code, 2)
        self.assertIn("Escape hatch rejected", err)
        (self.repo / "deploy/migration/ledger.yaml").unlink()
        self.assertEqual(self.verdict(bash("CANDYBAR_NATIVE_CLOCK=rollback:crontab:backup crontab f"))[0], 2)

    def test_hatch_must_be_a_leading_prefix(self):
        self.assertTrue(self.denied(bash("echo CANDYBAR_NATIVE_CLOCK=rollback:crontab:backup; crontab f")))
        self.assertTrue(self.denied(bash("crontab f # CANDYBAR_NATIVE_CLOCK=rollback:crontab:backup")))

    def test_hatch_does_not_matter_when_nothing_is_denied(self):
        self.assertEqual(self.verdict(bash("CANDYBAR_NATIVE_CLOCK=rollback:nope crontab -l"))[0], 0)


class PolicyDegradation(GuardCase):
    def test_unreadable_policy_still_exempts_watchdog_only(self):
        (self.repo / "deploy/scheduling-policy.json").write_text("{not json")
        self.assertFalse(self.denied(bash("systemctl --user restart candybar-watchdog.timer")))
        self.assertTrue(self.denied(bash("systemctl --user restart infra-doctor.timer")))
        self.assertTrue(self.denied(bash("systemctl --user enable new.timer")))

    def test_missing_candybar_checkout_still_denies(self):
        with mock.patch.dict(os.environ, {"CANDYBAR_REPO": str(self.tmp / "absent")}):
            self.assertTrue(self.denied(bash("crontab -e")))
            self.assertFalse(self.denied(bash("systemctl --user restart candybar-watchdog.timer")))


class OutputShapes(GuardCase):
    def run_guard(self, payload: dict, cli: str, cwd: Path | None = None):
        env = dict(os.environ, BB_HOOK_CLI=cli, CANDYBAR_REPO=str(self.repo))
        return subprocess.run(["/usr/bin/python3", str(GUARD)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, cwd=cwd or self.tmp, timeout=10)

    def test_deny_exits_2_with_stderr_for_exit_code_clis(self):
        for cli in ("claude", "codex", "kimi", "copilot", "hermes"):
            with self.subTest(cli=cli):
                r = self.run_guard(bash("crontab -e"), cli)
                self.assertEqual(r.returncode, 2)
                self.assertIn("BLOCKED by native-clock-guard", r.stderr)
                self.assertIn("candybar` skill", r.stderr)
                wrapper = json.loads(r.stdout)
                self.assertEqual(wrapper["_hook_hub"]["exit_code"], 2)
                self.assertEqual(wrapper["stdout"], "")

    def test_gemini_gets_decision_json_on_stdout(self):
        r = self.run_guard(bash("crontab -e"), "gemini")
        inner = json.loads(json.loads(r.stdout)["stdout"])
        self.assertEqual(inner["decision"], "deny")
        self.assertIn("BLOCKED by native-clock-guard", inner["reason"])

    def test_opencode_gets_permission_decision_json_on_stdout(self):
        r = self.run_guard(bash("crontab -e"), "opencode")
        inner = json.loads(json.loads(r.stdout)["stdout"])
        self.assertEqual(inner["permissionDecision"], "deny")
        self.assertIn("native-clock-guard", inner["permissionDecisionReason"])

    def test_allow_exits_0_with_skipped_receipt(self):
        r = self.run_guard(bash("crontab -l"), "claude")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")
        self.assertEqual(json.loads(r.stdout), {"_hook_hub": {"status": "skipped", "reason": "no_native_clock"}, "stdout": ""})

    def test_fail_open_on_bad_stdin_and_internal_errors(self):
        for blob in ("", "not json", "[]", "null"):
            with self.subTest(blob=blob):
                r = subprocess.run(["/usr/bin/python3", str(GUARD)], input=blob, capture_output=True,
                                   text=True, timeout=10)
                self.assertEqual(r.returncode, 0)
        with mock.patch.object(g, "evaluate", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stdin.read", return_value=json.dumps(bash("crontab -e"))):
            self.assertEqual(g.main(), 0)


if __name__ == "__main__":
    unittest.main()
