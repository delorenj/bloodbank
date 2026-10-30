# Farm command delivery (Create Invoice and the command palette)

The AutomaticAI portal's Worker (the Farm) posts one signed request per Create Invoice click to
`https://farm-commands.delo.sh/v1/invoice-commands` (Cloudflare Tunnel `*.delo.sh` -> Traefik ->
this container). The contract is Milkshake's `docs/farm-commands.md` (sections 2, 4, 5 and 10);
the schema is `schemas/bloodbank/billing/invoice.generate.json`.

In order, before anything is parsed or stored:

1. body at most 4096 bytes (`413 BODY_TOO_LARGE`), `Content-Type: application/json`;
2. `X-Milkshake-Signature: sha256=<hex>` present (`401 SIGNATURE_MISSING`), `X-Milkshake-Key-Id`
   known (`401 KEY_UNKNOWN`), HMAC-SHA256 over the raw bytes in constant time
   (`401 SIGNATURE_INVALID`);
3. UTF-8 JSON (`400 BODY_INVALID`) whose canonical form is exactly the signed bytes
   (`422 BODY_NOT_CANONICAL`);
4. the request rules of section 2.1, including `command_id = uuid5(NS, session_id:client_request_id)`
   and the envelope's JSON Schema (`422 REQUEST_INVALID`);
5. `issued_at` within 300 s of this clock (`422 COMMAND_EXPIRED`);
6. immutable archive insert keyed by `command_id` (same body: `200 duplicate`; different body:
   `409 COMMAND_ID_CONFLICT`);
7. publish `bloodbank.cmd.billing.invoice.generate` to `BLOODBANK_COMMANDS` with
   `Nats-Msg-Id: <command_id>` and wait for the PubAck (`503 BUS_UNAVAILABLE`; the archived row
   stays unpublished and the 15 s sweep or the client's identical retry publishes it);
8. `202 {success, duplicate:false, command_id, received_at}`.

### `POST /v1/commands` (the command palette, Milkshake Story 10.3)

One route for every palette command, keyed by the `subject` inside the signed v2 body
(contract section 14.3 to 14.5; `schema_version: 2`, `subject`, `command`, `args`, `mode`).
The route adds nothing a signed field does not: `ROUTES` in `main.py` maps each registered
subject to the one command it may carry, and a new command is a schema plus one `ROUTES` row.
Steps 1 and 5 to 8 are the invoice route's. The rest, in order:

2. the signature header has the shape `sha256=<64 hex>` (`401 SIGNATURE_MISSING`); the key id
   starts with `farm-command-` and is held (`401 KEY_UNKNOWN`: an invoice key never signs here,
   and a `farm-command-` key never signs `/v1/invoice-commands`); HMAC over the raw bytes
   (`401 SIGNATURE_INVALID`). The invoice route runs the same three checks with `farm-invoice-`;
3. UTF-8 JSON without duplicate keys (`400 BODY_INVALID`); canonical text re-encoded to UTF-8 in
   a `try` on both routes, so a lone surrogate (JSON can spell one) is `422 REQUEST_INVALID`, never
   a dropped connection; canonical equals raw (`422 BODY_NOT_CANONICAL`);
4. `check_palette_request`: exactly the eleven v2 keys, `schema_version` the integer 2, a subject
   in `ROUTES` (`422 SUBJECT_UNKNOWN`), the command that subject carries, the `command_id`
   derivation, `mode`, the `issued_at` spelling, a slug `client`, `requested_by` trimmed,
   lowercase and at most 158 characters, an `args` object, `date_from` / `date_to` real calendar
   dates (`date.fromisoformat`; no `FormatChecker`), and rules 2 to 5 of 14.3.2 (the `squeeze`
   span, `args.client` equals `client`, no control character in `args`, no lone surrogate
   anywhere). Any failure is `422 REQUEST_INVALID`. The argument shapes (rule 1) are the
   subject's JSON Schema in `schemas/bloodbank/billing/` at step 7, also `422 REQUEST_INVALID`;
7. the envelope is typed by the subject (`bloodbank.billing.cost.receive` for
   `bloodbank.cmd.billing.cost.receive`), validated, and published on its own subject; the startup
   sweep republishes each archived envelope on its own `subject`.

Both routes share one archive and one id space: the same `command_id` with different bytes is
`409 COMMAND_ID_CONFLICT` across a v1 and a v2 body. Logs name `command_id`, `subject`, status and
code; never `args` (a reason is free text), a key or a signature.

`fixtures/farm-palette-vectors.json` is the contract's golden file, byte for byte (sha256
`8b986c90...2bf9`, copied from Milkshake `tests/fixtures/`, never regenerated here): five signed
vectors, the 1569-byte envelope of `squeeze-plan`, and 47 `args_cases`; `test_bridge.py` asserts
all three.

