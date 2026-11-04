---
title: 'Stop Dev Journal partial retry mail churn'
type: 'bugfix'
created: '2026-11-03'
status: 'in-review'
route: 'oneshot'
review_loop_iteration: 0
context: []
---
<frozen-after-approval reason="delegated bounded implementation; independent review required">
## Intent
Suppress repeated partial emails for one immutable journal generation, allow one completion update and a new generation/correction, retain retry processing and all history. Prepare a durable provider retry payload before sending. Replace only the four journal native clock exports with Candybar webhook sources and disabled manifests; preserve the daily 06:00 report producer and event-driven ingress. No live deployment, activation, restart, mail, or schedule registration is authorized; feature push only.
</frozen-after-approval>

## Implementation Notes
- Actual execution snapshots 583338/583558 use selectReport and send one generation with hashes bff04773/c9f370b2. Partial errors vary (50 vs 36 Plane rate-limit errors); report attempts 311. Durable state is ~/.local/state/dev-journal/state.sqlite; n8n tables empty. Editor has rollup but active execution snapshots selectReport: do not attribute mail to editor drift or rewrite live state.
- Keep last successful receipt untouched. Logical delivery identity uses immutable report_key/content_sha256 and phase, not rendered markdown. A persisted pending mail outbox freezes provider retry bytes. Existing email_id suppresses all legacy partial retries; a complete transition may send once. New generation gets a new identity.
- Given partial receipt, when diagnostics/order change, then prepare no mail. Given pending provider attempt, when retries change state, then replay exact payload/key. Given completed processing after partial, then send one update. Given a new generation, then allow a new report. Given historical backfill, then never mail.
- Scope: journal source + generated programs/workflows, additive JournalStore pending_email column migration, targeted tests, four disabled Candybar manifests, incident/release notes. No independent approval claimed; parent must assign reviewer.
- Verification: isolated node test suites, old-vs-new replay of real execution payloads, node build and package tests. CodeGraph index exists but Voyage sidecar absent; used bounded source inspection rather than create network embeddings.
