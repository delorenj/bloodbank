"""hotreload -- a long-lived daemon that re-execs itself when its code changes.

WHY. A hook runs once and exits, so it always loads the `core/` on disk. A
daemon (bin/asm-tabpaint, bin/asm-orcapaint -- systemd --user services) imports
`core` ONCE and keeps that copy in memory for weeks. When d7ce6aa moved the
ASM's Lua prelude into statekit, the running painters kept the pre-statekit
asm.py: they sent asm.lua WITHOUT the prelude, so every tab-focus `ack` died on
the missing global `sk`. The fix was a hand restart, which is exactly the step
the next core edit would also need and forget.

Every long-lived importer of `core` calls `HotReload().check()` once per loop:

    reloader = HotReload()
    while running:
        reloader.check()          # no-op until a watched file changed
        ...

WHAT IS WATCHED. Every `core/*.py` and `core/*.lua` (the whole package, not just
what happens to be imported today: tomorrow's import must be covered too), plus
the daemon's own script. A snapshot of (path, mtime_ns) is taken at start; any
difference -- edited, added, removed -- is a change. Stat-ing ~20 files once a
second costs nothing against a 0.5 s paint loop.

WHAT HAPPENS ON A CHANGE. The changed `.py` files are compiled first; if one
does not parse (an editor caught mid-save, a half-finished edit) the daemon
keeps running its good in-memory copy, logs why, and tries again on the NEXT
change -- an exec into a SyntaxError would hand the bar to systemd's restart
loop. Otherwise it flushes stderr and `os.execv`s the same interpreter with the
same argv: same PID, so systemd sees nothing, and Python's sockets are
close-on-exec so no Redis connection leaks into the new image. The new process
re-snapshots, so a reload cannot loop on itself.

Stdlib only. Never raises into the daemon's loop.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable, Iterable

CORE_DIR = Path(__file__).resolve().parent
PATTERNS = ("*.py", "*.lua")


def snapshot(core_dir: Path = CORE_DIR, extra: Iterable[str | Path] = ()) -> dict[str, int]:
    """{path: mtime_ns} for every watched file that exists right now."""
    paths = [p for pat in PATTERNS for p in core_dir.glob(pat)]
    paths += [Path(p) for p in extra]
    out: dict[str, int] = {}
    for path in paths:
        try:
            out[str(path)] = path.stat().st_mtime_ns
        except OSError:
            continue          # vanished between glob and stat: the diff catches it
    return out


def _default_argv() -> list[str]:
    # orig_argv keeps interpreter flags (-u, -I); argv alone would drop them.
    orig = getattr(sys, "orig_argv", None)
    if orig:
        return list(orig)
    return [sys.executable, *sys.argv]


def _log(message: str) -> None:
    print(f"hotreload: {message}", file=sys.stderr, flush=True)


class HotReload:
    def __init__(self, *, extra: Iterable[str | Path] | None = None,
                 core_dir: Path = CORE_DIR, min_interval: float = 1.0,
                 argv: list[str] | None = None,
                 execv: Callable[[str, list[str]], object] = os.execv,
                 clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = _log) -> None:
        if extra is None:
            main = sys.modules["__main__"].__dict__.get("__file__")
            extra = [main] if main else []
        self.core_dir = Path(core_dir)
        self.extra = [Path(p).resolve() for p in extra]
        self.min_interval = min_interval
        self.argv = argv
        self._execv = execv
        self._clock = clock
        self._log = log
        self.seen = snapshot(self.core_dir, self.extra)
        self._next = 0.0

    def changed(self) -> list[str]:
        """Paths added, removed or re-stamped since the last accepted snapshot."""
        now = snapshot(self.core_dir, self.extra)
        return sorted(p for p in now.keys() | self.seen.keys() if now.get(p) != self.seen.get(p))

    @staticmethod
    def _broken(paths: list[str]) -> str | None:
        """The first changed .py that does not compile, as 'path: reason'; else None."""
        for path in paths:
            if not path.endswith(".py") or not os.path.exists(path):
                continue
            try:
                compile(Path(path).read_bytes(), path, "exec")
            except (SyntaxError, ValueError, OSError) as exc:
                return f"{path}: {exc}"
        return None

    def check(self) -> bool:
        """Re-exec when watched code changed. Returns True only if exec was
        attempted and returned (tests; a real os.execv does not return)."""
        now = self._clock()
        if now < self._next:
            return False
        self._next = now + self.min_interval
        try:
            changed = self.changed()
            if not changed:
                return False
            why = self._broken(changed)
            if why:
                self.seen = snapshot(self.core_dir, self.extra)   # retry on the NEXT change
                self._log(f"{why}; keeping the running copy")
                return False
            names = ", ".join(Path(p).name for p in changed)
            self._log(f"{names} changed; re-exec")
            argv = self.argv if self.argv is not None else _default_argv()
            self._execv(argv[0], argv)
            return True
        except OSError as exc:       # execv itself failed: stay up on the old code
            self._log(f"re-exec failed: {exc!r}; keeping the running copy")
            return False
