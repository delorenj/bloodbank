# Bloodbank Hook Hub — implementation plan

> Approved 2026-08-26. Companion diagram: `hook-hub-topology.excalidraw`
> (open at https://draw.delo.sh via drag-drop or File -> Open).

## Status

| phase | state |
|---|---|
| 0 — hub daemon, `bb-hook` client, registry, systemd units, tests | **DONE** (`a7f72a3`), installed and socket-activated |
| 1 — `Notification` cutover | not started — needs a live-config greenlight |
| 2–6 | not started |

**Concurrent work landed mid-flight.** `3f680d5 feat(agent-hooks): fan out exact
attention alerts` (2026-08-26 08:50, another agent on this repo) independently
implemented two things this plan called for: the `publish: false` binding flag,
and moving deckard off its own hook — `core/publisher.py::_fanout_alert` now
publishes `deckard.evt.attention` from the agent-hooks publisher, using
`ZELLIJ_PANE_ID`/`ZELLIJ_SESSION_NAME` from the hook environment.

Two consequences:

- The dispatch⊃publish decision below is now *implemented*, not just proposed —
  `role: "attention"` bindings carry `publish: false` and never reach the bus.
- **Phase 1 shrinks.** Deckard no longer needs a hub handler; wiring one would
  fire the amber key twice. Phase 1 is now `claude-notify` + `zellij-notify`
  only, and `handlers.toml` carries an explicit warning against a deckard row.

Phase 0 was deliberately scoped to new files only, so it could land without
conflicting with that concurrent work. Phases 1+ touch `hooks.master.json` and
`sync.py`, which are exactly the files the other agent is editing — coordinate
before starting them.

## Verified findings from deep review (2026-08-26)

A five-agent review pass ran against this plan. Claims below were re-verified
against the live tree; two were wrong and are corrected here.

### Landmines for Phase 1+ (confirmed, must be handled)

**L1 — swapping the runner orphans the old publisher hook.** `_merge_hooks()`
identifies its own entry by command **substring** via `_publisher_markers()`
(`= [publisher] + legacy_publishers + ["<name>/publish.py"]`). Change `runner`
to `bb-hook` *and* drop `bloodbank/publish.py` from the marker list, and the
installer stops recognizing the live entry: it appends the re-trigger and leaves
the old publisher firing forever.

Reviewer called this unbounded growth. **It is not** — `_merge_hooks` appends
only when no live hook matches, so it stabilizes at exactly one orphan. That is
worse than it sounds: a stable orphan looks like a clean install while every
event publishes twice. Fix is data-only — keep the retired path in
`legacy_publishers`. Pinned by `tests/test_runner_swap_idempotency.py`.

**L2 — two concerns have their own installers that will re-add native entries.**
`~/.claude/settings.json` has **four** independent writers: bloodbank `sync.py`,
Orca, pjangler's `project-hooks.py` (owner-prefix `PJ_HOOK_OWNER=…`), and
`deckard install-hooks`. Absorb project-notebook or deckard into the hub and the
next `pj` run or `deckard install-hooks` silently restores the native entry →
double-fire. `test_sync_coexistence.py` currently *asserts* the project-notebook
entries survive a sync.

Consequence: **project-notebook comes out of Phase 1–4 scope.** Absorb it only
together with deleting its projector, or teach the projector about the hub.
Deckard is already resolved for a different reason (see Status above).

**L3 — `clients/*.py` call `os.getcwd()` inside `shape_data`.** Three sites
(`claude.py`, `codex.py`, `hermes.py`). Harmless today because the hub does not
publish, but the moment Phase 5 moves publishing into the daemon they resolve to
the *daemon's* cwd and `data.working_directory` becomes wrong-but-plausible.
Phase 5 must add a `cwd` attribute on `ClientAdapter` and set
`WorkingDirectory=/` on the unit so a regression is loud rather than subtle.

### Corrections to the review

- **`hooks:uninstall` exists.** Reviewer asserted it does not and that rollback
  was unbuilt. It is defined in `task.sh` and lists in `mise tasks` as "Remove
  per-user 33GOD hook injections." The rollback section stands.
- **L1 is not unbounded** — see above.

### Facts worth acting on separately

- **`hindsight-retain` does almost nothing today.** Its `hindsight memory retain`
  call is commented out (`hindsight-retain.sh:92`); the hook only appends to
  `~/.agents/journal/sessions/<id>.jsonl`. Migrating it is near-free, but decide
  whether it should be doing its actual job first.
- **One recall costs ~4.1 s** and the hook makes one call per bank (primary +
  globals + linked). Claude caps it at `timeout=12`, so on multi-bank repos it is
  plausibly being killed mid-flight and injecting nothing. Independent of this
  refactor.
- **Scope is 6 CLIs, but 7 have hooks.** Kimi Code (`~/.kimi-code/config.toml`)
  and OpenCode (`~/.config/opencode/plugins/*.ts`) carry hook wiring and are not
  in `hooks.master.json`. Out of scope here; worth knowing the hub does not cover
  them.

## Context

You maintain agent hooks by **fan-out**: every behavioral concern gets hand-wired
into every agent CLI's native hook config, in that CLI's own dialect. The cost is
now measurable:

- `~/.claude/settings.json` carries **40 hook wiring entries across 13 events**,
  from **~11 distinct concerns**.
- `hooks.master.json` covers **6 CLIs** (claude, copilot, codex, hermes,
  antigravity, openclaw), each with its own dialect renderer in `sync.py`.
- Adding one concern today = up to **6 edits** across 5 different config shapes.

Your insight is correct, and stronger than you framed it: **the normalization work
is already done.** `bloodbank/services/agent-hooks/hooks.master.json` already maps
every CLI's native events onto one shared **lifecycle-role** vocabulary
(`session_start`, `prompt_submit`, `pre_tool`, `post_tool`, `subagent_stop`,
`session_end`, `invocation_start`, `invocation_failed`), and every CLI already
publishes a normalized CloudEvent per role. The re-triggers exist. What never
moved to the normalized side is the **behavior** — it stayed fanned out.

Evidence this is the natural shape: there are currently **four** independent
re-inventions of "one ingress for N CLIs" on this machine —

| bus | transport | wirings |
|---|---|---|
| Bloodbank | NATS CloudEvents | `publish.py --client X --hook Y` (SSOT-generated) |
| Orca | local HTTP `:port/hook/<cli>` | 6 per-CLI shims |
| nlp (nanoleaf) | Redis | `nlp hook <event>` × 11 |
| deckard | NATS `deckard.evt.attention` | 3, already carries `ZELLIJ_PANE_ID` |

Three of the four independently concluded "normalize the event name, dispatch
centrally." Bloodbank is the one with a locked schema contract, a live broker, and
durable consumers — so it wins.

**Intended outcome:** one registry file is the only thing you edit. Per-CLI native
config becomes a frozen, generated re-trigger block that stops changing. Adding a
concern goes from 6 edits to **1**.

---

## The approved topology

Per-CLI re-trigger → **unix domain socket** → `hook-hub` daemon (systemd `--user`,
on the host) → runs matching handlers, returns a synchronous verdict where needed,
and publishes the normalized CloudEvent to NATS for async/remote consumers.

```
  claude ─┐
  codex  ─┤   re-trigger          ┌── sync verdict ──────> back to the CLI
  copilot─┼──> unix socket ──────>│      hook-hub          (only hindsight-recall)
  hermes  ┤      (<1 ms)          │   (systemd --user)
  antigrv─┘                       ├── async handlers ────> hindsight-retain
                                  │                        claude-notify
        handlers.toml  <──────────┤                        zellij-notify
        THE ONE FILE YOU EDIT     │                        deckard
                                  │                        project-notebook
                                  │                        merge-forward
                                  │
                                  └── publish evt ──> NATS ──> candystore
                                                              holocene
                                                              event-toaster
```

Two properties make this right over "everything over NATS request/reply":

1. **The broker is never on the CLI's critical path.** A unix socket round trip is
   sub-millisecond. A NATS request/reply on every `UserPromptSubmit` and every
   `PreToolUse` is not, and a wedged broker would degrade all six CLIs at once.
2. **Handlers need host session context.** `zellij-notify` requires
   `ZELLIJ_SESSION_NAME` + `ZELLIJ_PANE_ID` and shells out to
   `zellij action rename-tab-by-id`; `claude-notify` needs the host audio session;
   deckard already proves pane context is available at hook time. A container on
   `bloodbank-network` can do none of that. Precedent for a host-side Bloodbank
   service: `services/plane-webhook-bridge/` ships its own systemd unit.

Fail-open is absolute: missing socket, dead daemon, or blown deadline → the CLI
behaves exactly as if no hook were installed.

---

## Scope

**Migrating to the hub** (your enumeration):

| # | concern | native event(s) | mode |
|---|---|---|---|
| 1 | `hindsight-recall` | UserPromptSubmit | **SYNC** — stdout injected into the prompt; calls `api.hs.delo.sh` |
| 2 | `hindsight-retain` | PostToolUse `Write\|Edit\|MultiEdit` | async |
| 3 | `hindsight-session-end` | SessionEnd | async (already `setsid nohup`) |
| 4 | `claude-notify` | Notification, PermissionRequest | async (host audio) |
| 5 | `zellij-notify` | Notification (mark), UserPromptSubmit (clear) | async (needs `ZELLIJ_PANE_ID`) |
| 6 | `deckard` | Notification, TeammateIdle, PermissionRequest | async (drops its own NATS subject) |
| 7 | `project-notebook` | SessionStart, SessionEnd | async (drops its own mini-fanout) |
| 8 | `merge-forward` | SessionEnd | async (hub replaces its self-detach) |
| — | bloodbank publisher | all roles | becomes the hub's own publish step |

**`hindsight-recall` is the only synchronous handler.** Big simplification: the
sync path must be correct, but it has exactly one client today.

**Staying natively wired, untouched:** Orca's 6 shims, `nlp hook <event>` (11
entries), the SKILL.md lint hook, `reminder-for-skill-check.sh`, and the
`~/.config/git/hooks/` guards. The plan must *prove* these keep firing —
`_merge_hooks()` foreign-entry preservation is the mechanism,
`tests/test_sync_coexistence.py` is the proof.

