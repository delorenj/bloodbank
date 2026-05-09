# Bloodbank

Bloodbank is the 33GOD event and command platform.

## What it does

- Publishes immutable domain events
- Handles command/reply workflows with correlation IDs
- Powers replay/trace operations for debugging and audits
- Exposes adapter surfaces for OpenClaw and internal automation flows

## Local development

Use mise tasks from this repo root:

```bash
mise run build
mise run test
mise run lint
mise run health
```

## Repository map

- `event_producers/` — event/command publishing and transport helpers
- `services/` — component services
- `adapters/` — integration adapters
- `compose/` — local runtime topology
- `ops/` — bootstrap, smoke, and operational scripts
- `_bmad-output/stories/` — canonical story planning artifacts
- `_bmad-output/autopilot/logs/` — canonical autopilot execution logs

## Planning + status artifacts

Authoritative planning state lives in BMAD markdown/workflow artifacts and source code.

`_bmad-output/workspace.html` is a generated human-facing dashboard only.
