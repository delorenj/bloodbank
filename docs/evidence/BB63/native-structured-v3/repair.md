# BB-63 structured native outcome repair — v3

Source-only candidate for independent final SPEC/QUALITY review. Baseline:
`029c571b687c5790945002796643a31e95b0e33f`; branch
`fix/bb63-native-turn-outcomes`. The candidate is the commit containing this
report; the exact five source-file SHA256 values are in [manifest.json](manifest.json).
No independent acceptance of this revision is claimed here.

## Correction

- Removed `returned_provider_rejection` and its unused regex/constants. Root
  finalizer booleans, not provider-shaped text, determine native capture outcome.
- Explicit failed captures receive `hermes_turn_error` / `native.failed` without
  inferring a provider-specific reason from prose. Owning turns that skip their
  root finalizer still fail closed, including pinned retry-exhaustion returns.
- Legacy interrupt-text fallbacks cannot demote native capture, including during
  a drain without a real interrupt. Actual runner interruption and later platform
  failure/cancellation retain authority to exclude the answer.
- Regressions cover exact `HTTP429:no eligible route; ...` genuine success,
  quoted HTTP/API-error prose, long multiline output with both successful and
  failed native flags, and missing-finalizer raw/sanitized/failed-turn replies.
  They assert terminal facts, ACKs and twice-restarted immutable outbox replay.
  Synthetic provider failures now explicitly use `failed=True`, not a successful
  finalizer plus heuristic guessing. README documents the structured contract.

## Real execution

Run from the candidate's `services/hermes-gateway` directory. No owning tests,
pytest selection/configuration, or dependency declarations were relaxed.

| Gate | Command | Actual result | Output |
| --- | --- | --- | --- |
| Expected red before source fix | `env -u PYTHONPATH BB30_HERMES_RELEASE=/home/delorenj/.hermes/hermes-agent uv run --python 3.13 --extra test --no-cache pytest -q --tb=short tests/test_native_outcome.py -k flags_control --basetemp="$TMPDIR/bb63-v3-red-native"` | exit 1; 6 failed, 12 passed, 35 deselected | [expected-red.txt](expected-red.txt) |
| Intermediate native suite | `env -u PYTHONPATH BB30_HERMES_RELEASE=/home/delorenj/.hermes/hermes-agent uv run --python 3.13 --extra test --no-cache pytest -q --tb=short tests/test_native_outcome.py --basetemp="$TMPDIR/bb63-v3-native-green"` | exit 0; 60 passed in 20.80s | Scratch `bb63-v3-native-green.log` |
| Full default | `env -u PYTHONPATH -u BB30_HERMES_RELEASE uv run --python 3.13 --extra test --no-cache pytest -q --tb=short` | exit 0; 284 passed, 59 skipped in 22.18s | [default.txt](default.txt) |
| Full pinned | `env -u PYTHONPATH BB30_HERMES_RELEASE=/home/delorenj/.hermes/hermes-agent uv run --python 3.13 --extra test --no-cache pytest -q --tb=short` | exit 0; 343 passed in 50.65s | [pinned.txt](pinned.txt) |
| Whole native processing/sanitizer probe | `env -u PYTHONPATH uv run --python 3.13 --extra test --no-cache python "$TMPDIR/bb63-v3-native-platform-probe.py"` | exit 0; 20 scenarios, source hashes checked before/after | [native-platform-results.json](native-platform-results.json) |

The expected-red text removes only six trailing-whitespace runs from pytest's
blank `E` lines so the evidence passes the repository diff check; the exact raw
log remains in active scratch `bb63-v3-red-native.log` (SHA256
`39bb40fe5e32a540201a845c8f281e454747c34d625bb1b95f9a4fe3e84316f1`).
All other retained output bytes are unchanged.

The final full gates include the added drain/quoted-interrupt regression, which
was added after the intermediate 60-test native run. Default skips are native
proofs requiring the release variable; the pinned gate runs the entire suite.
The release fixture reads native source at
`0408fec7a153e6c32c064acd2b8053917f1525f1`, not its checkout HEAD.
`git diff --check` passed. Earlier v2 evidence reported a scheduling transient;
this revision's fresh full runs both passed first try, without claiming the
historical transient's cause was resolved.

The private probe reuses the v2 complete pinned finalizer, full native platform
processing method and full gateway sanitizer, with expectation changes limited
to structured statuses and this revision's corrected behavior. It executes the
pinned retry-exhaustion return expression, not a live provider. A private tool
marker really executes, but is only an offline fixture, not a native live
provider/tool-result join. Synthetic identities/text only appear in retained
JSON; no live prompts, secrets or session databases were accessed.

Reproduction programs remain under the active profile scratch:
`/home/delorenj/.hermes/profiles/bloodbank-pm/cache/scratch/bb63-v3-native-platform-probe.py`
and its unchanged dependency `bb63_quality_probe.py` (dependency SHA256
`d8ac9976ae80870f47c87c3ad403703734071f53b24c0d96be1deb5463551bbe`).
The retained result is from `bb63-v3-native-platform-probe/db85faf45a0741489ce99be4349d3f23/results.json`.
The first probe attempt had an empty path inventory due to a service-relative
Git pathspec. It was rejected as fingerprint evidence, fixed to the worktree
root with an explicit five-file assertion, and rerun successfully; only that
second, hash-verified result is retained here.

## Open gates and boundaries

Independent final SPEC then QUALITY review remain required. This is local,
unpushed, unmerged source delivery, not installation, rollout, route approval,
transcript-preserving recovery, live same-profile tool/provider-ledger proof,
or downstream audit delivery. The native pinned display sanitizer still rewrites
some legitimate HTTP/API-error prose to warnings; the durable native success
and exact answer remain correct, and that upstream display behavior is unchanged.
Parent/owners coordinate the separate live blocker. BB-63 total ACs and SKRILL-49
release are not claimed. No Plane state, holds, other repositories, installed
runtime, credentials, accounts, billing, access, live session DBs, service
restarts or primary source WIP were changed.
