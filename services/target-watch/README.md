# target-watch

Target-focused alerts. You set up a watch for a person; it fires whenever any
inbound message from them arrives, on any provider the bus carries: email from
any of their addresses, a carrier text (SMS, MMS, RCS, iMessage, including a
group chat they are in), Telegram, Slack, Teams, Google Chat, WhatsApp, a phone
call or a voicemail. Every alert funnels into one sink that files or updates a
ticket on the target's board and tells the operator what it did.

The watch is not tied to a provider. A provider becomes watchable by adding an
`x-sender` block to its inbound schema, and every watch picks it up on the next
schema reload. No code changes, no restart.

## How it fits together

```
 n8n (Telnyx texts, calls)  ──┐
 gmail-poll (Candybar, 5 min) ┼─▶ bloodbank.evt.messaging.*.received ─┐
 any other inbound producer ──┘    bloodbank.evt.telephony.call.*      │
                                    bloodbank.evt.telephony.transcription.completed
                                                                        ▼
                       target-watch match  (durable target-watch-matcher)
                         x-sender index from schemas/ + target files
                                                                        ▼
                                     bloodbank.evt.watch.target.detected
                                                                        ▼
                       target-watch sink   (durable target-watch-sink)
                         px task create / px comment  (cwd = target's repo)
                         ntfy urgent push to ntfy:<topic>  (first: the loud one)
                         hermes send --to telegram:<chat id>
                         Telnyx outbound call to ring:<E.164> or ring:sip:<user>@<host>  (the phone rings),
                         then a background check of the call's outcome (a Telegram line if it did not get through)
                                                                        ▼
                                     bloodbank.evt.watch.target.routed
```

| command | what it does |
|---|---|
| `target-watch match` | Durable JetStream pull consumer `target-watch-matcher` on `bloodbank.evt.messaging.>` and `bloodbank.evt.telephony.>`. Reads each event's sender through its schema's `x-sender` block, normalises the handles and publishes one `watch.target.detected` per (target, message). |
| `target-watch sink` | Durable consumer `target-watch-sink` on `bloodbank.evt.watch.target.detected`. Files or comments a ticket (`plane` sink) or only notifies (`notify_only`), notifies every configured channel and publishes `watch.target.routed`. `--dry-run` prints the plan and changes nothing. |
| `target-watch gmail-poll` | One pass of target-scoped Gmail ingress, run by Candybar every five minutes. Publishes `messaging.email.received` for mail from any target's email address. |
| `target-watch replay --since <ISO> [--target <id>]` | Reads Candystore and prints what `match` would detect. Publishes nothing. This is the dogfood proof. |
| `target-watch check` | Validates every target file and the x-sender index and prints both. Exits non-zero on any problem. |
| `target-watch status [--limit N] [--target <id>]` | What the sink did for the last N detections (default 10, newest first): when the message arrived (America/New_York), provider, ticket, action, and the outcome of every channel (ntfy, Telegram, each ring and its retry). Reads the sink's sqlite state read-only, so it needs no running service. |

Shared options: `--targets <globs>`, `--schemas-dir`, `--state-dir`, `--nats-url`. `status` reads
`$TARGET_WATCH_STATE_DIR` (or `--state-dir`) like the services.
`match` and `sink` also take `--from-file <json|jsonl|->`, which handles
envelopes from a file instead of the bus, plus `--since <ISO>` and `--once`.

### Matching rules

- Handles are normalised the same way on both sides. Emails are lowercased bare
  addresses. Phone numbers are reduced to E.164 by their digits, so a 10-digit
  number gets +1. Labels are casefolded with whitespace collapsed. A platform
  handle becomes `<platform>:<id>`, so `slack:U123` in a target file matches a
  Slack sender `U123`.
- A label also tries the part after the last `": "`, because group-chat
  notifications title the sender `<group>: <name>`. The group title is recorded
  as `conversation.group_label`.
