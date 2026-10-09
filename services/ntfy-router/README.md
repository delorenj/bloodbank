# ntfy-router

Subscribes to every bloodbank event (`bloodbank.evt.>`) and turns the ones a
person wants to hear about into desktop/phone notifications on
[ntfy.delo.sh](https://ntfy.delo.sh), topic `bloodbank`.

## What it does

1. Connects to NATS at `nats://nats:4222` and core-subscribes to `bloodbank.evt.>`.
2. Decodes each CloudEvents envelope and gives it one disposition by `type`:

   | Disposition | Default types | What happens |
   |-------------|---------------|--------------|
   | **mute**    | `bloodbank.agent.hook.updated`, `bloodbank.system.hook.updated` | Counted, never posted. These are hook-hub state pulses (~2/s in a busy session since 2026-09-23 coalescing; ~9/s before); Holocene and Candystore are their read side. |
   | **digest**  | `bloodbank.agent.tool.*` | Counted and rolled into one low-priority summary route every `DIGEST_SECONDS`. |
   | **route**   | everything else | One route each at `NTFY_PRIORITY`, through a token bucket (`ROUTE_RATE_PER_MIN`, `ROUTE_BURST`), behind a smaller per-type bucket (`ROUTE_PER_TYPE_PER_MIN`, `ROUTE_PER_TYPE_BURST`) so one chatty type cannot spend the shared budget. Overflow joins the digest. |

   **Loud** types (`NTFY_ROUTER_LOUD_TYPES`, default `bloodbank.project.deployment.*`)
   are routes that skip both buckets, so a deploy and its catch-up installs never
   queue behind a burst of `llm.usage` events or fall into the digest.

3. Respects ntfy pushback: a 429 or 5xx pauses posting for `Retry-After` (or an
   exponential backoff from 10s to 5m when there is none), and anything arriving
   during the pause, loud types included, is counted into the digest instead of
   retried.

There is no JetStream consumer, no durability, no replay: routes are ephemeral
by design. If the router is down when an event fires, that event is missed
(Candystore still persists it).

### Links become buttons

Any event may carry `data.links`, a list of `{rel, label, url}` (see
`schemas/bloodbank/project/deployment.*.json`). When at least one link is usable
(a label, and a URL that starts `https://` or `itms-services://`):

- the links become ntfy `view` actions, at most 3, in the event's order; a `run`
  link gives way when three others exist. Each tap clears the notification.
  Other URLs are dropped and logged (`dropped link on <type>`).
- Click (a tap on the notification itself) opens the `page` link, else the `run`
  link, else the first https link.
- the event is published as JSON to the ntfy root (`POST https://ntfy.delo.sh/`
  with `{topic, title, message, priority, tags, click, actions}`), so the title
  can be UTF-8 and labels or URLs with commas, semicolons or quotes need no
  header escaping.

Events without usable links keep the header publish (`Title`, `Priority`,
`Tags`) they always had, byte for byte. A Title header is ASCII-only: accents
fold (`Grüvato` -> `Gruvato`), anything else is dropped.

There is no `Attach` header. ntfy-android's `canOpenAttachment` refuses
`application/vnd.android.package-archive`, and the app has no
`REQUEST_INSTALL_PACKAGES`, so an attachment would only download the APK. The
**Install APK** button hands the URL to the browser instead, whose download
opens in the package installer as an update.

### Deployment notifications

`bloodbank.project.deployment.completed` and `.failed` with links (mobile-deploy-hub
v1.1 and later) get plain titles:

| Event | Title | Priority | Tags |
|-------|-------|----------|------|
| deploy completed | `Tower of Lost Things 0.2.2 deployed` | 5 | `package` |
| catch-up completed | `Tower of Lost Things 0.2.2 installed on S26` | 4 | `white_check_mark` |
| deploy failed | `Tower of Lost Things 0.2.2 deploy failed at build` | 5 | `rotating_light` |
| catch-up failed | `Tower of Lost Things 0.2.2 catch-up failed on iPad` | 4 | `warning` |

A completed body is one line per device, the APK's size and the run:

```
S26: pending (not on big-chungus's adb)
iPad: installed 0.2.2 (4), verified
APK 72 MB
Run: https://github.com/delorenj/pile-of-dumb-things/actions/runs/37940000000
```

A failed body is the stage, the reason and the run. The event carries no size,
so the router reads the APK's `Content-Length` with an anonymous HEAD on its
`apk` link (3 s timeout; on any failure the size line is left out).

A catch-up install of a device the router already announced as installed in the
same run (by the deploy, or by an earlier copy of the catch-up event) is logged
as `duplicate:` and not posted. The memory is in-process and bounded.

Deployment events without links (hub v1.0.0) are routed as they always were.

### iPhone and iPad

ntfy-ios opens a `view` URL with `UIApplication.shared.open` and no scheme
filter, so **Install on iPad** (`itms-services://…`) reaches iPadOS unchanged.
iOS only wakes for a self-hosted topic when ntfy forwards a poll request
upstream, and `ntfy-upstream-gate` (33GOD `monitoring/ntfy`) forwards only
low-volume topics, never the `bloodbank` firehose. So every deployment and
catch-up event is also published to **`deploys`** (`NTFY_DEPLOY_TOPIC`), a
topic of a few messages a day that the gate forwards: subscribe ntfy-ios on the
iPad (and an iPhone) to `deploys`. The S26 keeps getting them on `bloodbank`;
subscribe it to `deploys` too only if you then mute deployments on `bloodbank`.

### Why it filters

Until 2026-09-22 it posted every envelope, one request per event. hook-hub's
`agent.hook.updated` alone ran at ~9/s (hook-hub now coalesces it to ~2/s), so ntfy answered ~65% of the posts with
429 (thousands every 10 minutes). Every publisher on this host reaches ntfy from
the same IP, and a user without an ntfy tier is limited per IP, so the flood also
429'd n8n's `lifecycle` pushes: the lane skip notifications silently never
arrived. The publisher users (`bloodbank-ntfy-router`, `n8n`) are now on ntfy's
`service` tier with their own limits (see 33GOD
`monitoring/ntfy/config/server.yml`), and the router posts a
few requests a minute instead of several a second.

## Is my event reaching the broker?

`docker logs -f bloodbank-ntfy-router` logs one line per non-muted event:
`routed: <type>`, `digested: <type>`, `rate-limited: <type>` or
`duplicate: <type>`; an event with buttons adds `buttons: <labels>`. Muted types
show in the `stats` line printed every minute (`received=… muted=… posted=…
http_429=…`). To see everything, set `BLOODBANK_NTFY_ROUTER_MUTE_TYPES=` (empty)
and redeploy.

## What would it send?

```bash
python3 main.py --render [APK_BYTES] < envelope.json
docker exec -i bloodbank-ntfy-router python /app/main.py --render < envelope.json
```

prints the request for one envelope (method, URL, headers with the token
redacted, body). Nothing is posted and no limit is spent.

## Env vars

Compose maps `BLOODBANK_NTFY_ROUTER_*` from your shell onto these.

| Var | Default | Purpose |
|-----|---------|---------|
| `NATS_URL`             | `nats://nats:4222`     | Broker connect URL |
| `SUBJECT_FILTER`       | `bloodbank.evt.>`      | NATS subject filter |
| `NTFY_URL`             | `https://ntfy.delo.sh` | ntfy base URL; link events post JSON to its root |
| `NTFY_TOPIC`           | `bloodbank`            | ntfy topic (`review.slop` events go to `slop`) |
| `NTFY_DEPLOY_TOPIC`    | `deploys`              | Every `bloodbank.project.deployment.*` event is also published here (the topic the upstream gate forwards, so ntfy-ios wakes). Empty turns the copy off |
| `NTFY_PRIORITY`        | `5`                    | Priority of individual routes, 1=min, 5=max (deployments set their own) |
| `NTFY_TAGS`            | `drop_of_blood,zap`    | ntfy tags / emoji shortcodes (deployments set their own) |
| `NTFY_TOKEN`           | _(required)_           | Bearer token for the `bloodbank-ntfy-router` ntfy user (`op://DeLoSecrets/ntfy Access Token/credential`). The router refuses to start without it. |
| `NTFY_ROUTER_MUTE_TYPES`   | `bloodbank.agent.hook.updated,bloodbank.system.hook.updated` | Comma list of `fnmatch` globs never posted. Empty turns muting off. |
| `NTFY_ROUTER_DIGEST_TYPES` | `bloodbank.agent.tool.*` | Comma list of globs rolled into the digest. Empty turns it off. |
| `NTFY_ROUTER_LOUD_TYPES`   | `bloodbank.project.deployment.*` | Comma list of globs that skip both rate-limit buckets (not ntfy's pushback). Empty turns it off. |
| `ROUTE_RATE_PER_MIN`   | `20`                   | Sustained individual routes per minute |
| `ROUTE_BURST`          | `10`                   | Individual routes allowed back to back |
| `ROUTE_PER_TYPE_PER_MIN` | `6`                  | Sustained routes per minute for any one event type; `0` turns the per-type cap off |
| `ROUTE_PER_TYPE_BURST` | `3`                    | Routes of one event type allowed back to back |
| `DIGEST_SECONDS`       | `300`                  | Digest interval (sent only when it has something to say) |
| `STATS_SECONDS`        | `60`                   | Interval of the `stats` log line |
| `MAX_BODY_CHARS`       | `400`                  | Truncate the data payload in the route body |
| `LOG_LEVEL`            | `INFO`                 | stdlib logging level |

## Subscribe to it

ntfy runs `auth-default-access: deny-all`, so subscribe as a user granted
`bloodbank` (e.g. `delorenj`): web `https://ntfy.delo.sh/bloodbank`, or the
ntfy app with server `https://ntfy.delo.sh` and topic `bloodbank`. For deploys
on an iPad or iPhone, subscribe to `deploys` instead (ntfy-ios is only woken
for topics the upstream gate forwards).

## Run

From this directory, with the token in the environment of that one command:

```bash
BLOODBANK_NTFY_ROUTER_NTFY_TOKEN="$(op read 'op://DeLoSecrets/ntfy Access Token/credential')" \
  docker compose -f compose.yml up -d --build
docker logs bloodbank-ntfy-router 2>&1 | grep -m1 'routed:'
```

## Test

```bash
cd services/ntfy-router
python3 -m unittest -v test_main
# or, without httpx and nats-py on the system python:
uv run --with nats-py --with httpx python -m unittest -v test_main
```

## Anti-patterns

- Don't use this for anything load-bearing: it is best-effort.
- Don't make it retry or queue. A route that could not go out is a count in
  the next digest, never a backlog that replays into a rate limit.
- Don't route per-producer notification logic through here. If a producer needs
  a targeted page, publish to its own ntfy topic from the producer side (as the
  n8n lifecycle lane does on `lifecycle`). Buttons are the exception: a producer
  puts `data.links` on its event and the router renders them for every type.
- Don't add an `Attach` header for APKs (see above).
- Don't extend this to also durably persist. That's what Candystore is for.
