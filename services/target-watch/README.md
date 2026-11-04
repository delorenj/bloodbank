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
                         hermes send --to telegram:<chat id>
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

Shared options: `--targets <globs>`, `--schemas-dir`, `--state-dir`, `--nats-url`.
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
  of the same target's detections within 60 s on any provider (the SMS copy of
  an RCS text), and records it as `skipped_duplicate`.
- Event ids are deterministic and also sent as `Nats-Msg-Id`. A detection's id
  is `uuid5(ns, "<watch_id>:<dedupe_key>")`, where `dedupe_key` is
  `<source type>:<message_id>` (falling back to the source event id). A routed
  event's id is `uuid5(ns, "<detection id>:<action>")`. A Gmail message's id is
  `uuid5(ns, "gmail:<id>")`.

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

State lives in `~/.local/state/target-watch/state.db`, or under
`$TARGET_WATCH_STATE_DIR`.

## Add a target

Drop a TOML file where the default globs find it: `~/code/*/.watch/*.toml`,
`~/code/33GOD/*/.watch/*.toml`, or this service's own `targets/*.toml`. Set
`TARGET_WATCH_PATHS` (colon-separated) to replace those globs. The matcher and
the sink reload target files within 30 s, with no restart.

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
notify = ["telegram:<operator chat id>"]
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
