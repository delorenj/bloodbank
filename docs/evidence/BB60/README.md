# BB-60: local-only checkpoints in the shared Krebs contract

Ticket: [BB-60](https://plane.delo.sh/33god/browse/BB-60/).
Reviewed correction: `f13982302a8e91851726bc1c6eaf3c02c120dac7`, based on
`0faf07ff5b01bf5e7b2eb08db7176d06392b9172`.

## Delivered behavior

`integrations/n8n-nodes-bloodbank/src/fleet.ts` now tells an implementation
worker to read the live description, latest comments and explicitly linked owner
policy before edits or commits. It selects an isolated execution lane first:

- An explicit local-only/no-push/no-merge/no-landing grant permits a local
  checkpoint, not publication. Keep its worktree, branch and commit, including
  when blocked. Independent review and handback do not grant publication.
- Ordinary landing requires an affirmative publication grant without a
  conflicting restriction. Standing merge approval alone is not that grant.
- Missing, ambiguous, incomplete, conflicting or unreadable authority means
  stop before edits and ask for input. Do not ask to lift an explicit restriction.
- Shared-checkout, human/hold, triage and independent-review safeguards remain.
  Paths-only commits and each other repository's own authority remain required.

The dispatch prompt is an instruction contract, not an automatic authorization
parser or a security sandbox. Titles, labels and dispatch payloads do not decide
authority. Krebs source, schedules and services were not changed.

## Independent source review and unchanged checks

Both independent source reviews report **PASS** for this exact correction,
without source findings. Neither report grants merge authority or full runtime
acceptance. Delivery is authorized by the original user's instruction, not by a
PM or a review verdict.

Evidence is retained under the active profile's local scratch directory:
`/home/delorenj/.hermes/profiles/bloodbank-pm/cache/scratch/`.

| Report | SHA-256 of `review.json` |
| --- | --- |
| `bb60-spec-final/review.json` | `2bb1fea4283314cd7ddf4fe307f0b6ed5f83dbaa1a47a9c0a3d9ae05a887206c` |
| `bb60-quality-final/review.json` | `c3ed23b1dac44edab22862fb3522cbff9704dcf75ce1b6fd44f126f922a817c4` |
| `bb60-rehearsal-spec/review.json` | `f0a15cd17476910c1375345e81b48b69b1ddd110f2f753b18e363bedca81158e` |

Delivery verification checked the three reviewed file hashes and Git blobs,
the frozen patch, candidate dist, unchanged check definitions, all 15 correction
receipt logs and all 16 independent QUALITY check logs. See
`bb60-delivery/verified-candidate.json` and `bb60-correction-receipts/manifest.json`.

| Check on the correction | Result |
| --- | --- |
| Unchanged `npm test` (includes build) | exit 0; 328 passed, 18 skipped, 0 failed |
| Unchanged `mise run smoketest:schemas` | exit 1; fail-fast at project-contracts |
| All 12 constituent schema/repo tasks run separately | 9 exit 0; 3 exit 1 |
| `smoketest:project-contracts` | exit 1: home-relative `~/code/x` raw-path rejection not raised |
| `smoketest:watch-contracts` | exit 1: `subject` enum member outside `HANDLE_KINDS` |
| `smoketest:plane-binding` | exit 1: clean export/worktree lacks untracked `.plane-project-id` |

Independent review reproduced the same three failures at the pre-BB60 base.
They are **still failures**, not waived checks. No schema, scaffold or gate was
weakened to turn them green. The parent owns separately scoped follow-up.
**There is no full-gate or all-acceptance-criteria PASS claim.**

## Actual disposable worker rehearsal

Run: `bb60-rehearsal/runs/rehearsal-f13982302a8e-v2/cases/` under that scratch root.
These are synthetic, private, documentation-only repositories, not the real
TOLT children. All repositories and checkpoint Git data stay local.

| Case | Local checkpoint | Outcome |
| --- | --- | --- |
| `local186-review` | `7ef88377a64d3d2ab620f1f03370cdff8a582de4` on `synthetic/local186-checkpoint`, at `local186-review/worktree-local186` | No remotes or observed forbidden attempts; main unchanged; blocked, not review |
| `local187-no-review` | `401a3aca5c4975dbac72aa3430833f77978bdf42` on `synthetic/retained-recovery`, at `local187-no-review/retained-recovery` | Recovery lane retained; no remotes or observed forbidden attempts; main unchanged; blocked, not Done |

Each checkpoint ran all 15 required commands: package test and build exited 0;
schema umbrella, project, watch and binding exited 1; the other nine components
exited 0. Rehearsal package results were 314 passed, 32 skipped, 0 failed (not
the source review's 328/18). Both workers honestly held for the baseline failures.
The ordinary-grant worker selected ordinary authority but stopped at the
rehearsal's overriding no-publication limit: decision-only, not ordinary landing.
Absent and conflicting authority workers held for input before edits or commits.

Independent rehearsal SPEC reports `PASS_BEHAVIORAL_CHECKPOINT_NO_PUBLICATION`.
Rehearsal QUALITY was not available at initial evidence capture and is not claimed
here. Initial local186 fixture reads occurred outside the bwrap wrapper, through
the exact private journal-only helper with no network/provider operations. This
limits containment proof; universal terminal containment is not claimed.
No successful review/Done handback, real Plane/bus E2E, ordinary publication,
real-child restoration or malicious-agent containment is certified.

## Delivery procedure and runtime boundary

Primary strict-clean preflight exited 1 because of unrelated journal and
`services/target-watch/` work. Those files were fingerprinted and left alone.
The existing clean BB60 worktree passed preflight; real
`git pull --no-rebase origin main` with autostash disabled exited 0 there.
Only these two evidence documents are added to the independently reviewed source.
Landing must preserve the shared checkout's dirty files and index, use a
fast-forward only when safe, push main, and verify remote ancestry and file blobs.

Canonical consumption requires rebuilding the **primary** package with Node
24.6.0, then calling the actual Krebs `loadBloodbank()` with no location override
and actual `dispatchCommand` for both review modes. The expected reviewed
`dist/fleet.js` SHA-256 is
`d74de4394dc3b09178e90cc2759002acb54f922fc0f8bf5f98ac701e83e201c5`.
At initial capture, canonical dist was still old; source approval alone did not
prove adoption. Final observed landing/build/loader results belong in the
runtime verification section added after execution. No restart or deployment is
needed or authorized for this file-loader proof.
