there is a real need for the service Holyfields is trying to provide, but probably not as a separate repo
right now.

For Bloodbank’s current maturity, I would fold the Bloodbank contract work back into bloodbank and treat Holyfields
as premature extraction.

The useful parts Holyfields is aiming at are legitimate:

- One canonical event contract source.
- Generated Python and TypeScript bindings.
- Runtime JSON Schemas for validation, replay, Apicurio, EventCatalog.
- Drift checks so producers and consumers cannot silently diverge.
- A neutral package boundary once multiple systems consume the same events.

  But the current shape has the worst failure mode for a contract registry: it looks authoritative while Bloodbank is
  enforcing a different, stricter contract. That is worse than having no separate registry, because it creates false
  confidence.

  My recommendation:
  1. Move schemas/bloodbank/v1/\*\*, generator scripts, and contract tests into bloodbank.
  2. Make Bloodbank’s docs/event-naming.md and validator the same contract surface, not parallel sources.
  3. Generate local SDK artifacts from Bloodbank first.
  4. Publish a package from Bloodbank if another project needs it.
  5. Re-extract Holyfields only after there are two or more serious consumers outside Bloodbank and the contract has
     stabilized.

  Holyfields should come back later as a registry/package workspace, not as the place where Bloodbank is still
  discovering its own contract model.

  The test I’d use is simple: if Bloodbank is the only system with strong opinions and the only runtime enforcing
  these rules, the contract belongs in Bloodbank. If multiple independent systems need to negotiate schemas without
  depending on Bloodbank internals, then Holyfields earns its keep. Right now, Bloodbank proper is the better center
  of gravity.