> Two near-misses I noticed but left out per your list:
> `reminder-for-skill-check.sh` and the SKILL.md lint hook are both yours and both
> sit in `~/.agents/hooks/`. They'd be one registry row each. Say the word and
> they come along; otherwise they stay native.

### Honest accounting of the win

Of Claude's 40 entries, **19 are in scope**; they collapse to ~11 re-trigger
entries (one per native event the hub observes). Per-CLI entry count therefore
drops modestly: **19 → 11 in Claude**.

The real win is the maintenance surface: **8 concerns × 6 CLIs = 48 wirings to
keep in sync** becomes **8 rows in one file**, plus a generated re-trigger block
per CLI that is frozen and never hand-edited again.

Secondary win: today `PostToolUse` spawns 4 hook processes per tool call
(`mise x -- python` alone measures **~17 ms**; plain `python3` ~6 ms). After
cutover the in-scope ones become **one** process that hands off to a warm daemon.

---

## Key design decision: dispatch vocabulary ⊃ publish vocabulary

`docs/event-naming.md` §6–§8 allowlists are **locked and PR-gated**. `notification`,
`attention`, and `idle` exist as neither entity nor action, so `Notification`,
`PermissionRequest`, and `TeammateIdle` have **no legal CloudEvents type** today.

Rather than amend the contract, note who actually consumes those three events:
`claude-notify`, `zellij-notify`, `deckard` — all local, all ephemeral, none needing
durable event history. So:

