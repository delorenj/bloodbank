# turn-enricher

Async enrichment lane for conversation turns (issue #271). Subscribes to
`bloodbank.evt.conversation.turn.completed` on the bloodbank NATS bus and
republishes each substantive turn as `bloodbank.conversation.turn.annotated`,
so enrichment happens once at the source and every downstream consumer
(roll-ups, hindsight, daily report, Grafana) reads typed facts off the bus
instead of re-extracting.

Modeled on `services/ntfy-router/` (async NATS subscriber, httpx outbound).

## Per turn

1. **Jev gate** — `POST https://openrouter.ai/api/alpha/decisions`, model
   `typesafe/jev-1.13`, one noul question `signal`: does this turn contain
   substantive work output? `answers.signal.noul < 0.5` → noise: counted,
   skipped. Jev failing never skips a turn (the gate is an optimization).
2. **Clairic** — `POST https://api.cloudflare.com/client/v4/accounts/
   {CF_ACCOUNT_ID}/ai/run/@cf/cloudflare/clairic`, fields `action` (text),
   `artifacts` (list of text), `blocker` (text). A field is accepted iff its
   `p_null < 0.5`. Clairic failing publishes with facts absent — the event is
   never dropped.

Turns with empty `data.reply_text` or `data.platform == "subagent"` are
counted and skipped. Extractor pushback: a 429 (or 5xx / transport error)
pauses enrichment for `Retry-After`, else 60s.

## Output event

`bloodbank.conversation.turn.annotated`, envelope built with
`core.envelope.build_envelope`: `causationid` = source event id,
`correlationid` preserved, `actor` copied from the source,
`ordering_key` = source event id + `.annotated`. Data carries
`target_event_id`, `thread_id`, `turn_id`, `outcome`, `signal`,
`action`, `artifacts` (≤ 4), `blocker`, `extractors`, `working_directory`.
Schema: `schemas/bloodbank/conversation/turn.annotated.json`.

## Secrets

From the environment, wired by compose from `.env.op` (op:// refs; never
commit resolved values):

- `JEV_API_KEY` — OpenRouter (Jev)
- `CF_API_KEY` — Cloudflare Workers AI
- `CF_ACCOUNT_ID` — Cloudflare account

## Run

```sh
# from the repo root
op run --env-file=.env.op -- \
  docker compose -f services/turn-enricher/compose.yml up -d --build

docker logs -f bloodbank-turn-enricher
```

Verify archived output (candystore; the archive may lag a few seconds):

```sh
curl 'http://127.0.0.1:8683/events?type=bloodbank.conversation.turn.annotated&limit=3'
```

## Tests

```sh
cd services/turn-enricher
python3 -m pytest test_main.py -v
```
