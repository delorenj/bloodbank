# BB-30 implementation plan

Implementer: bb30-codex-implementer. Base: a3ec3b53fb9fd40f854680e0e72949d184c7d979.
Scope: gateway, additive message schema, deterministic tests and this evidence.
No deployment, paid inference, live command, board mutation or independent review.

Read-only references: momo/PILLARS.md; spec-holocene-auto SPEC.md and companions
(CAP-4/6/7); Holocene HOLOC-9.md and implementation-artifacts/holoc-9-feasibility-evidence.md.
The companion feasibility document is beside issue-evidence/, not inside it.

GoF seam: Adapter translates Hermes finalizer callbacks into the existing
Bloodbank Command's immutable result. Reuse the SQLite execution journal as
the transactional outbox: terminal_events holds the ordered answer-plus-terminal
batch. No new broker, universal continuity layer or speculative abstraction.
This hardens the actual Holocene dependency (Dogfood the Platform; Build LEGO,
Not Statues).

1. Wrap the installed message handler with a scoped ContextVar command binding.
   Bind native turn/task on root pre_llm_call; observe final native session even
   after compaction; ignore delegated child callbacks.
2. post_llm_call supplies the full final assistant candidate. on_session_end
   supplies explicit outcome flags. Successful nonempty nonsilent candidates
   become role=assistant/final_answer=true facts. Never publish from send().
3. Validate via core.validate and local schemas, then atomically persist the
   exact ordered batch before any publish. Surface swallowed hook failures to
   the adapter waiter; await JetStream PubAck for every fact before command ACK.
4. Replay completed batches with deterministic event/message IDs, byte-identical
   lineage/body/timestamps, without calling Hermes. Conflicting captures fail closed.

| AC | Deterministic evidence |
|---|---|
| 1 | Full UTF-8 >500 chars; interim/send/history/tool/reasoning/empty/silent/failure/interruption/cancellation/rejection excluded |
| 2 | Issuer snapshot, command causation, logical identifiers and native identifiers independently asserted |
| 3 | Duplicate capture stable; conflict refused; stored bytes preserved on replay |
| 4 | Journal inspected at first answer publish; blocked PubAck prevents command ACK |
| 5 | Persistence/publish/terminal/ACK failures, redelivery and reopened temp journal; no post-capture reinvocation |
| 6 | Same profile/thread serial, different profiles isolated; child callbacks excluded; copy_context executor propagation |
| 7 | Canonical validator accepts positives/rejects malformed finals; generic compatibility; Nats-Msg-Id equals event ID |

Pre-capture process loss remains ambiguous and may rerun Hermes/external effects.
After durable capture the batch is replayable; no global exactly-once claim.
Live Candystore/full-body proof: NOT AUTHORIZED / NOT EXECUTED. PM owns independent
review, publication, integration and acceptance. No configured gateway lint or
typecheck command was found in pyproject.toml or mise.toml; report availability honestly.
