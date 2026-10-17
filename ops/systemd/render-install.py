#!/usr/bin/env python3
"""Render + install (or dry-run) the artifact-sidecar systemd --user units.

The committed *.service files under ops/systemd/ are TEMPLATES with the
development checkout's paths burned in (``%h/code/33GOD/bloodbank`` and
``/usr/bin/python3``). This tool renders them for THIS machine — an
explicit absolute repo root and interpreter — verifies the runtime can
actually import what the units need (nats-py, jsonschema), and writes the
result where systemd --user will find it. Nothing is installed unless you
ask; ``--dry-run`` only prints.

Usage:
  python3 ops/systemd/render-install.py --dry-run                 # print only
  python3 ops/systemd/render-install.py --repo /abs/repo --python /abs/python3
  python3 ops/systemd/render-install.py --install                 # writes units
  python3 ops/systemd/render-install.py --install --user-dir ~/custom/dir

Defaults: repo = this checkout, python = the interpreter running this script
(a venv/conda python works: its absolute path lands in ExecStart, so the
unit never depends on PATH). Existing destination files are NEVER
overwritten without --force; unit enablement/start is left to the operator
per docs/project-artifact-sidecar.md (no activation from this repo).

Exit codes: 0 ok, 1 usage/validation error, 2 refused (would overwrite).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OPS = HERE.parent
REPO_DEFAULT = OPS.parent

UNITS = (
    {
        "service_dir": "artifact-sidecar",
        "source": "artifact-sidecar/artifact-sidecar.service",
        "unit_name": "bloodbank-artifact-sidecar.service",
        "doc_relpath": "docs/project-artifact-sidecar.md",
        "imports": ["nats"],
    },
    {
        "service_dir": "artifact-vault-projector",
        "source": "artifact-vault-projector/artifact-vault-projector.service",
        "unit_name": "bloodbank-artifact-vault-projector.service",
        "doc_relpath": "docs/project-artifact-sidecar.md",
        "imports": ["nats", "jsonschema"],
    },
)

_WORKDIR_RE = re.compile(r"^WorkingDirectory=(.*)$", re.MULTILINE)
_DOC_RE = re.compile(r"^Documentation=(.*)$", re.MULTILINE)
_EXEC_RE = re.compile(r"^ExecStart=(\S+)(.*)$", re.MULTILINE)


class RenderError(RuntimeError):
    pass


def render_unit(
    template_text: str,
    *,
    repo_root: Path,
    python_bin: Path,
) -> str:
    """Rewrite the template's repo-rooted and interpreter paths.

    Rules (fail-loud on anything unexpected):
      - WorkingDirectory / Documentation: replace a path PREFIX that points
        inside the development checkout (…/33GOD/bloodbank or this worktree)
        with the operator-provided absolute repo root, keeping the tail.
      - ExecStart: first token becomes the operator-provided interpreter;
        the main.py argument's checkout prefix is rewritten the same way.
      - No shell quoting tricks: paths are embedded verbatim; the tool
        refuses paths containing whitespace or quotes instead of quoting.
    """
    repo_root = repo_root.resolve(strict=False)
    python_bin = python_bin.resolve(strict=False)
    for label, path in (("repo", repo_root), ("python", python_bin)):
        if not path.is_absolute():
            raise RenderError(f"{label} path must be absolute: {path}")
        text = str(path)
        if any(ch in text for ch in " \t\"'\\\n"):
            raise RenderError(
                f"{label} path contains whitespace/quotes; systemd quoting is "
                f"deliberately not attempted here: {path!r}"
            )

    known_prefixes = [str(p) for p in _checkout_prefixes()]

    def rewrite_path(token: str) -> str:
        # Templates spell the checkout with systemd's %h (home) specifier —
        # plain, or behind a file:// Documentation prefix; match those forms
        # and a literal home path. The operator's absolute repo root is
        # emitted (no %h — unambiguous for review); file:// is preserved.
        prefix = ""
        body = token
        if body.startswith("file://"):
            prefix, body = "file://", body[len("file://"):]
        candidates = [body]
        if body.startswith("%h/"):
            candidates.append(str(Path.home() / body[len("%h/"):]))
        for candidate in candidates:
            for known in known_prefixes:
                if candidate == known or candidate.startswith(known + "/"):
                    tail = candidate[len(known):]
                    return prefix + str(repo_root / tail.lstrip("/"))
        return token

    out = template_text
    out = _WORKDIR_RE.sub(lambda m: "WorkingDirectory=" + rewrite_path(m.group(1)), out)
    out = _DOC_RE.sub(lambda m: "Documentation=" + rewrite_path(m.group(1)), out)

    def _exec(m: re.Match[str]) -> str:
        script_arg = m.group(2).strip()
        script_arg = rewrite_path(script_arg)
        return f"ExecStart={python_bin} {script_arg}"

    out = _EXEC_RE.sub(_exec, out)
    return out


def _checkout_prefixes() -> list[Path]:
    """Absolute prefixes the templates may reference this checkout by.

    The committed template says ``%h/code/33GOD/bloodbank``; a linked
    worktree (this one) says ``%h/code/33GOD/bloodbank-artifact-sidecar``.
    Both are honored so rendering from either checkout works; %h expands to
    the current home directory.
    """
    home = Path.home()
    prefixes = [
        home / "code" / "33GOD" / "bloodbank",
        home / "code" / "33GOD" / "bloodbank-artifact-sidecar",
        REPO_DEFAULT.resolve(strict=False),
    ]
    return [p for p in prefixes if str(p) != "/"]


def verify_interpreter(python_bin: Path, imports: list[str]) -> dict[str, Any]:
    """Check the interpreter exists, is 3.11+, and can import what the units need."""
    info: dict[str, Any] = {"python": str(python_bin)}
    if not (python_bin.exists() and os.access(python_bin, os.X_OK)):
        raise RenderError(f"interpreter not executable: {python_bin}")
    probe = subprocess.run(
        [str(python_bin), "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"],
        capture_output=True, text=True, timeout=30,
    )
    if probe.returncode != 0:
        raise RenderError(f"interpreter probe failed: {probe.stderr.strip()}")
    version = probe.stdout.strip()
    info["version"] = version
    try:
        major, minor = (int(x) for x in version.split(".")[:2])
    except ValueError as exc:
        raise RenderError(f"unparseable version {version!r}") from exc
    if (major, minor) < (3, 11):
        raise RenderError(f"interpreter must be python 3.11+, got {version}")
    for module in imports:
        check = subprocess.run(
            [str(python_bin), "-c", f"import {module}"],
            capture_output=True, text=True, timeout=30,
        )
        info[f"import_{module}"] = check.returncode == 0
        if check.returncode != 0:
            raise RenderError(
                f"{python_bin} cannot import {module} (pip install it into that "
                f"interpreter's environment first): {check.stderr.strip()[:200]}"
            )
    return info


def verify_sources(repo_root: Path) -> None:
    """All template files exist under the checkout, docs under the repo root."""
    for unit in UNITS:
        template = OPS / "systemd" / unit["source"]
        if not template.is_file():
            raise RenderError(f"missing unit template: {template}")
        doc = repo_root / unit["doc_relpath"]
        if not doc.is_file():
            raise RenderError(f"missing doc under repo root: {doc}")
        for env_service in (repo_root / "services" / unit["service_dir"],):
            if not (env_service / "main.py").is_file():
                raise RenderError(f"missing service entrypoint: {env_service / 'main.py'}")


def install_units(rendered: dict[str, str], dest_dir: Path, *, force: bool) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for unit_name, text in rendered.items():
        target = dest_dir / unit_name
        if target.exists() and not force:
            raise RenderError(
                f"refusing to overwrite existing {target} (use --force)"
            )
        # Write temp + rename so a partial write never replaces a unit file.
        fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".unit-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        written.append(target)
    return written


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Render artifact-sidecar systemd --user units")
    parser.add_argument("--repo", default=str(REPO_DEFAULT),
                        help="absolute repo root the units run from (default: this checkout)")
    parser.add_argument("--python", default=sys.executable,
                        help="absolute python interpreter for ExecStart (default: this script's)")
    parser.add_argument("--install", action="store_true",
                        help="write rendered units (default: dry-run, print only)")
    parser.add_argument("--user-dir", default=None,
                        help="destination dir (default: ~/.config/systemd/user)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite existing unit files")
    parser.add_argument("--dry-run", action="store_true",
                        help="print rendered units without writing (default behavior; explicit form)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    repo_root = Path(args.repo).expanduser()
    python_bin = Path(args.python).expanduser()
    try:
        if not repo_root.is_absolute():
            raise RenderError(f"--repo must be absolute: {repo_root}")
        verify_sources(repo_root)
        reports: dict[str, Any] = {"repo": str(repo_root), "python": str(python_bin)}
        reports["checks"] = [
            verify_interpreter(python_bin, unit["imports"]) for unit in UNITS
        ]
        rendered: dict[str, str] = {}
        for unit in UNITS:
            template = (OPS / "systemd" / unit["source"]).read_text()
            rendered[unit["unit_name"]] = render_unit(
                template, repo_root=repo_root, python_bin=python_bin
            )
        reports["rendered"] = list(rendered)

        if args.install:
            dest = Path(args.user_dir).expanduser() if args.user_dir else (
                Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
                / "systemd" / "user"
            )
            if not dest.is_absolute():
                raise RenderError(f"--user-dir must be absolute: {dest}")
            written = install_units(rendered, dest, force=args.force)
            reports["installed"] = [str(p) for p in written]
            reports["next_steps"] = [
                "systemctl --user daemon-reload",
                "review Environment lines, then enable per docs/project-artifact-sidecar.md",
            ]
        else:
            reports["mode"] = "dry-run (nothing written)"
        if args.json:
            print(json.dumps(reports, indent=2))
        else:
            print(f"repo root : {reports['repo']}")
            print(f"python    : {reports['python']}")
            for check in reports["checks"]:
                print(f"verified  : {check['python']} ({check['version']}; "
                      + ", ".join(f"import {m}" for m in ("nats", "jsonschema") if f"import_{m}" in check)
                      + ")")
            for name in rendered:
                print(f"rendered  : {name}"
                      + ("" if args.install else "  (dry-run: not written)"))
            if args.install:
                for path in reports["installed"]:
                    print(f"installed : {path}")
                for step in reports["next_steps"]:
                    print(f"next      : {step}")
            if not args.install:
                print("--- rendered units ---")
                for name, text in rendered.items():
                    print(f"### {name}")
                    print(text)
        return 0
    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