> **The hub dispatches on native event + lifecycle role. It publishes only the
> roles that already have a contract-legal type.**

Consequence: **zero** new schemas, **zero** allowlist amendments, **zero** lock-file
ambiguity resolution, and candystore / holocene / event-toaster see *literally no
change*. This is what makes the refactor cheap.

---

## Implementation

### 1. The re-trigger client — `bb-hook`

New: `bloodbank/services/hook-hub/client/bb-hook` (symlinked to
`~/.agents/hooks/bb-hook`, matching the existing `~/.agents/hooks/bloodbank ->
repo` pattern).

```
bb-hook <cli> <native-event> [--trailer passive|stop] [extra-args...]
```

- Reads the hook JSON from **stdin itself** (bounded read + timeout). This removes
  the `cat | ` prefix that `_command()` renders today, and the antigravity
  `; printf '{}'` shell hack.
- Captures an env allowlist: `ZELLIJ_SESSION_NAME`, `ZELLIJ_PANE_ID`, `TERM`,
  `PWD`, tty. Never the whole environment — that would ship secrets into the bus.
- Connects to `$XDG_RUNTIME_DIR/33god/hook-hub.sock` (`/run/user/1000`, mode
  `0700` — already user-private, so no auth token needed).
- Writes one JSON request line, half-closes, waits for one response line.
- Prints `response.stdout` verbatim (this is the injected context), then the
  dialect trailer if `--trailer` was given; exits `response.exit_code`.
