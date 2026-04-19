# Bloodbank v3 Holyfields contract work tracker

This tracker records the Holyfields-owned work that Bloodbank v3 depends on but
does not implement inside this repository.

## Repository boundary

- Bloodbank owns runtime, ops, adapter scaffolds, and migration docs.
- Holyfields owns the contract definitions, generation pipeline, and registry
  synchronization work.
- No Bloodbank ticket should hide Holyfields implementation work in this repo.

## External Holyfields work

### Base event schema

- Define the base event schema used by every v3 immutable event.
- Establish the shared fields, extension policy, and validation rules.
- Keep this work in Holyfields, not Bloodbank.

### Command envelope schema

- Define the command envelope schema used by every mutable v3 command.
- Establish required fields, timeout semantics, and reply conventions.
- Keep this work in Holyfields, not Bloodbank.

### AsyncAPI template

- Publish the first reusable AsyncAPI template for service-level contracts.
- Include the standard structure for channels, messages, owners, and examples.
- Keep this work in Holyfields, not Bloodbank.

### SDK generation

- Generate the Python SDK from Holyfields contracts.
- Generate the TypeScript SDK from Holyfields contracts.
- Keep generated package ownership in Holyfields, not Bloodbank.

### Catalog generation

- Generate EventCatalog output from Holyfields contract sources.
- Keep catalog generation and catalog publishing in Holyfields, not Bloodbank.

### Apicurio sync

- Synchronize Holyfields schemas to Apicurio Registry.
- Keep schema registry publishing and compatibility enforcement in Holyfields,
  not Bloodbank.

## Bloodbank dependency notes

- Bloodbank adapter scaffolds should wait for these outputs before claiming
  production readiness.
- Bloodbank docs should point readers to this tracker whenever a ticket depends
  on Holyfields work.
- Bloodbank should not create parallel schema definitions while these outputs
  are still external work.
