# agent-hooks

Per-CLI bridges that turn agent-CLI lifecycle events (Claude Code, GitHub
Copilot CLI, Codex CLI, OpenClaw, …) into CloudEvents 1.0 envelopes on
the Bloodbank NATS bus, conforming to the **Bloodbank Event Naming
Contract v1** at `bloodbank/docs/event-naming.md`.

Each supported CLI gets a thin entry point under its own subdirectory; all
shared behavior — envelope construction, NATS publishing, session/causation
state, contract validation — lives in [`core/`](./core).

## Layout

```
agent-hooks/
├── hooks.master.json         # ★ SSOT — canonical lifecycle catalog + per-agent bindings
├── hooks.mappings.lock.json  # resolution memory for ambiguous/divergent mappings
├── sync.py                   # propagate SSOT → every agent's native config + event map
├── core/
│   ├── envelope.py       # build_envelope() — enforces v1 contract on every call
│   ├── validate.py       # §2 regex, §5 tense, §6–§9 allowlists, §11 fields
│   ├── event_map.py      # load a publisher's hook→type map from the SSOT projection
│   ├── nats_publish.py   # stdlib-only NATS text-protocol PUB
│   └── session.py        # file-backed SessionState (correlation/causation chain)
├── claude/
│   ├── publish.py            # entry point for Claude Code hooks
│   ├── settings.hooks.json   # GENERATED — merge into ~/.claude/settings.json
│   └── event_map.generated.json  # GENERATED — hook-arg → (type, bucket)
├── copilot/
│   ├── publish.py            # entry point for Copilot CLI hooks
│   ├── hooks.json            # GENERATED — symlink into ~/.copilot/hooks/
│   └── event_map.generated.json  # GENERATED
├── codex/
│   ├── publish.py            # entry point for Codex CLI hooks
│   ├── hooks.json            # GENERATED — merge into ~/.codex/hooks.json
│   └── event_map.generated.json  # GENERATED
├── openclaw/
│   └── watch.py          # tail OpenClaw session logs → Bloodbank (watcher; no config)
└── README.md
```

## SSOT propagation (`hooks.master.json`)

Every agent CLI has its own hook-config dialect and its own native hook names,
but they all publish the **same** v1 CloudEvents lifecycle. To keep them from
drifting, the mapping lives in exactly one place — `hooks.master.json` — and is
propagated outward:

```
hooks.master.json ──sync.py──┬─► claude/settings.hooks.json   (+ event_map.generated.json)
       (SSOT)                 ├─► copilot/hooks.json           (+ event_map.generated.json)
                              └─► codex/hooks.json             (+ event_map.generated.json)
                                        ▲
                  hooks.mappings.lock.json (remembered resolutions)
```

`hooks.master.json` defines a **canonical lifecycle catalog** (`role` → v1
`type` + ordering bucket) and, per agent, the **bindings** from that agent's
native hook names to those lifecycle roles, plus the dialect detail (`runner`,
`payload` mode, `matcher`, timeouts) needed to render its native config.

Publishers no longer hand-maintain their `EVENT_MAP` / `HOOK_MAP`: each loads
`<agent>/event_map.generated.json` (merged over a small embedded fallback) via
`core.event_map.resolve_map`. **Edit the SSOT, never the generated files or the
publisher maps.**

```bash
mise run hooks:check     # read-only: drift + unresolved-ambiguity report (CI gate)
mise run hooks:sync      # regenerate every agent's native config + event map (repo only)
mise run deploy          # sync + INSTALL into each agent's live config (see below)
python3 sync.py --check --json   # machine-readable report
python3 sync.py --apply --resolve  # resolve open ambiguities interactively, then apply
```

### Deploy (install to live agent configs)

`mise run deploy` (= `sync.py --apply --install`) regenerates the artifacts and
then installs each agent's config into its live `live_target`:

| Agent | live_target | install method |
|-------|-------------|----------------|
| claude | `~/.claude/settings.json` | surgical JSON merge |
| copilot | `~/.copilot/hooks/bloodbank.json` | symlink → repo `copilot/hooks.json` |
| codex | `~/.codex/hooks.json` | surgical JSON merge |
| hermes | **fleet-wide** — every agent in `~/.hermes/agents-registry.yaml`: `<role_dir>/runtime/config.yaml` `hooks:` block + `runtime/shell-hooks-allowlist.json` | YAML merge + allowlist seed per agent |
| openclaw | — | skipped (`watcher` — log tailer, no hook-config) |

For hermes, `deploy` reads the fleet registry and installs into **every** provisioned agent (uninitialized runtimes are skipped; a missing `config.yaml` is created). Newly-provisioned agents appear in the registry, so the next `mise run deploy` covers them.

The merge is **inner-hook surgical**: it updates only the bloodbank publisher
hook (identified by the `<agent>/publish.py` substring) in place, preserving
every foreign hook (hindsight, git-checkpoint, notify, lint-skills, zellij, …),
their order, groups, and matchers. The live file is backed up
(`<file>.bak-<UTC>`) only when content actually changes; re-running is a no-op.
`deploy` does **not** manage non-bloodbank hooks — those stay exactly as the
operator left them.

### Ambiguous mappings & resolution memory

`sync.py` flags an **ambiguity** when:

- a catalog `type` is contract-illegal (bad domain/entity/action), or
- the same lifecycle `role` maps to *different* types across agents (e.g. one
  agent's post-tool hook → `tool.invoked`, another → `tool.completed`), or
- an emitted type has no schema under `bloodbank/schemas/`.

Each resolution is recorded in `hooks.mappings.lock.json` keyed by ambiguity id
(`role:<role>` / `type:<type>`). On the next sync the recorded decision is
applied automatically — so re-syncs, and even adding a brand-new agent whose
roles are already decided, are **seamless**. Unrecorded ambiguities block
`--apply` until resolved (`--resolve` prompts and appends to the lock).

Decisions baked into the current lock (resolved 2026-06-07):

| Ambiguity | Resolution | Why |
|-----------|------------|-----|
| `role:session_start` / `role:session_end` | `agent.session.started` / `agent.session.ended` | Session events live under the `agent` domain (legal 5-token type; supersedes `cli.session.*`). |
| `role:post_tool` | `agent.tool.completed` | The single post-tool hook fires post-execution and carries `outcome`. Claude moved off `tool.invoked`. |
| `role:subagent_stop` | `agent.invocation.completed` | Sub-agent runs are nested invocations; `agent.delegation.*` was not allowlisted. |

## Envelope contract

Every envelope conforms to `bloodbank/schemas/_common/cloudevent_base.v1.json`
and the v1 naming contract. `core.envelope.build_envelope()` raises
`ContractViolation` on any deviation. There is no legacy 3-token path and
no compat aliases — see "Hard rename, no aliases" in
`docs/event-naming.md` §15.

| Field             | Source                                                                          |
|-------------------|---------------------------------------------------------------------------------|
| `type`            | 5-token `bloodbank.v1.<domain>.<entity>.<action>` — validated against §2 regex  |
| `subject`         | derived: `bloodbank.<kind>.v1.<...>` — validated against §3                     |
| `kind`            | `event` (hooks emit only events; commands flow through a different path)        |
| `actor`           | per-CLI constant (`actor.cli`, `actor.provider`); §10                           |
| `correlationid`   | `SessionState.session_id` — stable for the CLI session                          |
| `causationid`     | `SessionState.last_event_id` — id of the previously published event             |
| `ordering_key`    | `<bucket>:<session_id>` where bucket ∈ {session, thread, invocation, …}          |
| `id`              | new UUID (or `session_id` on the first event of a chain)                        |
| `source` / `producer` / `service` | per-CLI constants                                                       |

The chain self-roots: on the session-start event the entry point passes
`event_id=session_id` so the first event's `id == correlationid ==
causationid`. Every subsequent event sets `causationid` to the previous
event's id, giving downstream consumers a linkable chain.

Provider/CLI/model identity goes into `actor.cli`, `actor.provider`,
`actor.model` — and is **banned from `type`** (§9).

## Transport

NATS-direct on `127.0.0.1:4222` via a ~40-line stdlib socket client. No
nats-py, no virtualenv, no Dapr sidecar on the publish path. Justified by
the fire-and-forget shape of CLI hooks. Override via:

| env                      | default     | purpose                                                                 |
|--------------------------|-------------|-------------------------------------------------------------------------|
| `BLOODBANK_NATS_HOST`    | `127.0.0.1` | NATS host                                                               |
| `BLOODBANK_NATS_PORT`    | `4222`      | NATS port                                                               |
| `BLOODBANK_NATS_TIMEOUT` | `3.0`       | Connect/publish timeout (s)                                             |
| `BLOODBANK_ENABLED`      | `true`      | `false` disables publishing entirely                                    |
| `BLOODBANK_DEBUG`        | unset       | `true` logs each publish to stderr                                      |
| `BLOODBANK_HOOK_VERBOSE` | unset       | when set, log `published <subject>` to stderr                           |
| `BLOODBANK_HOOK_STRICT`  | unset       | `1` exits non-zero on publish failure (default: fail open)              |
| `BLOODBANK_HOOK_VALIDATE`| unset       | `1` runs JSON Schema validation against the matching `bloodbank/schemas/` schema  |
| `BLOODBANK_SCHEMAS_DIR`  | unset       | overrides the schema-tree lookup; defaults to repo-local `bloodbank/schemas/`     |

## Claude Code

| Hook              | event-type arg     | v1 CloudEvents `type`                       | NATS subject                                       |
|-------------------|--------------------|---------------------------------------------|----------------------------------------------------|
| `SessionStart`    | `session-start`    | `bloodbank.v1.agent.session.started`          | `bloodbank.evt.v1.agent.session.started`             |
| `UserPromptSubmit`| `prompt-submitted` | `bloodbank.v1.conversation.turn.started`    | `bloodbank.evt.v1.conversation.turn.started`       |
| `PreToolUse`      | `tool-request`     | `bloodbank.v1.agent.tool.requested`     | `bloodbank.evt.v1.agent.tool.requested`        |
| `PostToolUse`     | `tool-action`      | `bloodbank.v1.agent.tool.completed`     | `bloodbank.evt.v1.agent.tool.completed`        |
| `SubagentStop`    | `subagent-stopped` | `bloodbank.v1.agent.invocation.completed`   | `bloodbank.evt.v1.agent.invocation.completed`      |
| `Stop`            | `session-end`      | `bloodbank.v1.agent.session.ended`            | `bloodbank.evt.v1.agent.session.ended`               |

Session state lives at `~/.claude/bloodbank-session.json` (single global
session, cwd-independent). On `session-end` it's archived to
`~/.claude/bloodbank-sessions/<session_id>.json`. Per-event `working_directory`
and `git_branch` are captured live from cwd at event time.

`actor` constant: `cli=claude`, `provider=anthropic`, `model=null`.

Install: registered in user-global `~/.claude/settings.json`. Fires for
**every** Claude Code session regardless of launch directory.

## Copilot CLI

Reference: <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-hooks>

| Copilot hook         | v1 CloudEvents `type`                       | NATS subject                                       |
|----------------------|---------------------------------------------|----------------------------------------------------|
| `sessionStart`       | `bloodbank.v1.agent.session.started`          | `bloodbank.evt.v1.agent.session.started`             |
| `sessionEnd`         | `bloodbank.v1.agent.session.ended`            | `bloodbank.evt.v1.agent.session.ended`               |
| `userPromptSubmitted`| `bloodbank.v1.conversation.turn.started`    | `bloodbank.evt.v1.conversation.turn.started`       |
| `preToolUse`         | `bloodbank.v1.agent.tool.requested`     | `bloodbank.evt.v1.agent.tool.requested`        |
| `postToolUse`        | `bloodbank.v1.agent.tool.completed`     | `bloodbank.evt.v1.agent.tool.completed`        |
| `errorOccurred`      | `bloodbank.v1.agent.invocation.failed`      | `bloodbank.evt.v1.agent.invocation.failed`         |
| `agentStop`          | `bloodbank.v1.agent.invocation.completed`   | `bloodbank.evt.v1.agent.invocation.completed`      |

Session state lives at `~/.copilot/bloodbank-session.json`.

`actor` constant: `cli=copilot`, `provider=github_copilot`, `model=null`.

Install:

```bash
mkdir -p ~/.copilot/hooks
ln -snf "$(pwd)/copilot/hooks.json" ~/.copilot/hooks/bloodbank.json
```

Copilot CLI picks up `~/.copilot/hooks/*.json` on next launch.

## Codex CLI

Codex hooks are read from `~/.codex/hooks.json`. The tracked
`codex/hooks.json` is a mergeable example for Bloodbank-specific hook
entries; don't blindly replace a live config that also contains Hindsight
or local operator hooks.

| Codex hook        | v1 CloudEvents `type`                       | NATS subject                                       |
|-------------------|---------------------------------------------|----------------------------------------------------|
| `SessionStart`    | `bloodbank.v1.agent.session.started`          | `bloodbank.evt.v1.agent.session.started`             |
| `Stop`            | `bloodbank.v1.agent.session.ended`            | `bloodbank.evt.v1.agent.session.ended`               |
| `UserPromptSubmit`| `bloodbank.v1.conversation.turn.started`    | `bloodbank.evt.v1.conversation.turn.started`       |
| `PreToolUse`      | `bloodbank.v1.agent.tool.requested`         | `bloodbank.evt.v1.agent.tool.requested`            |
| `PostToolUse`     | `bloodbank.v1.agent.tool.completed`         | `bloodbank.evt.v1.agent.tool.completed`            |
| `SubagentStart`   | `bloodbank.v1.agent.invocation.started`     | `bloodbank.evt.v1.agent.invocation.started`        |
| `SubagentStop`    | `bloodbank.v1.agent.invocation.completed`   | `bloodbank.evt.v1.agent.invocation.completed`      |

Session state lives at `~/.codex/bloodbank-session.json`.

`actor` constant: `cli=codex`, `provider=openai`, `model=<payload or null>`.

Install by merging the entries from `codex/hooks.json` into the existing
`~/.codex/hooks.json`. For a fresh Codex profile with no existing hooks:

```bash
cp "$(pwd)/codex/hooks.json" ~/.codex/hooks.json
```

## Hermes

Hermes-agent fires **shell hooks** declared in the `hooks:` block of its
`config.yaml` (`agent/shell_hooks.py`); each command runs `shell=False` (shlex
argv) with the payload piped as JSON on stdin, gated by `shell-hooks-allowlist.json`.

| Hermes event | v1 CloudEvents `type` |
|--------------|------------------------|
| `on_session_start` | `bloodbank.v1.agent.session.started` |
| `on_session_end`   | `bloodbank.v1.agent.session.ended` |
| `pre_tool_call`    | `bloodbank.v1.agent.tool.requested` |
| `post_tool_call`   | `bloodbank.v1.agent.tool.completed` |
| `subagent_stop`    | `bloodbank.v1.agent.invocation.completed` |

`actor.cli=hermes`, `actor.agent_id=bloodbank.agent.hermes`. Each agent sets
`HERMES_HOME=<role_dir>/runtime`, so `mise run deploy` is **fleet-wide**: it reads
`~/.hermes/agents-registry.yaml` and, for every agent, merges the `hooks:` block
into `<role_dir>/runtime/config.yaml` and pre-approves the commands in
`runtime/shell-hooks-allowlist.json`. Verify any agent's live path with
`<role_dir>/hermes hooks test on_session_start` (fires the real hook → publishes
`agent.session.started`). Hermes has no clean user-prompt event, so
`conversation.turn.started` is not mapped.

## OpenClaw

`openclaw/watch.py` tails OpenClaw session logs and trajectory files,
synthesizing the same v1 event shapes (`agent.session.*`,
`conversation.turn.started`, `agent.tool.*`, `agent.invocation.*`).
`actor.cli=openclaw`, `actor.agent_id=bloodbank.agent.openclaw.<per-session-agent-id>`.

## Verify

With NATS up (`mise run up` from the bloodbank root):

```bash
# Single-hook smoke
echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' \
  | python3 services/agent-hooks/claude/publish.py tool-action

echo '{"probe":"sessionStart"}' \
  | BLOODBANK_HOOK_VERBOSE=1 python3 services/agent-hooks/copilot/publish.py sessionStart

echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' \
  | BLOODBANK_HOOK_VERBOSE=1 python3 services/agent-hooks/codex/publish.py PreToolUse

# Tail the catch-all toaster, then trigger a hook in another shell.
docker logs -f bloodbank-event-toaster
```

End-to-end, durable audit/inspection is handled by Candystore, which consumes
`bloodbank.evt.v1.>` through its own Dapr sidecar.

## Contract verifier

```bash
mise run smoketest:bloodbank-naming
```

Pure stdlib-only verifier — no Docker/NATS required. Runs every event in the
§14 canonical sequence through `cli/bb.py verify-envelope` for Claude,
Copilot, and Codex actors plus a set of negative-case probes.

## Adding a new CLI

1. Add an entry under `agents.<cli>` in **`hooks.master.json`**: pick a
   `dialect` (`claude_settings` | `copilot` | `codex` | `watcher`), set
   `runner` (use `{service_dir}` for an absolute publisher path), `actor`,
   `config_target` / `event_map_target`, and the `bindings` mapping each
   native hook name → a lifecycle `role` + canonical `lifecycle` key.
2. `mise run hooks:check`. If a binding's role is already decided in the lock
   (e.g. `post_tool`) it resolves automatically; any *new* ambiguity is
   surfaced — run `python3 sync.py --apply --resolve` to record the decision.
3. `mise run hooks:sync` to generate `<cli>/hooks.json` (or the settings
   fragment) and `<cli>/event_map.generated.json`.
4. Create `agent-hooks/<cli>/publish.py` that:
   - imports `core.envelope.build_envelope`, `core.nats_publish.publish`,
     `core.session.SessionState`, and `core.event_map.resolve_map`,
   - picks a stable on-disk `SessionState` path (`~/.<cli>/bloodbank-session.json`),
   - sources its hook→type map via `resolve_map(Path(__file__).parent, _DEFAULT_MAP)`
     (the `_DEFAULT_MAP` is a fallback; the SSOT projection wins),
   - declares the same per-CLI `actor` constant as the SSOT entry,
   - publishes via `nats_publish(...)` and updates `session.record_event(envelope["id"])` on success.
5. Document the hook table here. The `smoketest:agent-hooks-ssot` task will
   validate every binding's envelope against the contract + schema.
6. If the CLI introduces a new provider name, add it to the banned-token
   list in `docs/event-naming.md` §9 and `core.validate.BANNED_TOKENS`.

Anti-patterns:

- Don't hand-edit the generated artifacts (`*/hooks.json`,
  `claude/settings.hooks.json`, `*/event_map.generated.json`) or a publisher's
  `EVENT_MAP` / `HOOK_MAP`. Edit `hooks.master.json` and run
  `mise run hooks:sync`. `mise run hooks:check` is the CI gate.
- Don't widen the runtime deps past stdlib — that's why every publisher is
  single-file, drop-in usable from any agent CLI hook.
- Don't reintroduce a Dapr sidecar on the publish path. The bus is the
  contract; sidecars are an implementation detail of subscribers.
- Don't build envelopes by hand outside `core.envelope.build_envelope()`.
  Contract enforcement (regex, tense, banned tokens) runs there.
- Don't put provider, CLI, or model names in `type`. They live in `actor`.
