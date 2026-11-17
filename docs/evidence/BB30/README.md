# BB-30 source acceptance and integration evidence

Issue: https://plane.delo.sh/33god/browse/BB-30/

Accepted source: [`f168ba8f7071ba775096e790cfc3c0e34f2c8116`](https://github.com/delorenj/bloodbank/commit/f168ba8f7071ba775096e790cfc3c0e34f2c8116).
Independent **SPEC PASS (7/7 offline)** followed by **QUALITY PASS** applies to that historical source fingerprint, not to the later documentation commit carrying these reports. Integration is an operator-authorized direct merge to `main`; no PR is asserted. This package does not authorize deployment or live traffic.

## Acceptance map

| AC | Evidence |
|---|---|
| 1. Explicit final assistant body; exclusions | Full native-finalizer and final-answer regressions; complete native-platform success/failure/cancellation probes. |
| 2. Exact lineage; distinct observed native IDs | Full-lineage, compaction-session, root/child and identity-conflict tests. |
| 3. Immutable canonical bytes and deterministic replay | Canonical TEXT guards, bool/int and int/float permanent regressions, concurrent writers, exact reopened rows and outbox bytes. |
| 4. Persist first; all PubAcks before command ACK | Gated publication tests and answer/invocation/turn/ACK fault probes. |
| 5. Restart without rerunning after capture | Abrupt process exit, unknown-outcome failure closure, SQLite aborts, double restart and uncertain-ACK probes. Capture alone is not processing success. |
| 6. Profile/thread/root scope | Serialization/isolation, actual pinned executor context, target proxy/runtime/hook checks. |
| 7. Additive schema and generic messages | Canonical envelope tests, 227-schema validation, stable event IDs / `Nats-Msg-Id`. Consumer durable dedup remains a live gate. |

## Files and actual execution

- [`source-spec.md`](source-spec.md), [`source-quality.md`](source-quality.md): exact final independent reports, copied without edits.
- [`source-spec-base.md`](source-spec-base.md): exact prior seven-AC report referenced by the final delta SPEC.
- [`native-platform-probes.py`](native-platform-probes.py), [`journal-quality-probes.py`](journal-quality-probes.py): exact independent executable artifacts, 11 + 15 tests.
- [`verification.txt`](verification.txt): integration rerun commands, environment, real stdout/stderr and exits. **290 passed in 41.26s; 26 passed in 7.25s; zero skips; 227 schema files / IDs**, all exit 0. Native methods read exact object `0408fec7a153e6c32c064acd2b8053917f1525f1` from the object-only scratch fixture; it intentionally has no HEAD.
- [`verification-results.json`](verification-results.json): command/exit index.
- [`scoped-lint.json`](scoped-lint.json): Ruff 0.17.5 comparison for the final repair's three Python paths against `ec8f5c1`: **2 baseline / 2 candidate findings; zero new findings**. Not a full-lint PASS.
- [`source-identity.json`](source-identity.json): exact copied-artifact SHA256s, reviewed application blobs and whole gateway/schema tree IDs. The evidence-only commit must preserve these application trees.
- [`unrelated-wip-before.json`](unrelated-wip-before.json): four unrelated canonical target-watch files, hashes, sizes and modes. Binary tracked/index diffs and final before/after comparisons are retained by the integration worker in profile scratch; unrelated content is not copied into this package.

Reproduction uses the canonical gateway venv Python, `PYTHONDONTWRITEBYTECODE=1`, explicit candidate `BLOODBANK_SCHEMAS_DIR` and gateway/tests/agent-hooks `PYTHONPATH`, pinned `BB30_HERMES_RELEASE`, no pytest cache, and profile-scratch `--basetemp`. Exact commands are in `verification.txt`. The unchanged independent probes deliberately assert their historical absolute candidate path. After cleanup, recreate a temporary detached worktree at that path from the accepted source before rerunning those exact assertions; do not reinterpret their original identity checks as canonical-main checks.

## Limits and separate gates

This is offline fixture/fake-broker execution plus pinned native methods, not live NATS delivery, Candystore full-body receipt, durable-consumer dedup, installed-runtime proof or paid inference. No command publication, restart, deployment, production journal, profile/config or Plane mutation occurred. Pre-capture side effects are not exactly once. The earlier schema umbrella still has an unrelated project raw-path rejection and missing worktree-local Plane marker; neither was repaired or newly claimed green. No global typecheck/lint/umbrella PASS is asserted. Historical implementer checkpoints remain historical; final independent reports and this rerun supersede their pending-review status.