- **Any** failure — no socket, timeout, malformed reply — prints nothing (or the
  trailer alone) and exits **0**.
- `BB_HOOK_HUB=off` → immediate exit 0. That's the kill switch.

**Language: stdlib Python 3.** Not a compiled binary. `python3 -c pass` measures
~6 ms, and the whole `agent-hooks` tree is deliberately stdlib-only with no build
step (see the module docstrings). A Rust client would save ~5 ms per hook and add
a build/dist burden; revisit only if that 5 ms ever matters. Note it must be
invoked as plain `python3`, **not** `mise x -- python` — that's the 17 ms path.

The client always waits for a reply, even when no sync handler exists (the hub
answers immediately in that case). One uniform command shape for all five
dialects beats per-event sync/async variants.

### 2. Per-dialect rendering

`render_config()` in `sync.py` keeps its structure; only the command string
changes, and it gets *simpler*:

| dialect | rendered command |
|---|---|
| `claude_settings` | `~/.agents/hooks/bb-hook claude UserPromptSubmit` |
| `codex` | `~/.agents/hooks/bb-hook codex PreToolUse` |
| `copilot` | `exec ~/.agents/hooks/bb-hook copilot preToolUse` |
| `hermes_config` | `~/.agents/hooks/bb-hook hermes pre_tool_call` |
| `antigravity_bundle` | `~/.agents/hooks/bb-hook antigravity Stop --trailer stop` |

Hermes runs `shell=False` (shlex argv, no pipes or `$VAR`) — this line satisfies
that natively, and the new path must be added to
`<role_dir>/runtime/shell-hooks-allowlist.json` by `_install_hermes_fleet()`.

### 3. The socket protocol

Newline-delimited JSON, one request → one response, close.

```json
{"v":1,"cli":"claude","native":"UserPromptSubmit",
 "cwd":"/home/delorenj/code/33GOD/bloodbank",
 "env":{"ZELLIJ_SESSION_NAME":"Workspace","ZELLIJ_PANE_ID":"12"},
 "payload":{ ...raw hook JSON... },
 "extra":[]}
```

```json
{"v":1,"stdout":"<context to inject>","exit_code":0,"handled":["hindsight-recall"]}
```

The client sends `(cli, native)`; **the hub** resolves the lifecycle role from
`hooks.master.json`. Keeps the mapping in exactly one place and means a role
remap needs no config regeneration.

### 4. The handler registry — the one file you edit

`bloodbank/services/hook-hub/handlers.toml`:

