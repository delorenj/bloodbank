# Hermes (Bloodbank)

Hermes is the repository-scoped PM/ingress agent for Bloodbank.

## Provisioning status
✅ Provisioned as a repo-local runtime.

Runtime home:
- `agents/hermes/runtime` (scoped via `HERMES_HOME`)

Launcher:
- `agents/hermes/bin/hermes-bloodbank`

Provision script:
- `agents/hermes/provision.sh`

## Contract
- Consumes Bloodbank events relevant to this repository.
- Produces Bloodbank events as outputs/decisions/artifacts.
- Operates independently from Hermes instances assigned to other repositories.

## Initial subject lane (proposed)
- `agent.hermes.bloodbank.#` for Hermes-directed command/event routing.

## Usage
From repo root:

```bash
./agents/hermes/provision.sh
./agents/hermes/bin/hermes-bloodbank status
./agents/hermes/bin/hermes-bloodbank chat
```

## Notes
- Runtime scaffolding lives inside this repo (`agents/hermes/`) so deployment is repo-local.
- `runtime/` state is intentionally git-ignored (sessions/logs/auth/secrets).
- Planned future enhancement: emit artifact creation events when docs are created (not implemented yet).
