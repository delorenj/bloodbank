# Architecture docs

This directory is the entrypoint for Bloodbank architecture work.

## v3 direction (metarepo-owned)

The active v3 platform plan lives in the **metarepo**, not in bloodbank:

- [v3 implementation plan](../../../docs/architecture/v3-implementation-plan.md) —
  source of truth for the v3 pivot.
- [ADR-0001: v3 platform pivot](../../../docs/architecture/ADR-0001-v3-platform-pivot.md) —
  ratified decisions (Dapr, NATS JetStream, CloudEvents, AsyncAPI,
  EventCatalog, Apicurio).

Everything in this directory supports or historicizes that plan.

## Bloodbank-local docs

1. [bloodbank-vnext.md](bloodbank-vnext.md) — target platform, contract model,
   rollout phases, and workstream breakdown. Pre-dates the metarepo plan.
2. [dapr-vs-faststream.md](dapr-vs-faststream.md) — runtime selection
   rubric. Ratified by ADR-0001.
3. [overhaul-backlog.md](overhaul-backlog.md) — first-wave ticket map.
   Superseded by the metarepo v3 implementation plan; retained for
   historical context.
4. [v3-holyfields-contract-work.md](v3-holyfields-contract-work.md) —
   Bloodbank-side tracker of Holyfields-owned contract work (HOLYF-2 in the
   Holyfields Plane project).
5. [GOD.md](../../GOD.md) — current-state v2 component reference. Use only
   when operating, inspecting, or retiring the legacy stack.

New platform work must align with the metarepo v3 plan unless a later
architecture decision record replaces it.
