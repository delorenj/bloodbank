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

The bridge authorizes nobody. Milkshake's `milkshake-farm-consumer` (durable
`milkshake-farm-invoice`) re-verifies the signature, checks its own ledger allow-list and answers
with `bloodbank.evt.billing.invoice.generated|denied|failed` (Create Invoice) or
`bloodbank.evt.billing.farm_command.completed|denied|failed` (the palette).

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

Key purpose: `keys_json` holds `farm-invoice-<yyyy-mm>` keys (Create Invoice) and `farm-command-<yyyy-mm>` keys (the palette);
each prefix signs its own route only. Key rotation: add the new key to the item's `keys_json` (bridge and consumer accept both),
restart both, switch the Worker's `MILKSHAKE_COMMAND_HMAC_KEY` and `MILKSHAKE_COMMAND_KEY_ID`, then
drop the old key after five minutes. The `farm-command-archive` volume is permanent evidence:
back it up with the platform volumes and never delete it on an upgrade.

Tests: `python -m unittest discover -s services/farm-command-bridge -p 'test_*.py'` (asserts the
contract's golden vector).
