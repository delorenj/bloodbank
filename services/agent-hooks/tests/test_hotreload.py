"""core/hotreload.py: long-lived importers of core re-exec when core changes.

The incident (asm-1): asm-tabpaint / asm-orcapaint kept a pre-statekit asm.py in
memory after d7ce6aa and sent asm.lua without its prelude. The helper makes a
core edit reach a running daemon without a hand restart.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

from core import hotreload                                  # noqa: E402
from core.hotreload import HotReload, snapshot              # noqa: E402
from core.resp import RedisError                            # noqa: E402


def bump(path: Path, seconds: float = 5.0) -> None:
    """Move mtime forward so a same-tick rewrite is still a change."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + int(seconds * 1e9)))


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="hotreload-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.core = self.root / "core"
        self.core.mkdir()
        (self.core / "asm.py").write_text("X = 1\n")
        (self.core / "asm.lua").write_text("return 1\n")
        (self.core / "notes.txt").write_text("not watched\n")
        self.script = self.root / "daemon"
        self.script.write_text("print('daemon')\n")
        self.execs: list[tuple] = []
        self.logs: list[str] = []
        self.now = 100.0

    def reloader(self, **kw) -> HotReload:
        return HotReload(core_dir=self.core, extra=[self.script], argv=["/usr/bin/python3", "-u", str(self.script)],
                         execv=lambda exe, argv: self.execs.append((exe, argv)),
                         clock=lambda: self.now, log=self.logs.append, **kw)

    def tick(self, seconds: float = 2.0) -> None:
        self.now += seconds


class HotReloadTest(Fixture):
    def test_no_change_means_no_exec(self):
        r = self.reloader()
        for _ in range(5):
            self.tick()
            self.assertFalse(r.check())
        self.assertEqual(self.execs, [])

    def test_edited_core_py_execs_with_the_same_argv(self):
        r = self.reloader()
        (self.core / "asm.py").write_text("X = 2\n")
        bump(self.core / "asm.py")
        self.tick()
        r.check()
        self.assertEqual(self.execs, [("/usr/bin/python3", ["/usr/bin/python3", "-u", str(self.script)])])
        self.assertIn("asm.py", self.logs[0])

    def test_edited_core_lua_execs(self):
        r = self.reloader()
        (self.core / "asm.lua").write_text("return 2\n")
        bump(self.core / "asm.lua")
        self.tick()
        r.check()
        self.assertEqual(len(self.execs), 1)

    def test_added_and_removed_files_count(self):
        r = self.reloader()
        (self.core / "new.py").write_text("Y = 1\n")
        self.tick()
        r.check()
        self.assertEqual(len(self.execs), 1)
        r2 = self.reloader()
        (self.core / "asm.lua").unlink()
        self.tick()
        r2.check()
        self.assertEqual(len(self.execs), 2)

    def test_unwatched_file_is_ignored(self):
        r = self.reloader()
        (self.core / "notes.txt").write_text("changed\n")
        bump(self.core / "notes.txt")
        self.tick()
        r.check()
        self.assertEqual(self.execs, [])

    def test_the_daemon_script_itself_is_watched(self):
        r = self.reloader()
        self.script.write_text("print('v2')\n")
        bump(self.script)
        self.tick()
        r.check()
        self.assertEqual(len(self.execs), 1)

    def test_rate_limited_between_checks(self):
        r = self.reloader(min_interval=10.0)
        r.check()
        (self.core / "asm.py").write_text("X = 2\n")
        bump(self.core / "asm.py")
        self.tick(1.0)
        self.assertFalse(r.check())          # inside the window: not even a stat
        self.assertEqual(self.execs, [])
        self.tick(10.0)
        r.check()
        self.assertEqual(len(self.execs), 1)

    def test_a_syntax_error_keeps_the_running_copy_then_a_fix_reloads(self):
        r = self.reloader()
        (self.core / "asm.py").write_text("def broken(:\n")
        bump(self.core / "asm.py")
        self.tick()
        r.check()
        self.assertEqual(self.execs, [])
        self.assertIn("asm.py", self.logs[-1])
        self.tick()
        r.check()                              # same broken file: no log spam, no exec
        self.assertEqual((len(self.execs), len(self.logs)), (0, 1))
        (self.core / "asm.py").write_text("X = 3\n")
        bump(self.core / "asm.py", 10)
        self.tick()
        r.check()
        self.assertEqual(len(self.execs), 1)

    def test_a_failing_execv_does_not_raise_into_the_loop(self):
        def boom(exe, argv):
            raise OSError("exec failed")
        r = HotReload(core_dir=self.core, extra=[self.script], argv=["x"], execv=boom,
                      clock=lambda: self.now, log=self.logs.append)
        (self.core / "asm.py").write_text("X = 2\n")
        bump(self.core / "asm.py")
        self.tick()
        self.assertFalse(r.check())
        self.assertIn("re-exec failed", self.logs[-1])

    def test_snapshot_covers_the_real_core_package(self):
        names = {Path(p).name for p in snapshot(hotreload.CORE_DIR)}
        self.assertTrue({"asm.py", "asm.lua", "statekit.py", "statekit_prelude.lua", "hotreload.py"} <= names)


