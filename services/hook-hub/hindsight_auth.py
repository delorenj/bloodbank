#!/usr/bin/env python3
"""The one place a Hindsight client on this machine gets its API URL and key.

Every non-interactive client resolves credentials here: the hook-hub handlers
(recall, briefing, session capture, jot flush), the write probe, the server-side
maintenance sweeps, and any script that shells out to the `hindsight` CLI. Before
this existed each of them re-read `~/.hindsight/config` on its own, so when that
file lost its `api_key` (2026-11-26 00:53, deliberately: a plaintext key does not
belong in a file) every timer and hook went 401 at once while interactive shells,
which export HINDSIGHT_API_KEY, kept working and hid it.

Resolution, first hit wins (URL and key resolve independently):

  URL  1. $HINDSIGHT_API_URL
       2. `api_url` in ~/.hindsight/config ($HINDSIGHT_CONFIG overrides the path)
       3. https://hs-api.delo.sh, the canonical route. The nested legacy name
          api.hs.delo.sh only answers on the LAN: Cloudflare's Universal SSL
          covers one subdomain level, so it fails TLS on the public edge.
  KEY  1. $HINDSIGHT_API_KEY
       2. `api_key` in the config (an op:// value there is read from the vault;
          a literal still works, and `check` flags it as plaintext)
       3. $HINDSIGHT_API_KEY_REF (set but empty switches the vault off)
       4. `api_key_ref` in the config
       5. DEFAULT_KEY_REF, the DeLoSecrets "Hindsight" item

The config holds an op:// reference or no key at all, never a literal. The
upstream CLI ignores `api_key_ref`, and `hindsight configure` rewrites the file
with only the keys it knows, dropping it. That is why the vault default is built
in here: a wiped config must not take the clients down. To change the config
without losing keys use `hindsight-auth configure`.

CLI (`~/.local/bin/hindsight-auth` links here):

  hindsight-auth check [--json]        resolve, then make one authenticated call
  hindsight-auth exec -- CMD [ARG...]  run CMD with HINDSIGHT_API_URL/KEY set
  hindsight-auth env                   KEY=VALUE lines for eval/EnvironmentFile
  hindsight-auth url | key             one value (key refuses a terminal)
  hindsight-auth configure [--api-url URL] [--api-key-ref REF] [--drop-plaintext]

Stdlib only; runs under /usr/bin/python3 from a timer with an empty env.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_URL = "https://hs-api.delo.sh"
# DeLoSecrets item "Hindsight" (by UUID: the vault has duplicate titles).
DEFAULT_KEY_REF = "op://DeLoSecrets/vgfndnrcupczmjs55nnyjmq2km/credential"
LEGACY_HOSTS = {"api.hs.delo.sh": "hs-api.delo.sh"}

_VAULT: dict[str, tuple[str, str]] = {}  # ref -> (value, error); one op read per process


@dataclass(frozen=True)
class Credentials:
    url: str
    key: str
    url_source: str
    key_source: str
    error: str = ""


def config_path() -> Path:
    return Path(os.environ.get("HINDSIGHT_CONFIG") or Path.home() / ".hindsight" / "config")


def read_config(path: Path | None = None) -> dict:
    try:
        data = tomllib.loads((path or config_path()).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def op_binary() -> str:
    """The op-router wrapper first. With a bare PATH `which op` finds /usr/bin/op, the
    real binary, which has no service-account token and waits on the desktop app."""
    wrapper = Path.home() / ".local" / "bin" / "op"
    if os.access(wrapper, os.X_OK):
        return str(wrapper)
    return shutil.which("op") or "op"


def vault_read(ref: str) -> tuple[str, str]:
    """(value, error) for an op:// reference. Never raises, never logs the value."""
    if ref in _VAULT:
        return _VAULT[ref]
    timeout = float(os.environ.get("HINDSIGHT_AUTH_TIMEOUT", "") or 8)
    try:
        done = subprocess.run([op_binary(), "read", ref], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
        value = done.stdout.strip() if done.returncode == 0 else ""
        error = "" if value else f"op read failed: {(done.stderr or 'empty value').strip()[:200]}"
    except (OSError, subprocess.SubprocessError) as exc:
        value, error = "", f"op read failed: {type(exc).__name__}"
    _VAULT[ref] = (value, error)
    return value, error


def resolve(*, need_key: bool = True) -> Credentials:
    config = read_config()
    url, url_source = os.environ.get("HINDSIGHT_API_URL", "").strip(), "env"
    if not url:
        url, url_source = str(config.get("api_url") or "").strip(), "config"
    if not url:
        url, url_source = DEFAULT_URL, "default"
    url = url.rstrip("/")
    if not need_key:
        return Credentials(url, "", url_source, "skipped")

    key = os.environ.get("HINDSIGHT_API_KEY", "").strip()
    if key:
        return Credentials(url, key, url_source, "env")
    literal = str(config.get("api_key") or "").strip()
    if literal and not literal.startswith("op://"):
        return Credentials(url, literal, url_source, "config-plaintext")
    if literal:
        ref, ref_source = literal, "config-api_key-ref"
    elif "HINDSIGHT_API_KEY_REF" in os.environ:
        ref, ref_source = os.environ["HINDSIGHT_API_KEY_REF"].strip(), "env-ref"
    elif str(config.get("api_key_ref") or "").strip():
        ref, ref_source = str(config["api_key_ref"]).strip(), "config-ref"
    else:
        ref, ref_source = DEFAULT_KEY_REF, "default-ref"
    if not ref:
        return Credentials(url, "", url_source, "none", "vault lookup disabled (HINDSIGHT_API_KEY_REF is empty)")
    value, error = vault_read(ref)
    return Credentials(url, value, url_source, f"vault:{ref_source}" if value else "none", error)


def endpoint() -> tuple[str, str]:
    """(base URL, key); the key is "" when nothing resolves."""
    found = resolve()
    return found.url, found.key


def url() -> str:
    return resolve(need_key=False).url


def overrides() -> dict[str, str]:
    """HINDSIGHT_API_URL/KEY for a child process (the `hindsight` CLI, a script)."""
    found = resolve()
    env = {"HINDSIGHT_API_URL": found.url}
    if found.key:
        env["HINDSIGHT_API_KEY"] = found.key
    return env


def environment(base: dict[str, str] | None = None) -> dict[str, str]:
    return {**(os.environ if base is None else base), **overrides()}


# ------------------------------------------------------------------ config writer

_LINE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def write_config(updates: dict[str, str | None], path: Path | None = None) -> Path:
    """Set or remove top-level string keys, keeping every other line as it was.

    Atomic (temp file + rename), mode 0600. A value of None removes the key.
    """
    path = path or config_path()
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    pending = dict(updates)
    kept: list[str] = []
    in_table = False
    for line in lines:
        if line.lstrip().startswith("["):
            in_table = True
        match = None if in_table else _LINE.match(line)
        if match and match.group(1) in pending:
            value = pending.pop(match.group(1))
            if value is not None:
                kept.append(f"{match.group(1)} = {json.dumps(value)}")
            continue
        kept.append(line)
    head = [f"{name} = {json.dumps(value)}" for name, value in pending.items() if value is not None]
    first_table = next((i for i, line in enumerate(kept) if line.lstrip().startswith("[")), len(kept))
    kept[first_table:first_table] = head
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".config.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(kept).rstrip("\n") + "\n")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return path


# ------------------------------------------------------------------ CLI

def probe(found: Credentials, timeout: float = 15.0) -> tuple[int, str]:
    """(HTTP status or 0, detail) for one authenticated, read-only call."""
    request = urllib.request.Request(f"{found.url}/v1/default/banks",
                                     headers={"Accept": "application/json",
                                              **({"Authorization": f"Bearer {found.key}"} if found.key else {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read(8 << 20) or b"{}")
        banks = data.get("banks", data) if isinstance(data, dict) else data
        return response.status, f"{len(banks) if isinstance(banks, list) else '?'} banks"
    except urllib.error.HTTPError as exc:
        return exc.code, exc.reason or ""
    except (OSError, ValueError) as exc:
        return 0, type(exc).__name__


def cmd_check(args: argparse.Namespace) -> int:
    found = resolve()
    status, detail = probe(found)
    config = read_config()
    warnings = []
    if found.key_source == "config-plaintext":
        warnings.append("plaintext api_key in the config: run `hindsight-auth configure --drop-plaintext`")
    host = re.sub(r"^https?://([^/:]+).*$", r"\1", found.url)
    if host in LEGACY_HOSTS:
        warnings.append(f"{host} is the LAN-only legacy name; use {LEGACY_HOSTS[host]}")
    report = {"ok": status == 200, "status": status, "detail": detail, "url": found.url,
              "url_source": found.url_source, "key_source": found.key_source,
              "key_present": bool(found.key), "config": str(config_path()),
              "config_keys": sorted(config), "error": found.error, "warnings": warnings}
    if args.json:
        print(json.dumps(report))
    else:
        print(f"{'ok' if report['ok'] else 'FAIL'}  {found.url} -> HTTP {status} ({detail})")
        print(f"  url from {found.url_source}; key from {found.key_source}")
        for line in ([found.error] if found.error else []) + warnings:
            print(f"  ! {line}")
    return 0 if report["ok"] else 1


def cmd_env(args: argparse.Namespace) -> int:
    found = resolve()
    if not found.key:
        print(f"hindsight-auth: no key ({found.error or 'nothing resolved'})", file=sys.stderr)
    for name, value in overrides().items():
        print(f"{'export ' if args.export else ''}{name}={shlex.quote(value)}")
    return 0 if found.key else 1


def cmd_value(args: argparse.Namespace) -> int:
    if args.command == "url":
        print(url())
        return 0
    if sys.stdout.isatty() and not args.force:
        print("hindsight-auth: refusing to print the key to a terminal (pipe it, or use `exec`)",
              file=sys.stderr)
        return 2
    found = resolve()
    if not found.key:
        print(f"hindsight-auth: no key ({found.error or 'nothing resolved'})", file=sys.stderr)
        return 1
    print(found.key)
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not command:
        print("hindsight-auth exec: nothing to run", file=sys.stderr)
        return 2
    found = resolve()
    if not found.key:
        print(f"hindsight-auth: no key ({found.error or 'nothing resolved'}); running without one",
              file=sys.stderr)
    os.execvpe(command[0], command, environment())
    return 127  # unreachable


def cmd_configure(args: argparse.Namespace) -> int:
    if args.api_key:
        print("hindsight-auth configure: a literal key never goes in a file; store it in DeLoSecrets "
              "and pass --api-key-ref op://DeLoSecrets/<item-uuid>/<field>", file=sys.stderr)
        return 2
    updates: dict[str, str | None] = {}
    if args.api_url:
        updates["api_url"] = args.api_url.rstrip("/")
    ref = args.api_key_ref
    if ref is not None:
        if not ref.startswith("op://"):
            print("hindsight-auth configure: --api-key-ref must be an op:// reference", file=sys.stderr)
            return 2
        updates["api_key_ref"] = ref
    literal = str(read_config().get("api_key") or "")
    if literal and not literal.startswith("op://"):
        if not args.drop_plaintext:
            print("hindsight-auth configure: the config holds a plaintext api_key; re-run with "
                  "--drop-plaintext once it is in the vault", file=sys.stderr)
            return 2
        vaulted, error = vault_read(ref or str(read_config().get("api_key_ref") or "") or DEFAULT_KEY_REF)
        if vaulted != literal:
            print(f"hindsight-auth configure: the vault value does not match the plaintext key "
                  f"({error or 'different value'}); vault it first, nothing changed", file=sys.stderr)
            return 1
        updates["api_key"] = None
        updates.setdefault("api_key_ref", ref or DEFAULT_KEY_REF)
    if not updates:
        print("hindsight-auth configure: nothing to change", file=sys.stderr)
        return 0
    path = write_config(updates)
    print(f"updated {path}: {', '.join(f'{k}={'(removed)' if v is None else v}' for k, v in updates.items())}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hindsight-auth", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="resolve, then make one authenticated call")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=cmd_check)
    env = sub.add_parser("env", help="KEY=VALUE lines for eval or an EnvironmentFile")
    env.add_argument("--export", action="store_true")
    env.set_defaults(func=cmd_env)
    for name in ("url", "key"):
        one = sub.add_parser(name)
        one.add_argument("--force", action="store_true", help="print the key even to a terminal")
        one.set_defaults(func=cmd_value)
    run = sub.add_parser("exec", help="run a command with HINDSIGHT_API_URL/KEY set")
    run.add_argument("argv", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_exec)
    conf = sub.add_parser("configure", help="change ~/.hindsight/config without losing keys")
    conf.add_argument("--api-url")
    conf.add_argument("--api-key-ref")
    conf.add_argument("--api-key", help=argparse.SUPPRESS)
    conf.add_argument("--drop-plaintext", action="store_true")
    conf.set_defaults(func=cmd_configure)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
