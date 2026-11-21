# BB-60 execution record

Ticket: [BB-60](https://plane.delo.sh/33god/browse/BB-60/).
Evidence and delivery guide: [docs/evidence/BB60/README.md](../docs/evidence/BB60/README.md).
Public evidence: [complete sanitized package](https://s3.delo.sh/projects/bloodbank/2026-11-21/bb60-local-checkpoints/index.md)
and [actual command exits/full logs](https://s3.delo.sh/projects/bloodbank/2026-11-21/bb60-local-checkpoints/checks.md).
The MinIO publisher returned `ok: true` after anonymous SHA-256 verification of
every uploaded object. Original review hashes and publication provenance are
included; local checkpoint Git data, source repositories, credentials, dependencies
and native full transcripts are not exported.

## Scope

Support explicitly authorized local-only checkpoints in Bloodbank's shared
Krebs delegation prompt. Resolve live authority and isolation before edits;
require an affirmative grant for ordinary publication; preserve shared-checkout,
hold, human, triage, review and handback safeguards. No real child implementation,
other-repository source change, schedule, bus/board write or service restart.

## Exact reviewed correction

`f13982302a8e91851726bc1c6eaf3c02c120dac7` has independent source SPEC and QUALITY
PASS reports. Only `src/fleet.ts`, `test/fleet-node.test.cjs` and
`test/krebs-local-checkpoint.test.cjs` under `integrations/n8n-nodes-bloodbank/`
changed. Delivery verifies their frozen hashes; documentation additions do not
alter the reviewed source. The original user's authorization governs landing;
a source review is not merge authorization.

## Criteria and evidence

- Live-authority and route-selection instructions, default-deny publication,
  local checkpoint retention and ordinary-grant behavior are source-reviewed.
- Actual Krebs dispatch/triage/review entrypoints were probed. Synthetic prompt
  characterization is not substituted for actual-worker behavior proof.
- Five disposable workers exercised two local checkpoint lanes, ordinary grant
  selection and absent/conflicting authority. Both local checkpoints are
  retained locally; their Git data must never be published or cleaned up.
- Independent rehearsal SPEC reports
  `PASS_BEHAVIORAL_CHECKPOINT_NO_PUBLICATION`; independent rehearsal QUALITY
  reports `PASS_SCOPED_CHECKPOINT_QUALITY_NO_PUBLICATION` for both exact
  checkpoints. QUALITY `review.json` SHA-256:
  `052eb702d944b7c37cebe3da44aa23fc9c7372d8878195eeeeef962a25c711bc`.
  Both local workers held blocked after required checks; neither successful
  review-required handback nor no-review Done handback was exercised. Scoped
  independent review does not fill that remaining acceptance gap.
- QUALITY expands SPEC's containment exception beyond initial reads: local186
  host-side `show`/`comments` (tool/result 9170/9172), `decision` (9217/9218),
  and blocked-hold `label-add`/`comment` (9285/9286) ran outside the exec/bwrap
  wrapper. SPEC's whole-command substring classification missed `&&`-chained
  host calls. All five operations exited 0 through the frozen private
  journal-only helper; no real network/provider operation or board write was
  observed. Universal terminal containment is not proven. Preserve the original
  SPEC and QUALITY reports; this evidence correction needs no new checkpoint task.
- Required package/repo checks ran unchanged. Source `npm test`: exit 0,
  328 passed / 18 skipped / 0 failed. All 12 repo components ran separately;
  nine passed, project/watch/binding failed, and the aggregate exited 1.
  Independent review reproduced these three pre-existing failures. They remain
  red, are not waived, and are routed to the parent for separate owner follow-up.
- Owning-repository landing and canonical default Krebs artifact proof are
  recorded only after the real pull, push, primary build and loader probe.

## Local delivery receipts

`/home/delorenj/.hermes/profiles/bloodbank-pm/cache/scratch/bb60-delivery/`
contains exact commands, outputs, exits, review fingerprints and primary dirty
file preservation snapshots. No unrelated files may be staged or committed.

No full-gate or all-criteria PASS, child eligibility restoration, production E2E,
ordinary-publication rehearsal or universal sandbox-containment claim is made.
Board disposition remains the parent's responsibility.

## Observed source landing and canonical adoption

Evidence commit `3ccd05fe952ef3d77b5cb08180aa25bc410a2944` reached remote main
by safe fast-forward and push after the clean BB60 worktree's real
`git pull --no-rebase origin main` (exit 0). Remote main, reviewed-correction
ancestry and the exact three source/test blobs were verified. Node v24.6.0
`npm run build` in the actual primary package exited 0.

Default Krebs `loadBloodbank()` (no arguments or directory override) and actual
review/no-review `dispatchCommand` construction exited 0 against the canonical
artifact. Its `dist/fleet.js` SHA-256 is
`d74de4394dc3b09178e90cc2759002acb54f922fc0f8bf5f98ac701e83e201c5`, equal
to the candidate, a fresh compile and the independent QUALITY scratch build.
Both dispatch variants carry the new authority contract, not the old
unconditional landing instruction. Triage/review safeguards were also checked.

The nine pre-existing dirty paths, their contents/modes and the empty index
were unchanged after push and build; build introduced no Git-visible surprises.
See the linked evidence guide and `bb60-delivery/canonical-runtime.json`.
This proves offline default-loader adoption, not a running tick or published
command. No restart, deploy, board/bus write or schedule action was performed.
The remaining known failures and rehearsal limits above are not waived.
