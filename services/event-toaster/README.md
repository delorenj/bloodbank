# event-toaster

Pass-through subscriber that forwards every bloodbank `event.>` envelope as a
desktop notification via [ntfy.delo.sh](https://ntfy.delo.sh).

## What it does

1. Connects to NATS at `nats://nats:4222` (the sandbox broker).
2. Core-subscribes to `event.>` (NATS subject wildcard — receives every event).
3. Decodes the CloudEvents envelope JSON.
4. POSTs a short summary to `https://ntfy.delo.sh/<NTFY_TOPIC>` with `Priority: 5`
   so the toast is *loud*.

There is no JetStream consumer, no durability, no replay — toasts are ephemeral
by design. If the toaster is down when an event fires, that event is missed
(but Candystore still persists it).

## Env vars

| Var | Default | Purpose |
|-----|---------|---------|
| `NATS_URL`      | `nats://nats:4222`     | Broker connect URL |
| `SUBJECT_FILTER`| `event.>`              | NATS subject filter |
| `NTFY_URL`      | `https://ntfy.delo.sh` | ntfy base URL |
| `NTFY_TOPIC`    | `bloodbank`            | ntfy topic name (subscribe to this on your phone/desktop) |
| `NTFY_PRIORITY` | `5`                    | 1=min, 5=max (loud) |
| `NTFY_TAGS`     | `drop_of_blood,zap`    | Comma-separated ntfy tags / emoji shortcodes |
| `NTFY_TOKEN`    | _(empty)_              | Bearer token; only needed if your ntfy server enforces auth |
| `MAX_BODY_CHARS`| `400`                  | Truncate the data payload in the toast body |
| `LOG_LEVEL`     | `INFO`                 | stdlib logging level |

## Subscribe to it

On your phone or desktop:

```bash
# Web: open https://ntfy.delo.sh/bloodbank in a browser tab.
# CLI:
curl -s https://ntfy.delo.sh/bloodbank/json
# ntfy desktop or mobile app: subscribe to topic "bloodbank" on ntfy.delo.sh.
```

## Run

```bash
docker compose --project-name bloodbank-toaster \
  -f services/event-toaster/docker-compose.yml \
  up -d --build
```

## Anti-patterns

- Don't use this for anything load-bearing — it's best-effort.
- Don't add filtering logic in this service. If you need filtered toasts,
  publish them to a different ntfy topic from the producer side.
- Don't extend this to also durably persist. That's what Candystore is for.
