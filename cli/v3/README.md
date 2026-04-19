# Bloodbank v3 operator CLI scaffold

This directory holds the safe operator-facing entry point for Bloodbank v3.

Ownership split:

- Holyfields owns schemas, contracts, and generated artifacts.
- Bloodbank owns runtime operations, broker plumbing, and operator tooling.

## What is here

- `bb_v3.py` is the Python 3.12 stdlib-only CLI skeleton.
- `doctor` performs local static checks only.
- `trace`, `replay`, and `emit` are command stubs for future operator flows.

## Safety rules

- The CLI does not publish real traffic yet.
- The CLI does not require Docker, Dapr, NATS, or network access for the scaffold.
- The CLI should be used as a runtime and operations tool, not as a contract source.
- Any schema work belongs in Holyfields, not in Bloodbank.

## Usage

Run the scaffold directly:

```bash
python cli/v3/bb_v3.py doctor
python cli/v3/bb_v3.py trace --help
python cli/v3/bb_v3.py replay --help
python cli/v3/bb_v3.py emit --help
```

`doctor` is the only command intended to return a useful result in this first
wave. The other commands are explicit placeholders so future tickets can wire
them to the v3 runtime safely.
