---
title: '33GOD-59 — guarded retirement of redundant Orca registrations'
type: 'chore'
created: '2026-11-22'
status: 'in-progress'
route: 'oneshot'
review_loop_iteration: 0
---

<frozen-after-approval reason="human-owned intent">

## Intent

Retire only redundant direct Orca native CLI registrations, with a repeatable read-only preview and explicit apply. Preserve central bb-hook ingress, master/event maps, sibling settings, and synchronous Hindsight recall. Owner authorized Orca retirement and synchronous Hindsight on November 20; candidate only, no live activation or publication in this turn.

Where replacement cannot be proved, retain and report the direct hook rather than dropping behavior. In particular, Claude PreToolUse is a synchronous permission owner, not just a status sender; Antigravity PreToolUse is deliberately unbound. Retain unmapped events. Deliver source, isolated regression proof and exact commit for independent review.

</frozen-after-approval>

## Implementation Notes

- No intent gaps or irreversible actions in candidate preparation; live apply is explicitly deferred. Worktree starts at 0a142bf783fca32674cffed01169fdc70b4d9975 and strict-clean preflight passes. g33 preflight PASS (legacy enrollment WARN), route Bloodbank. CodeGraph sidecar absent; no fake indexing performed.
- Read master, sync, hub selection/environment, cutover and actual Orca scripts. Existing cutover is too broad: it removes unrelated concerns and does not prove replacement. A dedicated guarded retirement command is appropriate.
- Live inventory: 55 direct rows (Claude 16, Codex 9, Copilot 13, Gemini 4, Antigravity 5, Kimi 8). Source generated native configs call bb-hook, not direct publishers. Missing replacements and richer Orca shell behavior mean generic HTTP forwarding is insufficient.
- Preserve Orca-owned shell adapters behind the central dispatcher for transport, spool fallback and native metadata instead of reimplementing their protocol. Keep permission owners direct. Extend central Gemini/attention selection and pane attribution forwarding only. No master or generated-map edits needed for the safely redundant subset.
- Add a dedicated retirement utility and regression tests, plus ticket closeout inventory. Exact known command fingerprints prevent broad substring deletion of operator-owned commands. Apply must verify installed source parity, enabled replacement registration and Codex trust, with all-file preflight before writes.

## Candidate evidence and activation boundary

- The read-only live preview inventories **55** direct Orca rows: Claude 16, Codex 9, Copilot 13, Gemini 4, Antigravity 5, Kimi 8. It marks **48** for retirement only after matching the known command, native binding, selected `orca-status` handler, unconditional installed `bb-hook` command, executable adapter, and (for Codex) native loader trust. Seven remain direct: Claude `PreToolUse` (synchronous permission), `PostCompact`, `Elicitation`, `ElicitationResult`, `PostToolBatch`; Copilot `PreCompact`; Antigravity `PreToolUse` (unbound permission owner). The preview JSON reports every row's source path, native spelling, replacement native spelling, role and event type, action and reason; it contains no payload or credential values.
- Replacements already come from `services/agent-hooks/hooks.master.json` through `bb-hook`; `handlers.toml` selects the Orca adapter for mapped events. The only selection gaps closed here are Gemini and Copilot lowercase `notification`/`permissionRequest`. Retained unmatched events do **not** gain speculative master mappings. `hindsight-recall` stays `mode = "sync"`: turn/prompt context must be ready before continuation, and moving it async would change the owner's requested timing. There are no master or generated file edits.
- Direct-owner detection suppresses the central Orca adapter while a corresponding direct hook is installed; after guarded retirement the same central path invokes the existing Orca shell adapter with original payload and originating pane/launch environment. This preserves wire fields, endpoint refresh and offline spool, without double delivery in the transition. Permission decisions remain owned by the direct hook. A Codex native trust snapshot is captured before modifications and reconciled afterward. A rejected/partial apply keeps private backups; rerun preview before retry.
- Tests include a private loopback HTTP server and disconnected-spool probe for copies of the **six actual installed adapter scripts**, plus protected/unmapped events, no-op reapply, foreign sibling preservation, matcher scope, Codex trust, rollback and dispatch handoff. `mise run hooks:check` passes (SSOT/lock/generated artifacts in sync). Focused hub and migration suite: **121 passed, 8 subtests passed**; one existing pytest deprecation warning.
- **No live activation here.** The preview reports `installed_source_differs:concerns.py` because the review candidate has not been installed, so `--apply` refuses. After review and ordinary owning-repo publication, install/verify the reviewed hub/client/master via the established hook installation workflow and inspect the native loader/receipts before applying. From the published Bloodbank checkout, preview with `python3 services/hook-hub/orca_retirement.py > "$TMPDIR/33god-59-preview.json"`; inspect all retained rows and the blocker. Only when `activation_blocker` is `null` and the inventory still matches, run `python3 services/hook-hub/orca_retirement.py --apply --backup-dir "$TMPDIR/33god-59-orca-backup-$(date +%s)"`. This explicit command mutates only matched native CLI configs and Codex trust state, never unrelated profiles or the SSOT. No schedule or service restart is part of this candidate.
