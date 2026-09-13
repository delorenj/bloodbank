# Hook migration native acceptance — 2026-09-13

These checks ran against the installed hook configurations, live hook hub, and
Candystore on 2026-09-13. The evidence records identifiers and counts only.

## Installed wiring

`python3 services/agent-hooks/sync.py --check` reports the master, lock, and
generated artifacts in sync. `hook_healthcheck.py --json --check` exits zero
with status `healthy`. The installed inventory finds all eight supported CLIs,
including Copilot and Gemini when run with the user service's restricted PATH.
Gemini is installed in mise's Node 22 environment and reports version 0.41.2.

Codex's native `hooks/list` verifies ten enabled, trusted hub commands in each
of the default and Orca runtime homes. The retained MCP helper reaper remains
trusted. Hermes has twelve managed bindings in each of 41 discovered configs.
OpenClaw remains explicitly `unsupported`; its proposed bindings are not
accepted native coverage.

## Actual Codex and Claude command execution

Both native CLIs ran one harmless Bash `printf` command in separate disposable
`/tmp` directories and created no files there. Codex ran with its read-only
sandbox and ephemeral session mode; Claude ran with only Bash available and
an allowlist for the `printf` command, without session persistence.

| CLI | Native session | Duration | Command result |
| --- | --- | --- | --- |
| Codex | `01a09bd1-a110-7081-bb99-94c5b8df1d2f` | 19.0 s | exit 0 |
| Claude | `37a37ec6-37a9-48a4-a57c-ba1a54e22117` | 5.9 s | success, 2 model turns |

Each session produced exactly one hub invocation and one successful publisher
execution for each of these six native hooks. Every event ID below occurs
exactly once in Candystore.

| Native hook | Codex event ID | Claude event ID |
| --- | --- | --- |
| SessionStart | `01a09bd1-a110-7081-bb99-94c5b8df1d2f` | `37a37ec6-37a9-48a4-a57c-ba1a54e22117` |
| UserPromptSubmit | `28f7acb9-1ede-4634-a434-c7a83bb707e6` | `19deab67-8139-48d1-aa6a-5b127d249ec5` |
| PreToolUse | `c9203c84-94f6-4e15-9a9d-ce6dee590869` | `1594b250-ef07-4d75-b046-4b55841650f2` |
| PostToolUse | `f0ab6cbb-2541-42d2-8eda-0327ff6cd878` | `2972f79c-8805-4bfe-856f-3d51a3799397` |
| Stop | `f38757ad-3423-41d8-9f9d-45fc560e7f7d` | `5cb2b250-43ef-4673-bd0b-1dbb7155fb21` |
| SessionEnd | `ec725452-b21a-4dcf-9ab0-0d635eb7ce6a` | `3696e1d4-4656-427b-9c83-aa4b52881ded` |

Codex's Bash request and completion share tool ID
`805dac894bcf76960172279637960167`; Claude's share
`toolu_01A1GuND2iQY6gkqkkbfWUaC`. Prompt and Stop share the native Codex turn ID
`01a09bd1-a1e2-7692-bf30-db901cc594be` and the persisted Claude fallback turn ID
`37a37ec6-37a9-48a4-a57c-ba1a54e22117:turn:1`, respectively. Stop publishes
`bloodbank.conversation.turn.completed`; SessionEnd publishes
`bloodbank.agent.session.ended`.

The Codex PostToolUse receipt is `c40bb29e-951f-586f-8748-45b5d80215fd`.
The Claude PostToolUse receipt is `fab96c40-41e7-509a-933f-170bc8f50a35`.

These runs exposed notebook handlers incorrectly failing on non-project
directories. Commit `4195124` restricts notebook handling to its supported
Claude scope and explicitly skips non-repositories. A Claude recall also failed
in the original run; after the non-repository bank fallback correction in
`eeac48a`, two scoped hub probes completed successfully, including a full recall
in 7,992 ms. Historical failed receipts remain visible.