class RealExecTest(Fixture):
    def test_a_running_daemon_re_execs_itself_in_place_on_a_core_edit(self):
        # The real os.execv, in a real child: same PID, new code, no restart.
        (self.core / "version.py").write_text("VERSION = 'old'\n")
        daemon = self.root / "live-daemon"
        daemon.write_text(textwrap.dedent(f"""
            import sys, time, os
            sys.path.insert(0, {str(self.root)!r})
            import importlib.util
            spec = importlib.util.spec_from_file_location("hr", {str(SERVICE_DIR / "core" / "hotreload.py")!r})
            hr = importlib.util.module_from_spec(spec); spec.loader.exec_module(hr)
            HotReload = hr.HotReload
            from core import version
            reloader = HotReload(core_dir=__import__('pathlib').Path({str(self.core)!r}),
                                 min_interval=0.05)
            print(os.getpid(), version.VERSION, flush=True)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                reloader.check()
                time.sleep(0.05)
            sys.exit(3)
        """))
        (self.core / "__init__.py").write_text("")
        proc = subprocess.Popen([sys.executable, "-u", str(daemon)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.stderr.close)
        self.addCleanup(proc.kill)
        first = proc.stdout.readline().split()
        self.assertEqual(first[1], "old")
        (self.core / "version.py").write_text("VERSION = 'new'\n")
        bump(self.core / "version.py")
        second = proc.stdout.readline().split()
        self.assertEqual(second[1], "new")
        self.assertEqual(second[0], first[0])          # same PID: systemd sees nothing
        proc.kill()
        proc.wait()


def load_painter(name: str, filename: str):
    path = SERVICE_DIR / "bin" / filename
    loader = SourceFileLoader(name, str(path))
    spec = spec_from_loader(name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class DaemonWiringTest(unittest.TestCase):
    """Both systemd daemons must call the helper from their loop."""

    def test_both_daemons_import_and_call_hotreload(self):
        for filename in ("asm-tabpaint", "asm-orcapaint"):
            src = (SERVICE_DIR / "bin" / filename).read_text()
            self.assertIn("from core.hotreload import HotReload", src, filename)
            self.assertIn("reloader = HotReload()", src, filename)
            self.assertIn("reloader.check()", src, filename)
            self.assertLess(src.index("reloader = HotReload()"), src.index("reloader.check()"), filename)


class TabpaintAckLoggingTest(unittest.TestCase):
    """asm-1: a RedisError on the ack write was swallowed; it must reach stderr."""

    def test_ack_redis_error_is_logged_not_swallowed(self):
        tabpaint = load_painter("asm_tabpaint_under_test", "asm-tabpaint")
        painter = tabpaint.Painter()
        agents = {7: ("claude:s:x", "awaiting_human", {"block_kind": "bell"})}
        err = io.StringIO()
        with mock.patch.object(tabpaint.asm, "fire",
                               side_effect=RedisError("user_script:1: attempt to index global 'sk'")), \
                redirect_stderr(err):
            painter.acknowledge(object(), agents, {1: [7]}, 1)
            painter.acknowledge(object(), agents, {1: [7]}, 1)     # rate limited: one line
        lines = [ln for ln in err.getvalue().splitlines() if ln]
        self.assertEqual(len(lines), 1, err.getvalue())
        self.assertIn("ack claude:s:x failed", lines[0])
        self.assertIn("global 'sk'", lines[0])


if __name__ == "__main__":
    unittest.main()
