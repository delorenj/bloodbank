# Portfolio orchestration contracts

Bloodbank owns the immutable coordination facts shared by the DeLoNET Director,
child PMs, Holocene, and other projections. These events do **not** create a
second lifecycle engine: the child board and its local PM remain authoritative
for execution state. The portfolio stream is the company-level routing and
receipt ledger.

## Contract family

All messages are CloudEvents 1.0 events on
`bloodbank.evt.portfolio.<entity>.<action>`. Repository, Plane project,
ticket, work, delegation, lease, and agent identities appear only in `data`.
Every payload requires `portfolio_id`, `target_agent_id`, `correlation_id`,
`causation_id`, `occurred_at`, and `idempotency_key`. Correlation, causation,
and idempotency are repeated at envelope level and must match exactly.

| Phase | Types |
| --- | --- |
| Intake | `intake.received`, `intake.triaged` |
| Delegation | `work.delegated`, `work.updated`, `receipt.recorded` |
| Human boundary | `approval.requested`, `approval.resolved` |
| Exceptions | `escalation.raised`, `escalation.resolved` |
| Three-slot scheduler | `capacity.recorded`, `lease.granted`, `lease.released`, `lease.expired` |

The Director still invokes an agent through the existing command
`bloodbank.cmd.agent.invocation.start`; the fleet-shared Hermes gateway keeps
exclusive ownership of that command. `work.delegated` records the portfolio
decision and never substitutes for the invocation command.

## Producer rules

1. Build the subject with `core.validate.subject_for`; never include an ID in
   the type or subject.
2. Persist the CloudEvents `id`, envelope/payload correlation and causation,
   envelope/data `idempotency_key`, and complete payload before publish. A
   retry reuses them.
3. Set `data.target_agent_id` explicitly. Routers must not infer a default
   target from the subject, repository, or producer.
4. Set `correlationid == data.correlation_id` and use one value from intake
   through terminal receipt. Set
   `causationid == data.causation_id == <immediate-parent-id>` for non-root
   events. Only `intake.received` is a root, and both causation fields are null.
5. For `receipt.recorded`, set the envelope `id` equal to `data.receipt_id`, set
   `causationid` equal to `data.terminal_event_id`, use
   `portfolio.work.terminal:<work_id>:attempt:<attempt>`, and calculate
   `outcome_digest` with `portfolio_terminal_receipt_digest`.

## Consumer rules

- Treat every stream as at-least-once. Deduplicate ordinary events by
  CloudEvents `id` and use `idempotency_key` for materialized effects.
- Terminal receipts are first-write-wins by `idempotency_key`. An exact replay
  is acknowledge/no-op. A different envelope for an existing key is a terminal
  conflict: retain the first receipt, record/escalate the conflict, and never
  overwrite the terminal outcome. `assert_terminal_receipt_retry` first runs
  the canonical full envelope and JSON Schema validator over both messages,
  then implements the exact comparison. Identically malformed copies are not
  retries and must be rejected.
- Project `work.updated` facts for visibility only. Do not drive a second state
  machine or mutate a child board solely because a projection was replayed.
- A lease is released explicitly after the work attempt or becomes reclaimable
  after `lease.expired`; `capacity.recorded` must satisfy
  `in_use + available == total`.

## Fixtures and compatibility

Representative payloads are in
`ops/fixtures/portfolio-contracts.v1.json`; `mise run
smoketest:portfolio-contracts` wraps and validates complete envelopes.

`mise run smoketest:portfolio-transport` is the real-binding integration
proof. Its test publisher validates a root envelope, sends it through the
already-running `BLOODBANK_EVENTS` JetStream, pulls the exact message through a
temporary consumer, and runs the same complete validator at the consumer
boundary. It deliberately does not start or provision NATS: an absent service,
stream, or network is reported as a residual runtime gate.

- **Candystore:** its current durable Dapr subscription is `bloodbank.evt.>`, so
  it ingests every portfolio subject admitted by `BLOODBANK_EVENTS`, including
  the exact v2 maintenance failure subject. A portfolio-specific
  query/projection is separate downstream work.
- **Fleet Hermes gateway:** it filters only
  `bloodbank.cmd.agent.invocation.start`; these events neither collide with
  nor change that command consumer. Producing portfolio facts from gateway
  completion hooks is separate downstream work.
- **Runtime scope:** the transport harness is a bounded test publisher and
  consumer only. It does not implement or deploy a Director publisher,
  Candystore projection/query, Dapr portfolio subscription, or gateway
  completion publisher. Those remain separate live integration gates.
