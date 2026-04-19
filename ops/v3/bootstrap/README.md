# Bloodbank v3 bootstrap checks

This directory holds local, static validation for the Bloodbank v3 scaffold.

## Purpose

- Confirm the operator-facing scaffold files exist.
- Keep bootstrap validation independent from Docker, Dapr, NATS, or network access.
- Make it clear that Bloodbank owns runtime and ops, while Holyfields owns schemas.
- These checks are local-only and have no production side effects.

## Check script

Run:

```bash
bash ops/v3/bootstrap/check-platform.sh
```

The script only checks local files and prints clear pass/fail/action messages.
It is intended to be safe to run on a clean workstation or in CI without any
platform services running.
No Docker, Dapr, NATS, or network access is required.

## What this does not do

- It does not start containers.
- It does not touch production traffic.
- It does not validate schemas in place.
- It does not reach out to external services.

Future tickets can extend this bootstrap area with richer runtime checks once
the v3 platform scaffold is in place.
