# Farm command delivery (Create Invoice)

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

The bridge authorizes nobody. Milkshake's `milkshake-farm-consumer` (durable
`milkshake-farm-invoice`) re-verifies the signature, checks its own ledger allow-list and answers
with `bloodbank.evt.billing.invoice.generated|denied|failed`.

Run from this directory with the keys resolved only in process memory:

```sh
op run --env-file .env.op -- docker compose --env-file /dev/null up -d --build
```

`GET /healthz` reports `archived`, `unpublished`, `errors`, `last_error`, `broker_connected`
(503 while anything is unpublished or the broker is down). Logs carry `command_id`, status and
code only: never a body, a reason, a key or a signature.

Re-send one archived command with its original envelope (JetStream dedupes on the id within its
window; the consumer's ledger dedupes forever):

```sh
op run --env-file .env.op -- docker compose --env-file /dev/null exec farm-command-bridge \
  python /app/services/farm-command-bridge/main.py republish --command-id <uuid>
```

Key rotation: add the new key to the item's `keys_json` (bridge and consumer accept both),
restart both, switch the Worker's `MILKSHAKE_COMMAND_HMAC_KEY` and `MILKSHAKE_COMMAND_KEY_ID`, then
drop the old key after five minutes. The `farm-command-archive` volume is permanent evidence:
back it up with the platform volumes and never delete it on an upgrade.

Tests: `python -m unittest discover -s services/farm-command-bridge -p 'test_*.py'` (asserts the
contract's golden vector).