- A display name only counts when the type declares `label` handles. That holds
  for carrier texts, where the name comes from the operator's own contacts. An
  email display name is whatever the sender typed, so it never matches.
- Telephony names the call, not the pipeline stage. The `call.ended`,
  `call.recorded` and `transcription.completed` events of one voicemail all have
  provider `voicemail` and share the call id as thread, so they land on one
  ticket. Every other call is provider `call`.
- Near-duplicates are dropped by the matcher: same target, same provider, same
  normalised text, received within 2 s. The same text sometimes arrives twice
  about 10 ms apart. The sink also skips a detection whose text matches another
  of the same target's already-acted-on detections within 60 s on a different
  carrier provider (sms, mms, rcs, imessage: the SMS copy of an RCS text), and
  records it as `skipped_duplicate`. A failed route never counts as the twin.
- Event ids are deterministic and also sent as `Nats-Msg-Id`. A detection's id
  is `uuid5(NAMESPACE_URL, "<watch_id>:<dedupe_key>")`, where `dedupe_key` is
  `<source type>:<message_id>`; telephony events have no message id, so they
  use the call id (live n8n sends call.ended and call.recorded twice per call),
  and anything else falls back to the source event id. A routed event's id is
  `uuid5(NAMESPACE_URL, "<detection id>:<action>")`. A Gmail message's id is
  `uuid5(NAMESPACE_URL, "gmail:<id>")`. A source event whose id is not a UUID
  is refused permanently (the watch contract pins causationid to it).

### Sink rules

- **Coalescing.** If a ticket was created for the same conversation within
  `coalesce_hours`, the new message is added to it with `px comment`. The
  conversation key is provider plus thread id, else group label, else sender.
  Every new JIMB ticket dispatches a PM grooming turn, so a chatty thread must
  not become a ticket storm.
- **No blind retries.** The intent is written to sqlite before px runs. Success
  is read from px's first matching line (`created JIMB-450 ...` or
  `commented on JIMB-450`), never from the URL on the last line. If px does not
  confirm, or the sink died while px was running, the detection is routed as
  `failed` and the operator is told. It is never created a second time.
- **Idempotency.** Each detection id is acted on once. If a detection is
  redelivered after the publish failed, the sink republishes the stored routed
  event and runs nothing again.
- The notification is one line:
  `<Display name> via <provider>: <snippet> -> <action> <ticket_ref>`.
- **Channel order.** `ntfy:` first, then `telegram:`, then `ring:`, whatever order `sink.notify` lists
  them in. The loud channel must not wait behind a slow `hermes send`, and the ring goes last so the
  line the operator reads after the phone rings is already there.
- **The notification log.** journald keeps almost nothing for these units, so every channel attempt is
  also written to the sqlite `notifications` table (`detection_id`, `watch_id`, masked `channel`,
  `attempt`, `outcome`, `detail`, `ref`, `at`, `updated_at`). `sent` / `failed` for ntfy and Telegram,
  with the error in `detail`; for a ring, `placed` when Telnyx accepted the dial (`ref` is the
  `call_leg_id`), then the row is updated in place when the follower reaches a terminal outcome:
  `answered`, `unanswered`, `busy`, `rejected`, `unknown`. A refused dial is `failed` or
  `channel_limit`, a skipped one `throttled`, `quiet` or `stale`, and a retry is `attempt` 2. The table
  is created with `CREATE TABLE IF NOT EXISTS`, so the live `state.db` needs no migration; a state
  write that fails is logged and never blocks a notification or places a second call. Read it with
  `target-watch status`.

### ntfy: the loud push

A Telegram line lands in the same chat as every agent's chatter and does not stand out. An `ntfy:<topic>`
entry in `sink.notify` publishes an **urgent** (priority 5) message to that topic on the operator's own
ntfy server, which the phone app is subscribed to.

