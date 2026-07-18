# Lifecycle Controller Extraction Provenance

The lifecycle controller is no longer an executable Bloodbank service.
Bloodbank retains its canonical schemas, naming, validation, and transport;
the standalone repository [`delorenj/lifecycle`](https://github.com/delorenj/lifecycle)
is the only operational lifecycle authority and writer.

The historical extraction is reproducible from these immutable identities:

- Bloodbank extraction-source pin: `03415705a39d77f1e6d73c8a9c92ee177320df7e`
- Standalone extracted commit: `ae31b94c31eac6d4f9e7e57cc75b2eb673cbd8d2`
- Extracted `services/lifecycle-controller` subtree tree: `36054453f7ee192d7715a1676328c15bfdf89607`
- 33GOD standalone lifecycle integration pin at contract closure: `3e1c357d87d0853a627bc7d6ec8877342c61a230`

The source tree remains recoverable from Git history at the source pin and
must not be copied back into Bloodbank. This branch removes the embedded
entrypoint, database repository/schema, worker, sweeper, outbox publisher,
dogfood mutation script, package metadata, tests, and operational runbook.
There is therefore no Bloodbank service entry, runnable controller, or
second operational lifecycle database writer after extraction.

Contract behavior is documented in `docs/lifecycle-contracts.md`. Historical
lifecycle event schemas remain for wire compatibility; they do not create or
delegate producer authority.