```toml
[[handler]]
id         = "hindsight-recall"
mode       = "sync"                     # sync | async
on         = ["prompt_submit"]           # lifecycle role
command    = ["~/.agents/hooks/hindsight/hindsight-recall.sh"]
timeout_ms = 2500
order      = 10                          # sync composition order

[[handler]]
id         = "hindsight-retain"
mode       = "async"
on         = ["post_tool"]
match_tool = "^(Write|Edit|MultiEdit)$"
command    = ["~/.agents/hooks/hindsight/hindsight-retain.sh"]
timeout_ms = 10000

[[handler]]
id          = "zellij-notify-mark"
mode        = "async"
on_native   = ["Notification"]           # native-only — never published
command     = ["~/.config/zellij/scripts/zellij-notify", "attention"]
require_env = ["ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"]
timeout_ms  = 1000

[[handler]]
id          = "zellij-notify-clear"
mode        = "async"
on          = ["prompt_submit"]
command     = ["~/.config/zellij/scripts/zellij-notify", "--clear"]
require_env = ["ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"]
timeout_ms  = 1000

[[handler]]
id         = "merge-forward"
mode       = "async"
on         = ["session_end"]
command    = ["~/.agents/hooks/merge-forward/session-end.sh"]
timeout_ms = 900000
```

Field semantics:

- `on` = lifecycle role (portable across all 6 CLIs — the whole point).
- `on_native` = native event name, for the three events with no legal type. This
  is how the dispatch⊃publish rule is expressed.
- `require_env` = skip cleanly when absent. This is what makes `zellij-notify`
  correct in a plain terminal instead of noisy.
- `match_tool` = regex on tool name, replacing today's per-CLI matcher strings.
- Sync composition: stdout of each sync handler, in `order`, joined by a blank
  line. With one sync handler today this is trivially correct, and stays defined
  when a second arrives.

Remaining rows (`hindsight-session-end`, `claude-notify`, `deckard`,
`project-notebook-start/end`) follow the same shape; `deckard` and `claude-notify`
bind `on_native`, the notebook pair binds `on = ["session_start"|"session_end"]`.

### 5. Daemon internals

`bloodbank/services/hook-hub/hub.py` — asyncio, one task per connection.

- Async handlers: `asyncio.create_subprocess_exec` behind a bounded
  `asyncio.Semaphore` (start at 8) so a session-end storm across many panes can't
  fork-bomb. Per-handler timeout, killed on expiry.
- Sync handlers: run inline inside the request deadline; the response is written
  as soon as they finish (or the deadline trips), **without** waiting for async
  handlers.
- **Socket activation**: `hook-hub.socket` + `hook-hub.service`, `Accept=no`. This
  is what eliminates the cold-boot gap — the first hook of the day starts the
  daemon. systemd `--user` is already load-bearing here (Hermes fleet gateways,
  `deckard@Workspace`, `bloodbank-agent-hooks-health`), so this fits.
- Registry reload: `stat` the TOML per request (cheap) + `SIGHUP`. Editing
  `handlers.toml` takes effect on the next hook, with no restart.
- Log: `$XDG_STATE_HOME/33god/hook-hub/hub.log`, size-rotated — same best-effort,
  size-rotated pattern the current publisher uses.

### 6. The publish path

The hub takes over publishing. It **imports** rather than reimplements:
`core.envelope.build_envelope`, `core.nats_publish.publish`, `core.session.SessionState`,
and the existing `clients/*.py` adapters — those keep earning their place as
per-CLI **payload shapers** (`shape_data`), which is real per-dialect knowledge.

Subjects, types, correlation/causation chains and ordering keys stay **byte-for-byte
identical**, so candystore/holocene/toaster need no change.

Host context lands in `data.origin`:

```json
"origin": {"zellij_session":"Workspace","zellij_pane":"12","host":"big-chungus"}
```

Legal with no schema bump — every `bloodbank/schemas/bloodbank/v1/agent/*.json`
declares `"additionalProperties": true` on `data` (verified).

**Bonus fix:** session state (`~/.<cli>/bloodbank-session.json`) currently has
multiple concurrent writers — parallel hooks in one session both mutate it. With
the hub as sole writer that latent race disappears.

NATS down → log and drop, exactly as today (`nats_publish` already raises and
`publisher.run` already fails open). No spool in v1; the bus is for observability
and candystore is not a system of record for hook telemetry.

### 7. `hooks.master.json` changes

- Extend Claude's `bindings` to cover the native events the hub needs but today
  ignores: `Notification`, `PermissionRequest`, `TeammateIdle`, `SessionEnd`,
  `SubagentStart`. Give the three contract-illegal ones a `publish: false` flag —
  they render a re-trigger line but no event map entry.
