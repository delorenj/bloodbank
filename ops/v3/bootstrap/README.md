# Bloodbank v3 platform bootstrap check

`check-platform.sh` is a **junior-friendly static validator** for the v3
scaffold. It verifies that every required file is present on disk. It
performs no Docker, Dapr, NATS, Apicurio, or network operations.

Architectural source of truth:

- `docs/architecture/v3-implementation-plan.md` (metarepo)
- `docs/architecture/ADR-0001-v3-platform-pivot.md` (metarepo)

## What it checks

The script verifies the presence of the minimum v3 scaffold artifacts --
Compose file, Dapr component manifests, NATS topology definition, the
sandbox README, the operator CLI entrypoint, and this bootstrap script
itself. The exact list is inline in `check-platform.sh`.

The same list is cross-checked by `python cli/v3/bb_v3.py doctor`. If you
add a required file, update **both** checkers so the two stay in sync.

## What it does not do

- It does **not** run `docker compose config` or any other Docker command.
- It does **not** contact a NATS server or a Dapr sidecar.
- It does **not** talk to Apicurio, EventCatalog, or any HTTP endpoint.
- It does **not** parse YAML or JSON semantics -- file presence only.

Runtime / semantic validation will land in later tickets (see V3-011 in the
implementation plan and ADR-0001).

## How to run

```bash
# from the bloodbank root
bash ops/v3/bootstrap/check-platform.sh

# or from anywhere -- the script resolves its own location
bash /path/to/bloodbank/ops/v3/bootstrap/check-platform.sh
```

Typical successful output:

```
PASS compose/v3/docker-compose.yml
PASS compose/v3/components/pubsub.yaml
PASS compose/v3/components/statestore.yaml
PASS compose/v3/components/secretstore.yaml
PASS compose/v3/nats/streams.json
PASS compose/v3/README.md
PASS ops/v3/bootstrap/check-platform.sh
PASS cli/v3/bb_v3.py
Bootstrap check: 8/8 artifacts present
```

## Exit codes

| Code | Meaning                                                               |
|------|-----------------------------------------------------------------------|
| `0`  | **PASS.** Every required artifact is present.                         |
| `1`  | **FAIL.** One or more artifacts are missing. Each missing file is printed on its own `FAIL` line with a one-line reason; the final summary reports the total missing count. |

Non-zero exit is usable as a CI gate.

## Extending the check

To add a new required file:

1. Append a line to the `REQUIRED_FILES` array near the top of
   `check-platform.sh`. Entries are paths relative to the bloodbank repo
   root (the script resolves that automatically).
2. Add the matching entry to `SCAFFOLD_MANIFEST` in `cli/v3/bb_v3.py` so
   `bb_v3 doctor` remains equivalent.
3. If the addition is expected before a scaffold wave completes, mark it
   as `FAIL`-severity in both checkers. If it legitimately arrives mid-wave
   (e.g. in a later ticket in the same sprint), consider `WARN` severity
   in `bb_v3 doctor`; the shell script does not yet support `WARN` because
   its only job is strict CI gating.

Do not add checks that require Docker, Dapr, network I/O, or YAML/JSON
parsing to this script. That functionality belongs in `cli/v3/bb_v3.py`
or in later dedicated ops tooling.

## Relationship to `bb_v3 doctor`

| Aspect               | `check-platform.sh`                          | `bb_v3 doctor`                        |
|----------------------|-----------------------------------------------|---------------------------------------|
| Runtime requirement  | bash only                                     | Python 3.11+                          |
| Intended audience    | CI, shell-only environments, new operators    | operators who already use the CLI     |
| Severity levels      | PASS / FAIL                                   | PASS / WARN / FAIL                    |
| Failure behavior     | exit 1 on any missing file                    | exit 1 on any FAIL-severity miss only |

Both perform the same file-presence checks; choose the entry point that
matches your environment.

## Scope guard

This ticket (V3-006 / BB-22) ships the bootstrap script and this README
only. Do not add Docker, Dapr, network, or schema-validation logic here --
those belong in later tickets in the V3 plan.