```toml
notify = ["ntfy:<topic>", "telegram:<operator chat id>", "ring:sip:example-user@sip.telnyx.com"]   # FAKE user
```

- **What it sends.** `POST <base>/<topic>` with `Title: <display name> - <provider>`, `Priority: urgent`,
  `Tags: rotating_light`, and as the body the same line Telegram gets
  (`<Display name> via <provider>: <snippet> -> <action> <ticket_ref>`). When the sink filed or commented
  a ticket in this run, `Click` is px's own link to it (the bare URL line px printed after confirming the
  ticket), so tapping the notification opens the ticket; otherwise there is no `Click`. A title with
  non-ASCII characters is sent as an RFC 2047 word, which ntfy decodes.
- **Success** is HTTP 200 with a JSON body that carries the message `id`; anything else is a failed
  channel (`notify failed: ntfy:<topic>` in the routed event's reason, the cause in the notification log).
- **Base URL.** `TARGET_WATCH_NTFY_URL`, default `https://ntfy.delo.sh`.
- **Token.** Never in a file, argv or a log. The reference is `TARGET_WATCH_NTFY_TOKEN_REF` (an `op://`
  reference), else `NTFY_TOKEN_REF` from the shell env file `~/.config/infra-alerts/env` (override the
  path with `TARGET_WATCH_NTFY_ENV_FILE`; only `NTFY_TOKEN_REF` is read from it). It is read with
  `op read --no-newline <ref>` at first use (binary `TARGET_WATCH_OP`), held in memory, and dropped after
  a 401 or 403. A value that is not an `op://` reference is refused. The request sets its own User-Agent,
  because Cloudflare refuses Python-urllib's.
- **No cooldown, no quiet hours.** Every routed action except `skipped_duplicate` publishes, like
  Telegram. Quiet hours and the cooldown are the ring's: the ntfy push is the thing that is allowed to
  be loud. Mute it by dropping the `ntfy:` entry.
- A ring that did not get through (see below) is also pushed to ntfy, not only sent to Telegram.
- The topic must be one the token's user may publish to, and one the phone is subscribed to (the app
  syncs the account's subscriptions).

### Ring: make the operator's phone ring like an incoming call

A Telegram ping is quiet. A `ring:` entry in `sink.notify` places a real
call, with no speech, so the phone rings like someone is calling. The target is a
number (`ring:<E.164>`) or a SIP URI (`ring:sip:<user>@<host>`).

```toml
notify = ["telegram:<operator chat id>", "ring:sip:example-user@sip.telnyx.com"]   # FAKE user
# or a number: "ring:+12155550142"   (FAKE number)
ring_cooldown_minutes = 10          # optional, default 10
ring_quiet_hours = "22:00-07:00"    # optional, America/New_York, may wrap midnight
```

- **Mechanism.** Telnyx Call Control: `POST https://api.telnyx.com/v2/calls` with
  `{connection_id, to, from, from_display_name, timeout_secs, time_limit_secs}`. `connection_id` is
  `TARGET_WATCH_TELNYX_CONNECTION_ID` (the `target-watch-ring` call-control app, which has an
  outbound voice profile) and `from` is `TARGET_WATCH_RING_FROM` (the operator's Telnyx number). Both
  are set in `systemd/target-watch-sink.service`; they are ids of the operator's own account, not
  secrets. Success is an HTTP 2xx whose body has `data.call_control_id`; anything else (a Telnyx
  4xx, a timeout, no key) is a failed channel, logged as `ring_failed` with the HTTP status and
  Telnyx's error title only.
- **Prefer a SIP target (`ring:sip:<user>@<host>`).** The URI is dialled as is: the user is a credential
  user of a Telnyx SIP connection where the operator's SIP devices register, and the call goes straight to
  those devices with no carrier in the path. Accepted shape: `sip:` (lowercase), a user of letters, digits
  and `. _ + -` (1-64 characters), `@`, a dotted hostname. A port, `;parameters`, `?headers`, spaces or a
  second `@` are refused when the target file loads (the error never echoes the entry). The host is
  lower-cased. Proven live 2026-11-05: a dial to the credential user from the same ring connection was
  answered in 4 s.
- **Do not ring an E.164 cell that forwards to the `from` number.** From 2026-11-04 the operator's cell
  forwards unconditionally to the Telnyx number the ring dials from (`TARGET_WATCH_RING_FROM`), which routes
  to the SIP devices. A call to the cell is carried straight back to the number it came from and Telnyx
  declines the loop at once: `user_busy`, SIP 603, `hangup_source=callee`, about 0.6 s after
  `call.initiated`. Two rings (2026-11-05 01:10Z and 18:35Z) failed that way while the sink logged
  `ring_placed` and nothing else. An E.164 target is only right when nothing forwards to the `from` number.
- **Who is calling.** `from_display_name` is the target's `display_name`, cut down to what Telnyx accepts
  (ASCII letters, digits, spaces and `- _ ~ ! . +`, at most 128; accents are folded, anything else is
  dropped, and the field is left out if nothing remains). The phone shows "Jane Doe" instead of the bare
  `from` number, so a ring says whose message it is.
- **Ring length by kind.** A `ring:sip:` target rings **45 s** (`time_limit_secs` 60, which must exceed it).
  There is no carrier in the path, so nothing to outrun, and the device is a push-woken softphone that
  spends several seconds of the ring waking up: every real ring from 2026-11-05 to 2026-11-11 hung up
  `timeout` / SIP 487 after 20 s unanswered. A `ring:<E.164>` target keeps **20 s** (`time_limit_secs` 40):
  it must stay under the carrier's no-answer forward (about 25-30 s) whenever the cell forwards an
  unanswered call back to the Telnyx number, or the ring starts a loop: keep it at or under 22. An
  answered call stays silent and ends at the time limit.
- **Outcome.** HTTP 200 from Telnyx only means the dial was accepted. Each accepted ring is followed in a
  background task (off the detection path and the bus ack): `timeout_secs + 5` s after the dial (25 s for
  an E.164 ring, 50 s for a SIP one) it reads
  `GET /v2/call_events?filter[leg_id]=<call_leg_id from the dial answer>` and, while no hangup or answer
  is there yet, reads again every 5 s, 12 reads in all: the last is 60 s past the ring's timeout, and the
  follower stops there. (On 2026-11-11 a ring that hung up at +20 s was still unreadable ~37 s after the
  dial and readable later; three reads were too few.) It logs exactly one of:
  - `ring_answered`: a `call.answered` event exists.
  - `ring_unanswered`: hangup_cause `timeout`.
  - `ring_rejected`: any other hangup, with `hangup_cause`, `sip_hangup_cause`, `hangup_source` and
    `busy` (true for `user_busy` or SIP 486/600/603).
  - `ring_outcome_unknown`: the events never became readable (HTTP error, timeout, no key, no
    `call_leg_id` in the dial answer).

  Anything other than `ring_answered` sends **one** Telegram line to the target's telegram channel(s):
  `target-watch: the phone ring for <name> (<masked target>) did not get through: <why> (<causes>).` A silent
  ring cannot recur. The follower never raises and never blocks. It lives in the sink process only: a
  restart inside the window (the ring's timeout + 60 s, plus 100 s with a retry) loses the check and its
  failure line; the call itself is unaffected. The same outcome is written to the ring's row in the
  notification log, so `target-watch status` shows it even when journald does not.
- **Busy retry, once.** On `user_busy` or SIP 486/600/603 (the operator is on another call) the ring is
  dialled again after `RING_BUSY_RETRY_S` = 100 s: long enough for a short call to end, short enough that
  the news is fresh. Quiet hours still apply. The retry is not a new ring for the cooldown: it reads no
  cooldown and writes none (the first ring of the burst holds it), and it never schedules another retry. Its
  own outcome is logged as above; the single Telegram line is sent only if the retry also fails (or cannot
  be placed: quiet hours, a Telnyx refusal), and says so.
- **Channel limit, same rule.** Being on another call can also show up as Telnyx refusing the dial itself:
  on 2026-11-11 `POST /v2/calls` answered 4xx with error code `90042` "OB profile channel limit exceeded"
  and no ring started (the outbound voice profile's concurrent-call limit was 1; keep it at 4 or more).
  A 4xx whose error code is `90042`, or whose title or detail says "channel limit" or "concurrent", is
  treated like a busy phone: `ring_failed` is logged with `reason: channel_limit` (and the Telnyx `code`),
  the channel counts as failed in the routed event, and the ring is dialled once more after
  `RING_BUSY_RETRY_S`, with the same single-retry rule: no third try, quiet hours still apply, the retry's
  own outcome is followed and reported. The refusal does write the cooldown (unlike other refusals),
  because the pending retry stands for the burst's ring and a later text must not ring as well. Every
  other refusal (a bad request, a 401, a 5xx) is a plain failed channel with no retry.
- **The key** is never stored in a file or logged. It is read at first use with
  `op read --no-newline <ref>` (`TARGET_WATCH_TELNYX_KEY_REF`, default
  `op://DeLoSecrets/74k7nz3xkryqns4lpf65a323ue/credential`; binary `TARGET_WATCH_OP`, default
  `~/.local/bin/op`), held in memory for the life of the process, and dropped after a 401 or 403 so
  a rotated key is picked up. The unit inherits `OP_SERVICE_ACCOUNT_TOKEN` from the user manager.
  The HTTP request sets its own User-Agent, because Cloudflare-fronted APIs refuse Python-urllib's.
- **Cooldown.** One ring per (target, ring channel) per `ring_cooldown_minutes` (default 10), so a
  burst of 3-5 texts rings once. The time of the last ring is in the sqlite state, so a restart does
  not ring again. A throttled ring is neither sent nor failed: it logs `ring_throttled` and is left
  out of `notify_channels`. A ring Telnyx refused (HTTP 4xx/5xx, no key, a refused connection) does not
  use up the cooldown. A ring whose outcome is uncertain (a timeout, a response that breaks off, a
  2xx with no `call_control_id`, the 40 s deadline) is reported as failed but counts for the
  cooldown, because the phone may be ringing: the next text of the burst must not ring again.
  Nothing in the ring path raises, so the bus never naks and redelivers a detection into a second call.
- **Stale detections.** A detection older than 5 minutes (a backlog after a sink outage, or a late
  redelivery) does not ring: it logs `ring_stale`, and Telegram still goes out. It does not use up
  the cooldown.
- **Not blocking.** The key lookup and the POST run in a worker thread under a 40 s deadline, so a
  slow Telnyx cannot stall the event loop. The 15 s HTTP timeout is urllib's per-socket-operation
  timeout, not a wall-clock cap; the deadline is.
- **Quiet hours.** `ring_quiet_hours = "HH:MM-HH:MM"` in America/New_York (start inclusive, end
  exclusive; `22:00-07:00` wraps midnight). Inside the window the ring is skipped and logs
  `ring_quiet`; Telegram still goes out. Absent means ring at any hour.
- **What rings.** Every routed action except `skipped_duplicate`, including `failed`: a failed
  ticket still means the person wrote. The ntfy and Telegram lines go first and carry the content the
  operator reads after the phone rings.
- **Dry run** (`target-watch sink --dry-run`) prints the planned call (to, from, caller name, connection
  id, timeouts, and whether it would be skipped as quiet or throttled) and the planned ntfy push, and
  places nothing.
- **No speech.** The ring is the alarm. Speaking a message would need a TeXML app or a webhook
  handler that answers `call.answered` with a `speak` command; neither exists, and `target-watch`
  runs no web server.
- **Logs and `check`** show a ring number as `ring:...0142` (last four digits) and a SIP target as
  `ring:sip:...er@sip.telnyx.com` (host, and the last two characters of a user of 8+ characters). The
  notification log and `target-watch status` use the same masked form; an `ntfy:` topic and a Telegram
  chat id are shown as they are.
- **Check what happened to a ring**: `target-watch status` (every channel and its outcome, from sqlite), or
  `journalctl --user -u target-watch-sink --since -1h | grep -E 'ring_(placed|answered|unanswered|rejected|busy_retry|outcome_unknown)'`.
  `ring_placed` with no outcome line 30 s later means the sink restarted in between; read the call in
  Telnyx (Reporting, or `GET /v2/call_events?filter[leg_id]=<call_leg_id from ring_placed>`).

One-time device setup, or the call rings like any other unknown number: save the `from` number
(+1 215 449 8437) as a contact, for example "Jim Watch", give that contact a loud custom ringtone,
and let it through Do Not Disturb (Android: Settings > Notifications > Do Not Disturb > People >
Calls > starred contacts, then star the contact). The ring carries the target's name as its caller
name (`from_display_name`); a SIP softphone may show that name or the contact, depending on the app.

State lives in `~/.local/state/target-watch/state.db`, or under
`$TARGET_WATCH_STATE_DIR`.

## Add a target

Drop a TOML file where the default globs find it: `~/code/*/.watch/*.toml`
or `~/code/33GOD/*/.watch/*.toml`. The built-in `targets/selftest.toml`
is **not** loaded by default and has no outbound notification channel; include
its exact path with `--targets` or `TARGET_WATCH_PATHS` for a local synthetic
probe. Both options replace the default globs, so include the client glob(s)
when testing alongside real watches. The matcher and sink reload target files
within 30 s, with no restart. A synthetic probe does not change Jim's client
watch, Telegram ticket notification, SIP ring or cooldown.

Put real targets in the client's own repo under `.watch/`, never in this one:
this repo is public.

```toml
# ~/code/some-client/.watch/jane-doe.toml   (FAKE handles: illustration only)
[target]
id = "jane-doe"
display_name = "Jane Doe"

[target.handles]
email = ["jane.doe@example.com", "jane@example.org"]
e164 = ["+12155550100"]
label = ["Jane Doe", "Janie"]            # contact names as they appear on texts
platform = ["slack:U0123ABCD", "telegram:123456789"]

[sink]
kind = "plane"                           # or "notify_only" (no ticket)
repo = "~/code/some-client"              # px binds the board from its .project.json
state = "Todo"
labels = ["submitted-by-client"]
priority = "medium"
coalesce_hours = 24
notify = ["ntfy:<topic>", "telegram:<operator chat id>"]   # add "ring:sip:...", see "ntfy" and "Ring"
```

Then run `target-watch check`. It must say `ok`.

The self-test target at `targets/selftest.toml` (`watch-selftest@delo.sh` /
"Watch Selftest", `notify_only`) exists for live proofs. It notifies the
operator and files no ticket.

## Add a provider

Add an `x-sender` block to the provider's inbound schema
(`schemas/bloodbank/messaging/<platform>.received.json`, see
`docs/event-naming.md` section 10.1). Nothing else is needed:

```json
"x-sender": {
  "handle_paths": ["data.sender.handle"],
  "name_paths": ["data.sender.display_name"],
  "thread_path": "data.thread_id",
  "text_paths": ["data.content.text"],
  "time_path": "data.received_at",
  "inbound_when": {"path": "data.direction", "equals": ["inbound"]},
  "handle_kinds": ["platform_user"]
}
```

The matcher rescans `schemas/` within 30 s of a schema file changing, and every
watch matches the new provider from then on. `subject_path` is an optional
extra the matcher honours when a provider has a subject line. The provider name
on a detection is the type's entity segment (`messaging.signal.received` gives
`signal`).

## Gmail ingress

`gog` is authorised for `jaradd@gmail.com`, which also receives `@delo.sh` mail
through Cloudflare forwarding. Each run searches

```
from:(<every target email>) newer_than:2d in:anywhere -in:sent -in:drafts
```

skips message ids it has already seen, fetches each new message and publishes
it exactly per `messaging.email.received`: the sender's address and name, the
first `To:`, `content.text` as subject plus plain-text body (capped at 8000
characters), `thread_id` = Gmail threadId and `message_id` = Gmail id. A message
whose `From:` address is not a target's is never published. The run prints one
summary line and exits non-zero only on a real failure (gog failing, or a
message that could not be fetched or published). Candybar's `on_failure` then
pages through `ntfy-alert`.

The ingress is deliberately target-scoped ("I don't care about all email"). To
widen it to the whole Primary inbox later, change `build_query` in
`target_watch/gmail.py` to `category:primary newer_than:2d -in:sent -in:drafts`
and drop the non-target skip in `poll`. The matcher already ignores senders who
are not watched. Expect much more bus traffic, and keep `newer_than` short.

Candybar is the only clock. Never use an n8n Gmail Trigger, a Schedule Trigger
or a `.timer`.

## Deploy

This service depends on the schemas track (`feat/watch-domain-schemas`), which
registers the `watch` domain and `target` entity in
`services/agent-hooks/core/validate.py`, adds the two watch schemas and puts
`x-sender` on every inbound schema. Until that is on `main`:

- `target-watch check` reports `0 inbound types`.
- `match` matches nothing.
- Every envelope the sink receives is refused by the contract.

The tests overlay those files from `tests/fixtures/`.

```bash
cd ~/code/33GOD/bloodbank/services/target-watch
uv sync --frozen --no-dev
uv run target-watch check

# sink first, so no detection waits for its consumer
ln -sf "$PWD/systemd/target-watch-sink.service"  ~/.config/systemd/user/
ln -sf "$PWD/systemd/target-watch-match.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now target-watch-sink target-watch-match

# Gmail ingress: Candybar, registered disabled
candybar register --dry-run ~/code/33GOD/bloodbank/services/target-watch
candybar register ~/code/33GOD/bloodbank/services/target-watch
(cd ~/code/33GOD/candybar && uv run candybar show target-watch-gmail-poll -n 5 && uv run candybar resume target-watch-gmail-poll)
```

A new durable starts at new messages only, so no week of history turns into
tickets. To backfill once, run
`target-watch match --since <ISO> --once` by hand before you enable the unit.

## Proofs

1. `uv run target-watch check` prints `ok`, lists the targets, and shows 13
   inbound types once the schemas track is on main.
2. `uv run target-watch replay --since 2026-11-01T00:00:00Z --target <id>`
   lists the past messages the watch would have caught, from Candystore.
3. Selftest, end to end:
   1. Send mail from `watch-selftest@delo.sh` to `jarad@delo.sh`.
   2. Run `uv run target-watch gmail-poll`. It reports `published=1`.
   3. The matcher publishes `watch.target.detected` (`watch_id`
      `watch-selftest`).
   4. The sink sends one Telegram line and publishes `watch.target.routed`
      with action `notified_only`.
   5. Check both in Candystore:
      `curl '127.0.0.1:8683/events?domain=watch&from=<ISO>'`.
4. Sink dry run against the live consumer:
   `uv run target-watch sink --dry-run --since <ISO>` prints each plan (px
   argv, notification text) and does nothing.

## Tests

```bash
uv run --project services/target-watch --extra test pytest services/target-watch/tests -q
# or: mise run target-watch:test
```

The tests use fakes for NATS, px, hermes, gog and Candystore, and fixture
schemas with `x-sender` blocks. They never touch the bus, a board or Telegram.
