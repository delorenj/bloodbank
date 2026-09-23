# event-toaster

Subscribes to every bloodbank event (`bloodbank.evt.>`) and turns the ones a
person wants to hear about into desktop/phone notifications on
[ntfy.delo.sh](https://ntfy.delo.sh), topic `bloodbank`.

## What it does

1. Connects to NATS at `nats://nats:4222` and core-subscribes to `bloodbank.evt.>`.
2. Decodes each CloudEvents envelope and gives it one disposition by `type`:

   | Disposition | Default types | What happens |
   |-------------|---------------|--------------|
   | **mute**    | `bloodbank.agent.hook.updated`, `bloodbank.system.hook.updated` | Counted, never posted. These are hook-hub state pulses (~9/s in a busy session); Holocene and Candystore are their read side. |
   | **digest**  | `bloodbank.agent.tool.*` | Counted and rolled into one low-priority summary toast every `DIGEST_SECONDS`. |
   | **toast**   | everything else | One toast each at `NTFY_PRIORITY`, through a token bucket (`TOAST_RATE_PER_MIN`, `TOAST_BURST`). Overflow joins the digest. |

3. Respects ntfy pushback: a 429 or 5xx pauses posting for `Retry-After` (or an
   exponential backoff from 10s to 5m when there is none), and anything arriving
   during the pause is counted into the digest instead of retried.

There is no JetStream consumer, no durability, no replay: toasts are ephemeral
by design. If the toaster is down when an event fires, that event is missed
(Candystore still persists it).

### Why it filters

Until 2026-09-22 it posted every envelope, one request per event. hook-hub's
`agent.hook.updated` alone runs at ~9/s, so ntfy answered ~65% of the posts with
429 (thousands every 10 minutes). Every publisher on this host reaches ntfy from
the same IP, and a user without an ntfy tier is limited per IP, so the flood also
429'd n8n's `lifecycle` pushes: the lane skip notifications silently never
arrived. The publisher users (`bloodbank-toaster`, `n8n`) are now on ntfy's
`service` tier with their own limits (see
`~/docker/stacks/monitoring/ntfy/config/server.yml`), and the toaster posts a
few requests a minute instead of several a second.

## Is my event reaching the broker?

`docker logs -f bloodbank-event-toaster` logs one line per non-muted event:
`toasted: <type>`, `digested: <type>` or `rate-limited: <type>`. Muted types
show in the `stats` line printed every minute (`received=… muted=… posted=…
http_429=…`). To see everything, set `BLOODBANK_TOASTER_MUTE_TYPES=` (empty)
and redeploy.

## Env vars

Compose maps `BLOODBANK_TOASTER_*` from your shell onto these.

| Var | Default | Purpose |
|-----|---------|---------|
| `NATS_URL`             | `nats://nats:4222`     | Broker connect URL |
| `SUBJECT_FILTER`       | `bloodbank.evt.>`      | NATS subject filter |
| `NTFY_URL`             | `https://ntfy.delo.sh` | ntfy base URL |
| `NTFY_TOPIC`           | `bloodbank`            | ntfy topic |
| `NTFY_PRIORITY`        | `5`                    | Priority of individual toasts, 1=min, 5=max |
| `NTFY_TAGS`            | `drop_of_blood,zap`    | ntfy tags / emoji shortcodes |
| `NTFY_TOKEN`           | _(required)_           | Bearer token for the `bloodbank-toaster` ntfy user (`op://DeLoSecrets/ntfy Access Token/credential`). The toaster refuses to start without it. |
| `TOASTER_MUTE_TYPES`   | `bloodbank.agent.hook.updated,bloodbank.system.hook.updated` | Comma list of `fnmatch` globs never posted. Empty turns muting off. |
| `TOASTER_DIGEST_TYPES` | `bloodbank.agent.tool.*` | Comma list of globs rolled into the digest. Empty turns it off. |
| `TOAST_RATE_PER_MIN`   | `20`                   | Sustained individual toasts per minute |
| `TOAST_BURST`          | `10`                   | Individual toasts allowed back to back |
| `DIGEST_SECONDS`       | `300`                  | Digest interval (sent only when it has something to say) |
| `STATS_SECONDS`        | `60`                   | Interval of the `stats` log line |
| `MAX_BODY_CHARS`       | `400`                  | Truncate the data payload in the toast body |
| `LOG_LEVEL`            | `INFO`                 | stdlib logging level |

## Subscribe to it

ntfy runs `auth-default-access: deny-all`, so subscribe as a user granted
`bloodbank` (e.g. `delorenj`): web `https://ntfy.delo.sh/bloodbank`, or the
ntfy app with server `https://ntfy.delo.sh` and topic `bloodbank`.

## Run

```bash
BLOODBANK_TOASTER_NTFY_TOKEN="$(op read 'op://DeLoSecrets/ntfy Access Token/credential')" \
docker compose --project-name bloodbank-toaster \
  -f services/event-toaster/docker-compose.yml \
  up -d --build
```

## Test

```bash
cd services/event-toaster
uv run --with nats-py --with httpx python -m unittest -v test_main
```

## Anti-patterns

- Don't use this for anything load-bearing: it is best-effort.
- Don't make it retry or queue. A toast that could not go out is a count in
  the next digest, never a backlog that replays into a rate limit.
- Don't route per-producer notification logic through here. If a producer needs
  a targeted page, publish to its own ntfy topic from the producer side (as the
  n8n lifecycle lane does on `lifecycle`).
- Don't extend this to also durably persist. That's what Candystore is for.
