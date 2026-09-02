# Project activity report contract

Bloodbank owns the periodic activity report a pjangler project publishes about
itself: what moved across its repos, boards and agent sessions inside one
bounded window, composed once per audience. The event is the durable artifact
(the rendered HTML travels inside it), and nothing downstream may invent a
second shape for the same fact.

## Contract family

One type, one schema, one ordering bucket.

| Field | Value |
| --- | --- |
| Type | `bloodbank.project.activity.recorded` |
| Subject | `bloodbank.evt.project.activity.recorded` |
| Schema | `schemas/bloodbank/project/activity.recorded.json`, `schemaref` `bloodbank.project.activity.recorded.v1` |
| Ordering key | `project:<project_slug>`, the slug from the project's `.project.json` |
| Correlation | `data.generator.run_id`: one skill run, shared by both audiences |
| Causation | `null`; every report is a root, lineage is `data.window.previous_event_id` |

The audience is `data.audience` (`internal` or `external`), never a type
token (docs/event-naming.md §11.4). Project, repo, board and ticket identity
live only in `data`.

## Producer rules

1. One run is one `run_id` (uuid4). Put it in `data.generator.run_id` and
   pass it as `--correlation`; the validator refuses a mismatch.
2. Emit `internal` first, then `external`. Both carry the same window, tokens
   and generator; the external payload carries no `sources` and no `tickets`.
3. Preflight with `bb-emit --check`, which holds a piped-in payload to the
   JSON Schema, then publish with the same flags plus `--strict`:

   ```bash
   python3 bin/bb-emit --check \
     --type bloodbank.project.activity.recorded \
     --source urn:33god:skill:activity-report \
     --producer activity-report --service activity-report \
     --actor-type service --actor-id bloodbank.skill.activity-report \
     --correlation "$RUN_ID" --ordering-key "project:$SLUG" < data.json
   # publish: same flags without --check, with --strict
   ```

   `--ordering-key` is derived from `data.project.slug` when omitted; pass it
   anyway so the value is visible in the runner's log.
4. Respect the caps and never raise them: `report.title` ≤ 180,
   `report.raw` ≤ 5000 (the portal `UpdateBody` grammar), `report.markdown`
   ≤ 20000, `report.html` ≤ 262144 and a complete document starting with
   `<!doctype html>`; ≤ 8 repos, ≤ 100 commits per repo (set `truncated`),
   ≤ 64 branches, ≤ 200 tickets with ≤ 8 labels each. NATS runs the 1 MiB
   default `max_payload`. A report that needs more moves `html` to an
   artifact reference in a new schema revision; the cap does not move.
5. Set `generator.dry_run: true` whenever nothing reached the portal, so a
   consumer's previous-report lookup skips the run.
6. `window`: `end > start` and `duration_seconds == end - start`. `basis` is
   `previous_report` (with the prior same-audience event id), `cap_24h`
   (exactly 86400 s, `previous_event_id` null) or `explicit`.
7. `tokens.by_agent.<cli>` buckets sum (`input + output + cache_read +
   cache_write == total`), `tokens.total` sums the buckets, and a CLI that
   reports nothing is `null`.

## Audience rule

`assert_project_invariants` in `services/agent-hooks/core/validate.py`
refuses an `external` event whose `report.title`, `report.raw` or
`report.markdown` matches any of these markers:

```
ticket key        \b<identifier>-\d+\b          (identifier = data.project.identifier)
commit sha        (?<![#\w])(?=[0-9a-f]{7,40}\b)(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b
workstation path  (?<![\w.])/(?:home|Users|root|tmp|var|etc|opt|srv|mnt)/
```

`report.html` is held to the ticket-key marker only: hex ids and colour
tokens are legitimate HTML, so sha and path hygiene for HTML is the
renderer's job and is proven on the text the HTML is rendered from. An
`internal` event must carry `sources` and `tickets`; an `external` one must
not. Every object refuses unknown fields.

## Consumer rules

- Filter on `data.audience` exactly as on `actor.provider`; a client-facing
  reader takes `external` only.
- `report.html` is the artifact of record; `report.raw` is what the portal's
  daily-update rows are written from.
- Previous-report lookup: the latest event with the same `data.project.slug`,
  the same `data.audience` and `data.generator.dry_run == false`. The next
  window starts at its `data.window.end`, not its `time`. Read it bounded,
  `GET /events?type=bloodbank.project.activity.recorded&from=<now-45d>&limit=25`
  filtered client-side, or through the Candystore GIN index:

  ```sql
  SELECT id, time, data->'window'->>'end' AS window_end
  FROM events
  WHERE type = 'bloodbank.project.activity.recorded'
    AND data @> '{"project":{"slug":"<slug>"},"audience":"external","generator":{"dry_run":false}}'
  ORDER BY time DESC LIMIT 1;
  ```

- Both audiences share the ordering bucket. Treat the stream as
  at-least-once and deduplicate on the CloudEvents `id`.

## Fixtures and compatibility

`ops/fixtures/project-contracts.v1.json` holds one internal and one external
payload for the synthetic project `smoketest-project` (identifier `SMK`,
`dry_run: true`), so a live probe that publishes it can never become a real
run's `previous_event_id`. `mise run smoketest:project-contracts` is the
executable spec: both audiences validate, the negative cases are refused, a
maximal payload stays under the NATS default, and `bin/bb-emit --check`
builds the same envelope the test builds. Candystore projects the domain with
no change; nothing in it keys on a domain allowlist.