- No new lifecycle roles, no new types, no new schemas (see the dispatch⊃publish
  decision). `detect_ambiguities()` therefore has nothing new to resolve, and
  `hooks.mappings.lock.json` is untouched.
- Add a `retires: [...]` list of command substrings per agent, naming exactly the
  entries the installer is allowed to remove (see below).

### 8. `sync.py` changes — and the one genuinely dangerous part

`_command()` simplifies (no `cat |`, no `printf` trailer). `render_config()` keeps
its five branches.

The crux is **removal**. `_merge_hooks()` today only ever *updates or appends* its
own entry; it never deletes. Cutover needs it to delete the migrated per-concern
entries while never touching orca/nlp/skill-lint/reminder.

Mechanism: an explicit **`retires:` allowlist of command substrings** in
`hooks.master.json`, e.g. `"hindsight/hindsight-recall.sh"`,
`"zellij-notify"`, `"claude-notify"`, `"deckard-attention-hook.sh"`,
`"project-notebook/hooks/session-"`, `"merge-forward/session-end.sh"`.

Rules that make it safe:

- Removal is **opt-in by exact substring**. An entry not on the list is never
  touched, so a foreign hook can't be deleted by accident.
- Never a regex, never a prefix wildcard. `nlp hook` and `orca` appear nowhere on
  the list, so they cannot match.
- `--check` must print the exact set of entries it *would* remove, and the
  installer must refuse to run if a `retires` substring matches **zero** live
  entries (a stale entry means the list has drifted and you're about to trust a
  no-op).
- Extend `tests/test_sync_coexistence.py` with a case asserting that after
  install, a fixture containing orca + nlp + skill-lint + reminder entries still
  contains all four, byte-identical.

### 9. Failure modes

Every row must end in "the CLI is unaffected." If any row can't, the design is wrong.

| failure | what happens | why it's safe |
|---|---|---|
| Fresh boot, daemon not started | socket activation starts it on the first hook | no gap exists — this is *why* socket activation over a plain daemon |
| Daemon restarts mid-session | in-flight request gets ECONNRESET → client exits 0 | one hook silently skipped; next one reconnects |
| Stale socket file, no listener | `connect()` → ECONNREFUSED → exit 0 | `RuntimeDirectory=` has systemd clean it up |
| Socket permission error | `connect()` → EACCES → exit 0 | `/run/user/1000` is `0700`, single-user by construction |
| Sync handler exceeds deadline | hub returns what it has; partial or empty context | prompt still submits; worst case is a turn without recalled memory |
| Sync handler hangs forever | killed at `timeout_ms`; response already sent at deadline | the deadline is the hub's, not the handler's |
| Hub crashes on malformed payload | per-connection task dies, daemon lives; client times out → exit 0 | one task per connection, exceptions contained |
| Two CLIs hit simultaneously | independent asyncio tasks; async pool bounded at 8 | session state serialized by the single-writer hub |
| Plain terminal, no zellij | `require_env` skips zellij/deckard rows cleanly | no error output, no noise |
| NATS down | publish raises, logged, dropped | already today's behavior (`publisher.run` fails open) |
| `handlers.toml` malformed | hub keeps the last good registry, logs loudly | never dispatches against a half-parsed file |
| Registry lists a missing command | that row logs and is skipped | one handler dark; others unaffected |

The one risk I'd flag in the approved topology: **the sync path is a new
single point of failure for prompt submission.** Today `hindsight-recall` failing
degrades to "no memory injected." After cutover, a wedged hub does the same — but
only because the client's deadline-and-exit-0 discipline is absolute. That
behavior deserves a dedicated test, not just review.

---

## Cutover sequence

The rule that eliminates double-firing: **migrate one native event at a time, and
in the same commit that adds the handler rows, remove the old entries.** Never let
both paths be live. Shadow mode is deliberately rejected — `hindsight-retain`
firing twice writes memory twice and `merge-forward` firing twice spawns two
900 s workers.

