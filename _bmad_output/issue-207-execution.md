# Issue 207 — execution closeout

## Ticket
- Issue: #207
- Title: BMAD: prevent runtime SOUL.md mutations in primary checkout
- Owner: Hermes (pilot loop)

## Scope completed
- Reproduced and captured unsanctioned tracked-doc mutation signal on `agents/hermes/pm/SOUL.md` in primary checkout and matching runtime `SOUL.md` local mutation.
- Contained drift by reverting both tracked-file mutations in primary checkout and runtime submodule, restoring strict-clean repo-health.
- Bootstrapped dedicated issue branch/worktree (`/tmp/bloodbank-issue-207`, `fix/issue-207-soul-mutation-guardrail`) for guardrail implementation under ticket-first discipline.
- Implemented guardrail: switched runtime submodule mode in `.gitmodules` from `ignore=untracked` to `ignore=dirty` so runtime-local tracked-file edits (including `SOUL.md`) no longer dirty primary checkout.
- Updated guardrail verification test `ops/smoketest/smoketest-hermes-runtime-hygiene.sh` to enforce `ignore=dirty` contract.
- Updated operator guidance in `AGENTS.md` to document the `agents/hermes/pm/runtime` operator-local posture and required submodule ignore mode.

## Out of scope
- Root-cause elimination inside runtime process for why it attempts tracked doc mutations.

## Verification evidence
- `git diff -- agents/hermes/pm/SOUL.md` → detected inserted "Template-governor command contract" block during incident.
- `git -C agents/hermes/pm/runtime status --short --branch` → showed tracked `SOUL.md` mutation during incident.
- `echo '# guardrail-test' >> agents/hermes/pm/runtime/SOUL.md && git status --short --branch` (on issue branch with `.gitmodules` guardrail) → primary status did **not** show `m agents/hermes/pm/runtime`.
- `bash ops/smoketest/smoketest-hermes-runtime-hygiene.sh` → PASS with `ignore=dirty` expectation.
- `git checkout -- agents/hermes/pm/SOUL.md` + `git -C agents/hermes/pm/runtime checkout -- SOUL.md` + `mise run repo-health:json` → restored clean state (`worktree_dirty=false`, no submodule drift).

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/207
- PR: <url>
- Follow-up tickets: <none | #id, #id>

## Notes
- Runtime process can mutate tracked docs if guardrails are missing; keep primary checkout immutable and route doc-template maintenance only through issue branches/worktrees.
