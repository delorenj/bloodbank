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
sys.path.insert(0, str(SERVICE))
import tool_guards


class ToolGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.uv = self.root / "uv/tools"
        self.binary = self.uv / "code-review-graph/bin/code-review-graph"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))\n"
            "print('upstream stderr', file=sys.stderr)\n"
            "raise SystemExit(int(os.environ.get('GUARD_TEST_EXIT', '0')))\n"
        )
        self.binary.chmod(0o700)
        self.registry = self.root / "handlers.toml"
        self.registry.write_text('[[handler]]\nid = "code-review-graph-update"\nenabled = false\n')
        self.manifest = self.root / "ownership.json"
        self.manifest.write_text(json.dumps({
            "version": 1, "registry": str(self.registry), "clis": ["claude", "codex", "opencode"],
            "handler_ids": ["code-review-graph-update"],
        }))
        self.environment = os.environ | {
            "UV_TOOL_DIR": str(self.uv), "BB_HOOK_OWNERSHIP": str(self.manifest),
        }
        self.environment.pop("BB_HOOK_HUB", None)

    def tearDown(self):
        self.temporary.cleanup()

    def run_guard(self, arguments, **environment):
        result = subprocess.run(
            [str(tool_guards.GUARD), *arguments], input="native stdin\n", text=True,
            capture_output=True, env=self.environment | environment,
        )
        return result, json.loads(result.stdout)

    def test_owned_install_and_alias_preserve_arguments_streams_and_exit(self):
        for command in ("install", "init"):
            with self.subTest(command=command):
                argv = [command, "--repo", "/a path/with spaces", "--platform", "codex", "--no-skills"]
                result, output = self.run_guard(argv, GUARD_TEST_EXIT="23")
                self.assertEqual(output["argv"], [*argv, "--no-hooks"])
                self.assertEqual(output["stdin"], "native stdin\n")
                self.assertEqual(result.stderr, "upstream stderr\n")
                self.assertEqual(result.returncode, 23)

    def test_existing_flag_and_non_installer_subcommands_are_unchanged(self):
        for argv in (["install", "--no-hooks", "--yes"], ["update", "--full"],
                     ["status"], ["serve"], ["--version"], []):
            with self.subTest(argv=argv):
                _, output = self.run_guard(argv)
                self.assertEqual(output["argv"], argv)

    def test_no_owner_and_hub_children_keep_standalone_behavior(self):
        _, output = self.run_guard(["install"], BB_HOOK_HUB="off")
        self.assertEqual(output["argv"], ["install"])
        self.manifest.unlink()
        _, output = self.run_guard(["install"])
        self.assertEqual(output["argv"], ["install"])

    def test_install_is_idempotent_and_repairs_a_replaced_uv_link(self):
        launcher = self.root / ".local/bin/code-review-graph"
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(self.binary)
        with mock.patch.dict(os.environ, self.environment):
            self.assertEqual(tool_guards.install(self.root)["status"], "linked")
            self.assertEqual(launcher.resolve(), tool_guards.GUARD)
            self.assertEqual(tool_guards.install(self.root)["status"], "current")
            launcher.unlink()
            launcher.symlink_to(self.binary)
            self.assertEqual(tool_guards.install(self.root)["status"], "linked")
        result = subprocess.run([str(launcher), "install"], input="", text=True,
                                capture_output=True, env=self.environment)
        self.assertEqual(json.loads(result.stdout)["argv"], ["install", "--no-hooks"])

    def test_install_leaves_unrelated_command_untouched(self):
        launcher = self.root / ".local/bin/code-review-graph"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("an unrelated local command\n")
        with mock.patch.dict(os.environ, self.environment), self.assertRaises(RuntimeError):
            tool_guards.install(self.root)
        self.assertEqual(launcher.read_text(), "an unrelated local command\n")

    def test_uninstalled_optional_tool_is_not_linked(self):
        self.binary.unlink()
        with mock.patch.dict(os.environ, self.environment):
            self.assertEqual(tool_guards.install(self.root)["status"], "not_installed")
        self.assertFalse((self.root / ".local/bin/code-review-graph").exists())


if __name__ == "__main__":
    unittest.main()
