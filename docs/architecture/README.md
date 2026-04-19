# Architecture docs

This directory is the entrypoint for Bloodbank architecture work.

Use these documents in this order:

1. Read [bloodbank-vnext.md](bloodbank-vnext.md) for the target platform,
   contract model, rollout phases, and workstream breakdown.
2. Read [dapr-vs-faststream.md](dapr-vs-faststream.md) for the runtime
   selection rubric and the decision to use Dapr for Bloodbank vNext.
3. Read [overhaul-backlog.md](overhaul-backlog.md) for the first-wave ticket
   map and strict sequencing constraints.
4. Read [v3-implementation-plan.md](../../v3-implementation-plan.md) for the
   junior-safe execution plan, Plane ticket source, and subagent protocol.
5. Read [v3-holyfields-contract-work.md](v3-holyfields-contract-work.md) for
   the Bloodbank-side tracker of Holyfields-owned contract work.
6. Use [v3-plane-tickets.json](v3-plane-tickets.json) as the import payload
   for the `v3-refactor` Plane epic and child tickets.
7. Use [GOD.md](../../GOD.md) only when you need to operate,
   inspect, or retire the current v2 implementation.

These docs define the active overhaul target. New platform work must align with
them unless a later architecture decision record replaces them.
