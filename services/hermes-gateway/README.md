# Bloodbank Hermes gateway

This directory is the Bloodbank-owned, standalone Hermes platform plugin for
the canonical `bloodbank.cmd.v1.agent.invocation.start` command. It does not
modify or monkey-patch Hermes core.

The adapter binds one JetStream durable pull consumer, validates every command
before dispatch, maps `data.target_agent_id` to an existing Hermes profile,
and stamps that profile on an internal `MessageEvent`. A command is acknowledged
only after the Hermes processing-complete hook and terminal lifecycle event
publishes both finish. Malformed or unroutable poison messages receive a
JetStream terminal acknowledgement; transient registry, broker, publication,
or Hermes failures receive a delayed negative acknowledgement.

## Installation

Install this directory into the Python environment used by the dedicated
Hermes gateway process:

```bash
python -m pip install ./services/hermes-gateway
```

The package advertises the `bloodbank-platform` entry point in the
`hermes_agent.plugins` group. Enable that plugin in the host/default Hermes
home, then configure the platform in its `config.yaml`:

```yaml
plugins:
  enabled:
    - bloodbank-platform

gateway:
  multiplex_profiles: true
  multiplex_secondary_adapters: false
  platforms:
    bloodbank:
      enabled: true
      typing_indicator: false
      extra:
        target_profiles:
          bloodbank-pm: bloodbank-pm
        fleet_registry: ~/.hermes/agents-registry.yaml
        execution_state_file: ~/.hermes/bloodbank-hermes-gateway-state.sqlite3
        allow_direct_profile_targets: false
        durable_name: bloodbank-hermes-gateway-v1
        max_inflight: 4
        ack_wait_seconds: 90
        ack_progress_seconds: 20
```

Resolution precedence is the explicit `target_profiles` mapping, followed by
`agents.<target_agent_id>.profile_name` in the fleet registry. Direct routing
where the external target exactly equals an existing profile is disabled by
default and is available only with `allow_direct_profile_targets: true`.
Unknown targets never fall back to the default profile.
If the registry is missing, unreadable, or invalid, routing is unavailable and
the command is negatively acknowledged for retry. An unknown target is
terminally rejected only after a valid registry was loaded successfully.

`BLOODBANK_NATS_URL` overrides `extra.nats_url`. `BLOODBANK_NATS_CREDS` may
point at a credentials file; credentials belong in runtime secret injection,
not tracked YAML.

## Delivery behavior

- Input is capped by `max_command_bytes` (default 256 KiB).
- `max_inflight` (default 4, maximum 64) bounds fetch size, client pending
  messages, JetStream `max_ack_pending`, and active Hermes turns.
- In-progress acknowledgements extend `ack_wait_seconds` during long turns.
- Lifecycle publications use deterministic event IDs and `Nats-Msg-Id`, so
  broker-level retries deduplicate within the stream's duplicate window.
- A mode-`0600` SQLite execution journal persists the command digest, selected
  profile, and exact lifecycle payloads. `pending` means dispatch may be
  attempted but no Hermes completion outcome has been durably recorded;
  `completed` means the outcome and terminal events were committed before
  publication. Redelivery of a completed record republishes those stored
  events and acknowledges the command without invoking Hermes again.
- The command's correlation, causation, command, idempotency, target, thread,
  and turn identifiers are carried into schema-conformant lifecycle events.

This is not an exactly-once transport claim. JetStream delivery and lifecycle
publication remain at-least-once. The local guarantee is at-most-once Hermes
execution for a command whose `completed` record is durable. A restart while a
record is still `pending` retries execution because the completion boundary is
unknown; a process crash after an external side effect but before Hermes'
processing-complete callback can therefore repeat that pending command. Do not
delete the execution-state database unless intentionally discarding this
deduplication history.

`multiplex_secondary_adapters: false` is required for the dedicated Bloodbank
gateway topology: profile runtime routing remains enabled, while this process
does not also start each profile's Telegram/Slack adapters and collide with
their separate gateway processes. The option defaults to `true` in Hermes for
backward compatibility.

The plugin publishes only existing canonical events:

- `bloodbank.v1.conversation.turn.started`
- `bloodbank.v1.agent.invocation.started`
- `bloodbank.v1.agent.invocation.completed`
- `bloodbank.v1.agent.invocation.failed`
- `bloodbank.v1.conversation.turn.completed`

## Hermes API assumptions

The implementation relies on the public platform-plugin registration seam,
`BasePlatformAdapter.build_source()`, `MessageEvent.internal`,
`SessionSource.profile`, and the processing lifecycle hooks. Hermes currently
defines `BasePlatformAdapter.handle_message()` as background dispatch; the
adapter therefore waits on `on_processing_complete()` before acknowledging the
JetStream command.

Profile runtime selection requires `gateway.multiplex_profiles: true`. The
dedicated shared Bloodbank process must also set
`gateway.multiplex_secondary_adapters: false`; Telegram and Slack continue in
their existing profile-scoped gateway processes. This plugin does not work
around, replace, or mutate Hermes internals.

## Tests

```bash
cd services/hermes-gateway
pytest -q
```
