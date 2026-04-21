# Bloodbank v3 Operator CLI (`bb_v3`)

Operator tool for the 33GOD v3 event platform sandbox. This CLI is intended
for **humans operating** the v3 stack (emission tests, trace walkthroughs,
replay rehearsals) — it is **not** the primary production publish path.

Architectural source of truth:

- `docs/architecture/v3-implementation-plan.md` (metarepo)
- `docs/architecture/ADR-0001-v3-platform-pivot.md` (metarepo)

Per ADR-0001: production traffic flows through Dapr publishers embedded in
services, using Holyfields-generated SDKs. The CLI is a diagnostic and
operator tool, not a replacement for that publish path.

## Status in this wave

All subcommands are **stubs** in the first scaffold wave. They return quickly,
perform no network I/O, and never publish traffic. Production-capable
replay/trace/emit functionality lands in later tickets (see `ops/v3/replay/`
and `ops/v3/trace/` docs produced under V3-007).

## Subcommands

| Subcommand | Purpose                                                                                   | Wave 1 behavior                                       |
|------------|-------------------------------------------------------------------------------------------|-------------------------------------------------------|
| `doctor`   | Verify the local v3 scaffold is intact (file presence checks only; no network, no Docker) | Prints `PASS path` / `WARN path` / `FAIL path reason` per artifact; exit 0 iff no FAIL. |
| `trace`    | Walk an event chain by correlation/causation IDs                                          | Prints "not yet implemented" pointer. Exit 0.         |
| `replay`   | Replay a historical event into the sandbox (preserving original IDs)                      | Prints "not yet implemented" pointer. Exit 0.         |
| `emit`     | Publish a handcrafted event for smoke testing                                             | Prints "not yet implemented" pointer. Exit 0.         |

## Running without installing

The CLI is dependency-free and uses only the Python standard library. It can
be invoked directly from a checkout, from any working directory:

```bash
# from the bloodbank root
python cli/v3/bb_v3.py doctor
python cli/v3/bb_v3.py trace
python cli/v3/bb_v3.py replay
python cli/v3/bb_v3.py emit

# or from anywhere — the CLI resolves its own location to find scaffold files
python /path/to/bloodbank/cli/v3/bb_v3.py doctor
```

If the file is marked executable (it is after scaffold install):

```bash
./cli/v3/bb_v3.py doctor
```

Python 3.11+ is required. No third-party dependencies. No `pip install` step.

## Usage examples

### `doctor`

Static, read-only check that the v3 scaffold is present and self-consistent:

```bash
$ python cli/v3/bb_v3.py doctor
PASS compose/v3/docker-compose.yml
PASS compose/v3/components/pubsub.yaml
PASS compose/v3/components/statestore.yaml
PASS compose/v3/components/secretstore.yaml
PASS compose/v3/nats/streams.json
PASS compose/v3/README.md
PASS ops/v3/bootstrap/check-platform.sh
PASS cli/v3/bb_v3.py
doctor: 8/8 artifacts present (0 warn, 0 fail)
```

Exit codes:

- `0` — every required artifact present (WARN lines do not fail this wave).
- non-zero — at least one artifact marked `FAIL`.

### `trace`

```bash
$ python cli/v3/bb_v3.py trace
trace: not yet implemented — see ops/v3/trace/README.md
```

### `replay`

```bash
$ python cli/v3/bb_v3.py replay
replay: not yet implemented — see ops/v3/replay/README.md
```

### `emit`

```bash
$ python cli/v3/bb_v3.py emit
emit: not yet implemented — operator emission requires Dapr sidecar; will land in a later ticket
```

## Invariants

- No third-party Python dependencies. Standard library only.
- No network I/O from any subcommand in this wave.
- No publishing to Dapr, NATS, Apicurio, or EventCatalog.
- `doctor` resolves the bloodbank root from its own file path, so it works
  regardless of the current working directory.

## Relationship to `ops/v3/bootstrap/check-platform.sh`

`bb_v3 doctor` and `ops/v3/bootstrap/check-platform.sh` check the same set of
required scaffold files. The bash script is the portable, junior-friendly
entry point for CI and shell-only environments; the Python CLI is the
operator-facing multi-tool. If you add a required file, update both checkers.

## Scope guard

This ticket (V3-005 / BB-21) ships the CLI skeleton only. Do not add
production emission, network calls, or third-party dependencies here —
those belong in later tickets under the V3 plan.
