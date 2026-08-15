# Portfolio orchestration contracts

Bloodbank owns the immutable coordination facts shared by the DeLoNET Director,
child PMs, Holocene, and other projections. These events do **not** create a
second lifecycle engine: the child board and its local PM remain authoritative
for execution state. The portfolio stream is the company-level routing and
receipt ledger.

## Contract family

All messages are CloudEvents 1.0 events on
`bloodbank.evt.v1.portfolio.<entity>.<action>`. Repository, Plane project,
ticket, work, delegation, lease, and agent identities appear only in `data`.
Every payload requires `portfolio_id`, `target_agent_id`, `occurred_at`, and
`idempotency_key`; the same idempotency key is repeated at envelope level.

| Phase | Types |
| --- | --- |
| Intake | `intake.received`, `intake.triaged` |
| Delegation | `work.delegated`, `work.updated`, `receipt.recorded` |
| Human boundary | `approval.requested`, `approval.resolved` |
| Exceptions | `escalation.raised`, `escalation.resolved` |
| Three-slot scheduler | `capacity.recorded`, `lease.granted`, `lease.released`, `lease.expired` |

The Director still invokes an agent through the existing command
`bloodbank.cmd.v1.agent.invocation.start`; the fleet-shared Hermes gateway keeps
exclusive ownership of that command. `work.delegated` records the portfolio
decision and never substitutes for the invocation command.

## Producer rules

1. Build the subject with `core.validate.subject_for`; never include an ID in
   the type or subject.
2. Persist the CloudEvents `id`, `correlationid`, `causationid`, envelope/data
   `idempotency_key`, and payload before publish. A retry reuses them.
3. Set `data.target_agent_id` explicitly. Routers must not infer a default
   target from the subject, repository, or producer.
4. Use one correlation ID from intake through terminal receipt. Set causation
   to the immediate prior event/command ID; only a root intake may use null.
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
  overwrite the terminal outcome. `assert_terminal_receipt_retry` implements
  the comparison.
- Project `work.updated` facts for visibility only. Do not drive a second state
  machine or mutate a child board solely because a projection was replayed.
- A lease is released explicitly after the work attempt or becomes reclaimable
  after `lease.expired`; `capacity.recorded` must satisfy
  `in_use + available == total`.

## Fixtures and compatibility

Representative payloads are in
`ops/fixtures/portfolio-contracts.v1.json`; `mise run
smoketest:portfolio-contracts` wraps and validates complete envelopes.

- **Candystore:** its current Dapr subscription is `bloodbank.evt.v1.>`, so it
  will ingest and retain this family without a subscription change. A
  portfolio-specific query/projection is separate downstream work.
- **Fleet Hermes gateway:** it filters only
  `bloodbank.cmd.v1.agent.invocation.start`; these events neither collide with
  nor change that command consumer. Producing portfolio facts from gateway
  completion hooks is separate downstream work.
- **Runtime scope:** this unit defines contracts and validation only. It does
  not deploy a Director producer, Candystore projection, or gateway publisher.