### `POST /v1/mercury-webhooks` (Mercury transaction webhooks, Milkshake Story 9.6)

A second trust source on the same container: Mercury POSTs its `transaction.created|updated`
deliveries to `https://mercury-hooks.delo.sh/v1/mercury-webhooks` (Traefik router `mercury-hooks`,
Host and Path). The contract is Milkshake's `docs/farm-commands.md` section 16; the schemas are
`schemas/bloodbank/billing/transaction.*.json` and `schemas/_common/mercury_hook.v1.json`. The rule
behind the order: **only a body that is not authentic is answered 4xx**. Mercury does not retry a
4xx and counts it toward disabling the endpoint, so once the HMAC matches every outcome is a 2xx
or a 503 Mercury retries.

0. `Host` (port stripped, lowercased) is `mercury-hooks.delo.sh`, else 404; the Farm paths answer
   404 on that host. No endpoint secret or no `mercury-hook-` key configured: `503 HOOK_UNCONFIGURED`;
1. the body: `Content-Length` 1 to 65536, or `Transfer-Encoding: chunked` decoded here (cloudflared
   may forward that way) up to 65536 bytes; more is `413 BODY_TOO_LARGE`; no length, a zero,
   negative or non-numeric one, broken chunk framing or a 3 s stall is `503 BODY_UNREADABLE`.
   `Content-Type` is not checked;
2. `verify_mercury`: `Mercury-Signature: t=<unix s>,v1=<hex>` (exactly one `t`, one or more `v1`,
   other keys ignored; else `401 SIGNATURE_MISSING`), HMAC-SHA256 with each endpoint secret
   (`_1`, then `_2`) over `<t>.<raw body>` in constant time (`401 SIGNATURE_INVALID`). The
   five-minute window is evaluated here and answered at step 5;
3. `read_mercury_event`: only `id`, `resourceId` (uuids, lowercased), `resourceType` and
   `operationType` (`create|update|delete`) are required; `occurredAt`, `resourceVersion`,
   `changedPaths`, `mergePatch.createdAt|postedAt` become null when absent or unreadable. A body
   that fails a required rule is archived `ignored` `EVENT_UNREADABLE`, `200`;
4. dedupe by Mercury's event id, inside one archive transaction with a **1.5 s** busy timeout
   (a lock or disk error is `503 ARCHIVE_UNAVAILABLE`): same body, `200 duplicate` whatever `t`
   says (a still-pending delivery is published now); different body, `200 conflict`, kept in
   `mercury_conflicts`, nothing published;
5. `t` outside 300 s: archived `ignored` `SIGNATURE_EXPIRED`, `200`, nothing published (a later
   in-window attempt of the same event still publishes);
6. `resourceType` other than `transaction`: archived `ignored` `RESOURCE_IGNORED`, `200`;
7. the minimal request (ids, types and instants; never an amount, a name, a note or the patch) is
   built, signed with the greatest `mercury-hook-*` key id in `keys_json`, wrapped and validated
   before one insert (`mercury_events` plus a `pending` `mercury_delivery`). A validation failure is
   a bridge bug: archived `ignored` `ENVELOPE_INVALID`, `200`;
8. publish `bloodbank.cmd.billing.transaction.receive` with `Nats-Msg-Id: <command_id>`
   (`command_id = uuid5(NS_MERCURY_HOOK, "event:" + event_id)`), waiting at most 2 s for the
   PubAck; a miss leaves the row `pending` for the 15 s sweep;
