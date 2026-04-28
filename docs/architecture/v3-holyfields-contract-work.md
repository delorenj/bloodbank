# Bloodbank v3 — Holyfields contract work tracker

This document tracks **Holyfields-side work** that Bloodbank v3 depends on.
It is **not** something implemented inside bloodbank. Bloodbank owns runtime
and ops; Holyfields owns contracts and generation. This tracker exists so
bloodbank tickets can link to the Holyfields work they block on without
hiding that work inside bloodbank.

Companion docs:

- Metarepo plan: [../../../docs/architecture/v3-implementation-plan.md](../../../docs/architecture/v3-implementation-plan.md).
- ADR-0001: [../../../docs/architecture/ADR-0001-v3-platform-pivot.md](../../../docs/architecture/ADR-0001-v3-platform-pivot.md).
- ADR-0002: [../../../docs/architecture/ADR-0002-holyfields-scope-refactor.md](../../../docs/architecture/ADR-0002-holyfields-scope-refactor.md).
- Bloodbank architecture index: [README.md](README.md).

## Status update — 2026-04-28 (post-ADR-0002)

ADR-0002 narrowed Holyfields' scope to **JSON Schema source + Pydantic/Zod
generation only**. Several work items below were authored before that
decision and are now reassigned. The table reflects current ownership:

| # | Work item | Ownership now | Status |
|---|---|---|---|
| 1 | CloudEvents 1.0 base schema | Holyfields | **DONE** (`_common/cloudevent_base.v1.json`, PR holyfields#21) |
| 2 | Command envelope schema | Holyfields | Pending |
| 3 | AsyncAPI template per service | **Each service** (not Holyfields) | Reassigned per ADR-0002 |
| 4 | Python SDK generation | Holyfields | **DONE** (CI gen + drift, PR holyfields#25) |
| 5 | TypeScript SDK generation | Holyfields | **DONE** (same PR) |
| 6 | EventCatalog source | **Consumes** aggregated AsyncAPI + Apicurio (not authored) | Reassigned per ADR-0002 |
| 7 | Apicurio sync | Holyfields CI (runtime registry populated by CI) | Pending — runs at deploy time |

The narrative below remains historically accurate but should be read
through the ADR-0002 lens: items 3 and 6 are no longer Holyfields work,
and item 7 is a CI-driven publish step rather than a standalone script.

## Plane ticket

**HOLYF-2** — Holyfields project.

URL: <https://plane.delo.sh/33god/projects/5f764642-8442-4005-9035-6e5041aaf9ba/issues/c3b8d87c-9d11-41f9-915c-660d2b782c36/>

HOLYF-2 is the **parent tracker** for the v3 contract work listed below. Do
not create a second Holyfields tracker ticket for Bloodbank-initiated work;
reference HOLYF-2 from BB tickets instead.

## Work items (owned by Holyfields)

All items live in the Holyfields repo and the Holyfields Plane project. They
are enumerated here so bloodbank tickets can point at the specific
deliverable they depend on.

1. **CloudEvents 1.0 base schema registration.** Publish the canonical
   CloudEvents 1.0 envelope with the 33GOD extension fields
   (`correlationid`, `causationid`, `producer`, `service`, `domain`,
   `schemaref`, `traceparent`) and register it with Holyfields.
2. **Command envelope schema.** Publish the mutable command envelope
   (`command_id`, `command_type`, `target_service`, `issued_by`,
   `issued_at`, `timeout_ms`, `correlation_id`, `causation_id`, `reply_to`,
   `payload_schema`, `payload`) and register it with Holyfields.
3. **AsyncAPI template per service.** Ship one reusable AsyncAPI document
   template each producing service can copy. Must include standard
   structure for channels, messages, owners, and examples.
4. **SDK generation — Python.** Generate a Python SDK from Holyfields
   contracts. Bloodbank adapters (see below) consume this SDK.
5. **SDK generation — TypeScript.** Generate a TypeScript SDK from
   Holyfields contracts for service-side use on the TS stack.
6. **EventCatalog source.** Produce the source content that EventCatalog
   renders. Catalog publishing stays inside Holyfields, not bloodbank.
7. **Apicurio sync script.** Synchronize Holyfields schemas into Apicurio
   Registry. Producers and consumers fetch schemas from Apicurio at
   runtime; Holyfields is the write side.

## Dependency direction

Bloodbank tickets that need Holyfields output **block on HOLYF-2
deliverables** until the relevant artifact lands.

Specifically:

| Bloodbank ticket | Bloodbank scope                             | Blocks on                                                                 |
|------------------|---------------------------------------------|---------------------------------------------------------------------------|
| BB-24 (V3-008) — hookd adapter            | `adapters/v3/hookd/`           | Items 1, 2, 4 (base envelope schemas + Python SDK).                       |
| BB-24 (V3-008) — openclaw adapter         | `adapters/v3/openclaw/`        | Items 1, 2, 4 (base envelope schemas + Python SDK).                       |
| BB-24 (V3-008) — infra_dispatcher adapter | `adapters/v3/infra_dispatcher/`| Items 1, 2, 4 (base envelope schemas + Python SDK).                       |

None of the three adapters from BB-24 can write real code until the Python
SDK is consumable — they must translate external payloads into
Holyfields-generated types, not hand-rolled envelopes.

Downstream tickets (reference slice, production cutover) additionally block
on items 3, 5, 6, and 7. Those are tracked separately; this tracker records
the direct bloodbank dependencies only.

## Non-blocking work

The documentation wave (BB-23, BB-25, BB-26) does **not** block on HOLYF-2.
Those tickets produce specification and tracking docs that describe the
contract shape the Holyfields work must implement.

## Explicit rule

**Do not edit `holyfields/` from bloodbank tickets.** All Holyfields changes
go through HOLYF-2 in the Holyfields Plane project. If a bloodbank ticket
appears to require a schema change, a new AsyncAPI document, an SDK regen,
or an Apicurio update, stop — that work belongs in Holyfields.

If a bloodbank ticket tries to hide Holyfields work inside bloodbank, the
reviewer rejects the ticket and opens a Holyfields sub-ticket linked to
HOLYF-2.
