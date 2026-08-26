# hook-hub

One dispatcher for every agent CLI's hooks.

Every supported agent CLI already re-triggers into a shared lifecycle-role
vocabulary via [`../agent-hooks/hooks.master.json`](../agent-hooks/hooks.master.json).
What never followed was the *behavior*: each concern stayed hand-wired into every
CLI's native config, so adding one meant editing six files across five dialects.
`~/.claude/settings.json` alone carries 40 wiring entries across 13 events.

This service is where that behavior moves. One registry — [`handlers.toml`](handlers.toml)
— binds handlers to lifecycle roles, and every CLI reaches it through the same
`bb-hook` re-trigger.

```
  claude ─┐
  codex  ─┤   re-trigger          ┌── sync verdict ──────> back to the CLI
  copilot─┼──> unix socket ──────>│      hook-hub
  hermes  ┤      (<1 ms)          │
  antigrv─┘                       └── async handlers ────> hindsight, notify,
        handlers.toml  <───────────────                    zellij, notebook, …
        the one file you edit
```

Full design and cutover sequence: [`../../docs/hook-hub-plan.md`](../../docs/hook-hub-plan.md).
Topology diagram: `../../docs/hook-hub-topology.excalidraw`.

## Why a unix socket and not NATS

Two reasons, both load-bearing:

1. **The broker stays off the CLI's critical path.** A socket round trip is
   sub-millisecond. NATS request/reply on every `UserPromptSubmit` and every
   `PreToolUse` is not, and a wedged broker would degrade all six CLIs at once.
2. **Handlers need host session context.** `zellij-notify` needs
   `ZELLIJ_SESSION_NAME` + `ZELLIJ_PANE_ID` and shells out to `zellij action`;
   `claude-notify` needs the host audio session. A container on
   `bloodbank-network` can reach none of that, so the hub is a host daemon.

## Scope today

**This daemon dispatches; it does not publish.** Envelope publishing stays in
[`../agent-hooks/publish.py`](../agent-hooks/publish.py) until the cutover phase
that moves it — notably so the `_fanout_alert` → `deckard.evt.v1.attention` path
added on 2026-08-26 is not duplicated here. Candystore, Holocene and the
event-toaster are unaffected by this service.

## Layout

| path | what |
|---|---|
| `hub.py` | the daemon: asyncio, unix socket, socket-activated |
| `client/bb-hook` | the re-trigger every CLI calls |
| `handlers.toml` | the handler registry — **the one file you edit** |
| `systemd/hook-hub.socket` | socket unit (starts the service on first hook) |
| `systemd/hook-hub.service` | the daemon unit |
| `tests/test_hub.py` | behavioral tests; no daemon or NATS needed |

## Install

```bash
mise run hub:install     # link units into ~/.config/systemd/user, enable the socket
mise run hub:status      # is it listening?
mise run hub:logs        # tail the hub log
```

Or by hand:

```bash
ln -sf ~/code/33GOD/bloodbank/services/hook-hub/systemd/hook-hub.socket  ~/.config/systemd/user/
ln -sf ~/code/33GOD/bloodbank/services/hook-hub/systemd/hook-hub.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hook-hub.socket
```

Nothing is wired into any agent CLI by installing this. The hub sits idle until
`hooks.master.json` renders `bb-hook` lines — that is a separate, per-event
cutover step.

## The wire protocol

Newline-delimited JSON over `$XDG_RUNTIME_DIR/33god/hook-hub.sock`. One request,
one reply, close.

```json
{"v":1,"cli":"claude","native":"UserPromptSubmit",
 "cwd":"/home/delorenj/code/33GOD/bloodbank",
 "env":{"ZELLIJ_SESSION_NAME":"Workspace","ZELLIJ_PANE_ID":"12"},
 "payload":{"prompt":"..."},"extra":[]}
```

```json
{"v":1,"stdout":"<context to inject>","exit_code":0,"handled":["hindsight-recall"]}
```

The client sends `(cli, native)`; the **hub** resolves the lifecycle role from
`hooks.master.json`, so a role remap needs no config regeneration.

## Fail-open, in detail

The client exits `0` and prints nothing on every abnormal path: no socket, no
daemon, connection reset, malformed reply, blown deadline. Two specifics worth
knowing, because they are what keep a hook from wedging an agent:

- **stdin is read through `select()` against a 0.5 s budget**, never a bare
  `read()`. A harness that opens stdin and never closes it cannot hold the
  client. There is a test for exactly this
  (`test_stdin_that_never_closes_cannot_hang_the_client`).
- **every deadline is absolute** from process start, so a slow stdin cannot lend
  its remaining budget to a slow socket.

On the daemon side: a hung handler is killed at its `timeout_ms`; a missing
binary, a malformed request, or a broken registry is logged and scoped to the one
connection or handler that caused it. A registry that fails to parse leaves the
last good config serving.

## Environment

| var | default | purpose |
|---|---|---|
| `BB_HOOK_HUB` | — | `off` makes the client an immediate no-op. The kill switch. |
| `BB_HOOK_SOCKET` | `$XDG_RUNTIME_DIR/33god/hook-hub.sock` | socket path (client and daemon) |
| `BB_HOOK_DEADLINE` | `3.0` | client's total budget, seconds |
| `HOOK_HUB_REGISTRY` | `handlers.toml` beside `hub.py` | registry path |
| `HOOK_HUB_SYNC_BUDGET` | `2.5` | shared deadline for all sync handlers |
| `HOOK_HUB_ASYNC_SLOTS` | `8` | concurrent async handlers; bounds a session-end storm |
| `HOOK_HUB_LOG` | `$XDG_STATE_HOME/33god/hook-hub/hub.log` | size-rotated at 1 MiB |
| `HOOK_HUB_STDERR` | — | also log to stderr (for `journalctl`) |

Handlers additionally receive `BB_HOOK_CLI`, `BB_HOOK_NATIVE`, `BB_HOOK_ROLE`,
and `BB_HOOK_HUB=off` — the last so a handler that re-enters an agent CLI cannot
recurse back into the hub.

## Verify

```bash
python3 -m pytest services/hook-hub/tests/test_hub.py     # 19 behavioral tests
mise run hub:smoke                                        # live round trip
```

Manual round trip against a throwaway hub:

```bash
export BB_HOOK_SOCKET=/tmp/hub.sock HOOK_HUB_LOG=/tmp/hub.log
python3 services/hook-hub/hub.py &
printf '{"tool_name":"Bash"}' | services/hook-hub/client/bb-hook claude PreToolUse
cat /tmp/hub.log
```

## Adding a handler

One row in `handlers.toml`. Bind to a lifecycle role (`on`) and it fires for
every CLI; bind to `on_native` for signals with no contract-legal event type
(`Notification`, `PermissionRequest`, `TeammateIdle`). Field reference is in the
file's own header comment.

The registry is re-read when its mtime changes, so an edit takes effect on the
next hook — no restart. `systemctl --user kill -s HUP hook-hub` forces it.

**During cutover, enable a row in the same commit that removes that concern's old
native wiring.** Never both at once: `hindsight-retain` firing twice writes memory
twice, and `merge-forward` firing twice spawns two 900-second workers.