9. `202 {success, duplicate:false, command_id, published}`: after the durable archive, not after
   the PubAck, because Mercury only needs to stop retrying.

The raw Mercury body is evidence (it holds amounts and names): kept in the archive volume only,
never published, never logged. `/healthz` and `/readyz` add `mercury_configured`,
`mercury_archived`, `mercury_unpublished`, `mercury_ignored` (counts by code),
`mercury_last_received_at` and `mercury_alarm`; `ready` also needs every hook delivery published
and no alarm, which is an `EVENT_UNREADABLE`, `ENVELOPE_INVALID` or `SIGNATURE_EXPIRED` row in the
last 24 hours (an authentic Mercury event dropped until Milkshake's nightly backstop).
`RESOURCE_IGNORED` never alarms. `fixtures/mercury-hook-vectors.json` is the contract's golden file,
byte for byte (sha256 `d0617a1d...213d`, copied from Milkshake `tests/fixtures/`, never regenerated
here).

Configuration (`.env.op`, references only, after `ms mercury webhook register --apply`):
`MERCURY_WEBHOOK_ENDPOINT_1` (the endpoint id, a plain uuid) with `MERCURY_WEBHOOK_SECRET_1`
(`op://DeLoSecrets/<item-uuid>/credential`), and `_2` only during a rotation; half a pair stops the
start. `keys_json` must also hold a `mercury-hook-<yyyy-mm>` key, which the Farm's Worker never
holds. Re-send one archived hook envelope:

```sh
op run --env-file .env.op -- docker compose --env-file /dev/null exec farm-command-bridge \
  python /app/services/farm-command-bridge/main.py republish --event-id <mercury event uuid>
```

The bridge authorizes nobody. Milkshake's `milkshake-farm-consumer` (durable
`milkshake-farm-invoice`) re-verifies the signature, checks its own ledger allow-list and answers
with `bloodbank.evt.billing.invoice.generated|denied|failed` (Create Invoice) or
`bloodbank.evt.billing.farm_command.completed|denied|failed` (the palette). A separate durable,
`milkshake-mercury-hook`, takes `bloodbank.cmd.billing.transaction.receive` and answers with
`bloodbank.evt.billing.transaction.recorded|denied|failed`.

Run from this directory with the keys resolved only in process memory:

```sh
op run --env-file .env.op -- docker compose --env-file /dev/null up -d --build
```

`GET /healthz` is liveness: 200 whenever the process serves, with readiness in the body
(`live`, `ready`, `broker_connected`, `archived`, `unpublished`, `errors`, `last_error`). The
container healthcheck reads only that, so Traefik keeps routing through a NATS blip (a POST then
answers `503 BUS_UNAVAILABLE` with the command archived, and the sweep publishes it once the broker
is back). `GET /readyz` is the same body with 503 while anything is unpublished or the broker is
down, for monitors. The 15 s sweep skips while the broker is down and moves past a row that cannot
publish (logged `republish_failed`, its error kept on the row), so one bad row never holds up the
rest. Logs carry `command_id`, status and code only: never a body, a reason, a key or a signature.

Re-send one archived command with its original envelope (JetStream dedupes on the id within its
window; the consumer's ledger dedupes forever):

```sh
op run --env-file .env.op -- docker compose --env-file /dev/null exec farm-command-bridge \
  python /app/services/farm-command-bridge/main.py republish --command-id <uuid>
```

Key purpose: `keys_json` holds `farm-invoice-<yyyy-mm>` keys (Create Invoice), `farm-command-<yyyy-mm>` keys (the palette)
and `mercury-hook-<yyyy-mm>` keys (the bridge's own signature on hook requests); each prefix signs its own route only. Key rotation: add the new key to the item's `keys_json` (bridge and consumer accept both),
restart both, switch the Worker's `MILKSHAKE_COMMAND_HMAC_KEY` and `MILKSHAKE_COMMAND_KEY_ID`, then
drop the old key after five minutes. The `farm-command-archive` volume is permanent evidence:
back it up with the platform volumes and never delete it on an upgrade.

Tests: `python -m unittest discover -s services/farm-command-bridge -p 'test_*.py'` (asserts the
contracts' golden vectors: the palette's and the Mercury hook's).
