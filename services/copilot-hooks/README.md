# copilot-hooks

GitHub Copilot CLI → Bloodbank event-bus integration. Every supported Copilot
hook fires a CloudEvents envelope on NATS subject `event.copilot.*`, which the
[`event-toaster`](../event-toaster/README.md) catch-all consumer forwards to
`https://ntfy.delo.sh/bloodbank`.

## What ships here

| Path | Purpose |
|------|---------|
| `copilot_hook_publish.py` | Stdlib-only NATS publisher. Reads hook payload JSON on stdin, builds an envelope, pubs to `event.copilot.<dotted>`. |
| `hooks.json`              | Canonical Copilot CLI hooks config. Installed (via symlink) at `~/.copilot/hooks/bloodbank.json`. |

## Supported hooks

Reference: <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-hooks>

| Copilot hook         | NATS subject                       | Envelope `type`            |
|----------------------|------------------------------------|----------------------------|
| `sessionStart`       | `event.copilot.session.started`    | `copilot.session.started`  |
| `sessionEnd`         | `event.copilot.session.ended`      | `copilot.session.ended`    |
| `userPromptSubmitted`| `event.copilot.prompt.submitted`   | `copilot.prompt.submitted` |
| `preToolUse`         | `event.copilot.tool.pre`           | `copilot.tool.pre`         |
| `postToolUse`        | `event.copilot.tool.post`          | `copilot.tool.post`        |
| `errorOccurred`      | `event.copilot.error.occurred`     | `copilot.error.occurred`   |
| `agentStop`          | `event.copilot.agent.stopped`      | `copilot.agent.stopped`    |

Any unrecognized hook name falls through a camelCase→dotted transform.

## Install

```bash
mkdir -p ~/.copilot/hooks
ln -snf "$(pwd)/hooks.json" ~/.copilot/hooks/bloodbank.json
```

Copilot CLI picks up `~/.copilot/hooks/*.json` automatically on next launch.

## Verify

Trigger every hook manually and watch the catch-all chain:

```bash
SCRIPT=services/copilot-hooks/copilot_hook_publish.py
for hook in sessionStart sessionEnd userPromptSubmitted preToolUse postToolUse errorOccurred agentStop; do
  echo "{\"probe\":\"$hook\"}" | python3 "$SCRIPT" "$hook"
done

# Toaster ate them all:
docker logs bloodbank-event-toaster --tail 20 | grep 'toasted: copilot'

# ntfy received them all:
curl -s "https://ntfy.delo.sh/bloodbank/json?poll=1&since=60s" \
  | jq -r 'select(.title | startswith("copilot.")) | .title'
```

## Configuration

The publisher honors a few env vars (set in the hook entry's `"env"` block or
the shell):

| Var | Default | Purpose |
|-----|---------|---------|
| `BLOODBANK_NATS_HOST` | `127.0.0.1`  | NATS host (locally exposed by `bloodbank-nats:4222`) |
| `BLOODBANK_NATS_PORT` | `4222`       | NATS port |
| `BLOODBANK_NATS_TIMEOUT` | `3.0`     | Connect/publish timeout in seconds |
| `BLOODBANK_HOOK_STRICT`  | _(unset)_ | When `1`, exit non-zero on publish failure (default: swallow failure so the agent never blocks) |
| `BLOODBANK_HOOK_VERBOSE` | _(unset)_ | When set, log "published <subject>" to stderr |

## Design notes

- **Stdlib only.** The publisher speaks the NATS text protocol directly over a
  TCP socket — no `nats-py`, no virtualenv, no extra install steps. Justified
  by the one-shot fire-and-forget shape: open, PUB, PING/PONG, close.
- **Fail open.** A hook that errors must never break the agent's session, so
  the publisher exits `0` on connect/publish failure unless
  `BLOODBANK_HOOK_STRICT=1`.
- **5-second hook timeout.** Copilot's default is 30s; we cap at 5s so a
  broken NATS doesn't visibly slow the agent.
- **Mirrors the existing pattern.** `hookd_bridge/` does the same job for
  Claude Code hooks via HTTP→RabbitMQ; this script does it for Copilot via
  stdin→NATS. Different broker because the v3 catch-all consumer
  (`event-toaster`) lives on NATS.

## Anti-patterns

- Don't add filtering or per-hook business logic here. Subscribe a dedicated
  consumer to the `event.copilot.*` subject downstream instead.
- Don't switch the publisher to RabbitMQ — the catch-all toaster is NATS-only.
- Don't widen the script's deps to include `nats-py` — stdlib keeps install
  drift at zero.