## Actual OpenCode loader and lifecycle

The installed OpenCode loader originally stopped at the canonical Momo
definition's comma-separated Claude tools field. The OpenCode projector in the
canonical agent repository now preserves shared definitions and renders that
allowlist as native deny-by-default permissions plus its seven allowed tools.
Compatible definitions remain linked to their canonical sources.

The source/projector commit is `9659315` in `~/.agents`; the native configuration
link commit is `baedc4c` in `~/.config/opencode`. Run
`python3 ~/.agents/scripts/project-opencode-agents.py --apply` after changing a
canonical definition that needs adaptation; `--check` detects stale output.
Two focused tests cover permission equivalence, unchanged prompts, linked
native definitions, removal, and repeat installation.

`opencode debug config` now succeeds with all eight definitions and includes
the installed `bloodbank-hook-hub.js` plugin. A separate native headless server
created and deleted only test session `ses_f64221a1cffelDOXdaFGK8bXBh`, without a
model turn or files in its disposable directory. This exercised the actual
OpenCode plugin loader and event callbacks.

| Native event | Hub invocation ID | Published event ID | Candystore count |
| --- | --- | --- | --- |
| session.created | `18af18ab-3aab-488d-b8af-3148a8f1f163` | `ea178fc4-36f9-5967-b1b1-6a2a3f4253ad` | 1 |
| session.deleted | `96048315-ff98-4483-9c45-1db9dedadaf2` | `fc697d22-8309-4c90-a4c6-e980041d20f5` | 1 |

Gemini's native `--list-sessions` command successfully loaded its installed
settings with no settings errors and no authentication or provider changes.
Gemini hook execution is covered by the adapter replay below.

## Adapter replays across all supported CLIs

Replay batch `4098ad80-a1e8-4464-9077-71506e762aa9` sent one harmless tool payload
through each installed `bb-hook` entrypoint. The command text was payload
metadata and was not executed. These are adapter replays; the actual native
CLI checks are documented above. Each receipt and publisher execution succeeded,
and each published ID occurs exactly once in Candystore.

| CLI | Native event | Hub invocation ID | Published event ID |
| --- | --- | --- | --- |
| Claude | PreToolUse | `fb1a83e8-533b-5be9-92f0-919ae16d1910` | `3b21f1d7-b66c-4651-ae83-737808d92226` |
| Copilot | preToolUse | `f910c9bc-3a77-5155-bd6b-74087eca9b25` | `8e850dcf-fe17-4fd2-bf57-da13fdd75aad` |
| Codex | PreToolUse | `2c6586ef-8d6c-585b-bd5e-09e46a32e1d3` | `975b4f99-baf9-450e-8550-3045e7bc1963` |
| Hermes | pre_tool_call | `a4b31e09-474f-5be0-8796-9882b02e0c55` | `40c53a4d-3c4a-4a77-8980-cecab00f7460` |
| Antigravity | PostToolUse | `e5af4e8c-2dae-5e71-88a6-c6476ebb6ec9` | `d0996b5f-754a-4fe8-80a2-7462493623e7` |
| Gemini | BeforeTool | `69c11649-2476-5341-9e66-710eae2185dd` | `06b0981b-c723-4ecd-856c-2f1ab2073f6a` |
| Kimi | PreToolUse | `460cbca2-e729-5466-bf7a-1e6720be276a` | `ca67eaed-c975-4137-9477-84445ced7d73` |
| OpenCode | tool.execute.before | `0e085861-271f-536a-bc0a-e0f166d8de5d` | `badc0d9a-aac9-42c8-98b9-8ab8e35d5213` |

Antigravity uses its existing PostToolUse binding; its permission-bearing
pre-tool surface is intentionally outside this passive adapter. No GUI or
active pane was started or modified for this replay.

Evidence was read from the local hub's `/v1/hooks/invocations` API and
Candystore's `/events` API, filtering by the exact session and publication IDs.