| phase | change | verification observable |
|---|---|---|
| 0 | Ship hub + client + registry; nothing wired | `echo '{}' \| bb-hook claude Notification` returns `{"v":1,...}`; `systemctl --user status hook-hub.socket` is listening |
| 1 | `Notification` only (claude-notify, zellij-notify, deckard) | Trigger a notification: sound plays, zellij tab renames, deckard goes amber. `git -C ~/.claude diff settings.json` shows exactly 3 removals + 1 addition |
| 2 | `SessionEnd` (hindsight-session-end, merge-forward, project-notebook-end) | Exit a session: hindsight journal line appears; **exactly one** `33god-merge-forward` worker in `pgrep -af rebalance.py`; notebook updated |
| 3 | `UserPromptSubmit` — **the sync path** (hindsight-recall + zellij clear) | Recalled memories still appear in-context; `~/.claude/.hindsight-journal/` gets a `recall` event; tab name clears |
| 4 | `PostToolUse` (hindsight-retain) + `SessionStart` (project-notebook) | Edit a file → retain event in the journal; new session → notebook session-start ran |
| 5 | Fold the bloodbank publisher in — re-trigger replaces `publish.py` entries | `docker logs -f bloodbank-event-toaster` shows the same subjects; candystore row count still climbing; `data.origin` now present |
| 6 | Roll to codex / copilot / hermes / antigravity via `mise run deploy` | `mise run hooks:check` clean; `mise run health:hooks:check` exit 0 |

At every phase, these must stay green:

```bash
cd ~/code/33GOD/bloodbank
mise run hooks:check                  # SSOT drift/ambiguity gate
mise run smoketest:agent-hooks-ssot   # every binding builds a contract+schema-valid envelope
mise run health:hooks:check           # deployed configs produce error-free hooks
python3 -m pytest services/agent-hooks/tests/test_sync_coexistence.py
```

And the untouched-by-design check — orca and nlp still firing:

```bash
pgrep -af 'orca|nanoleaf' ; nlp status     # nlp still driving the panels
```

### Rollback

`~/.claude` is a **git repo** and `settings.json` is **tracked** — so the real
rollback is better than the `.bak-<timestamp>` files:

```bash
git -C ~/.claude checkout settings.json          # undo one phase
systemctl --user stop hook-hub.socket hook-hub.service
export BB_HOOK_HUB=off                            # instant global kill switch
mise run hooks:uninstall                          # remove 33GOD injections entirely
```

`sync.py:_backup()` keeps writing `settings.json.bak-<ts>` beside it; those are
covered by `*.bak-*` in `~/.config/git/ignore` and are untracked — verified, no
leak risk.

### Repos this will dirty (land all of them)

- `~/code/33GOD/bloodbank` — the hub service, `hooks.master.json`, `sync.py`,
  tests, mise tasks
- `~/.agents` — the `bb-hook` + `handlers.toml` symlinks
- `~/.claude` — `settings.json` at each phase
- `~/code/deckard` — retire `install-hooks` and the `deckard.evt.attention`
  subject **(your call — flagging, not assuming)**
- `project-notebook` (skillex) — retire its mini `hooks.master.json` +
  `claude.settings.json` **(your call)**

Finish with `git unpushed` clean.

### What becomes dead code

Confident: the per-CLI `hooks.json` / `settings.hooks.json` publisher entries,
the `cat |` and `; printf '{}'` shell scaffolding in `_command()`, and
`legacy_publishers` (`claude/publish.py` et al.) once nothing references them.

Keep: `clients/*.py` (payload shapers — the hub imports them), `core/*`,
`forward_envelope.py` (a separate producer path for pr-crusher; unrelated).

Your call: deckard's own subject and hook installer; project-notebook's mini
fanout. Both are safe to leave in place indefinitely — they just become redundant.

---

## Diagram

`docs/hook-hub-topology.excalidraw` — two panels: today's 6x8 fan-out hairball
beside the hook-hub topology. Open it at https://draw.delo.sh by drag-drop or
File -> Open. Fully editable; nothing leaves the machine.

Note: `draw.delo.sh` is the stock Excalidraw build, so its *share-link* button
still posts to `json.excalidraw.com`. Local file open is unaffected. On-domain
share links would need a self-hosted `excalidraw-json` backend — separate work.
