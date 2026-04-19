# Bloodbank v3 Compose scaffold

This directory holds the first self-hosted scaffold for Bloodbank v3.

Compose project name: `bloodbank-v3`

## Contents

- `docker-compose.yml` defines the local platform sandbox.
- `components/` holds Dapr component manifests for pub/sub, state, and secret access.
- `nats/` holds the JetStream topology notes and stream definitions.
- `apicurio/` is reserved for Apicurio Registry documentation and future runtime notes.
- `eventcatalog/` is reserved for EventCatalog documentation and future runtime notes.

## Services

- `nats` runs the local JetStream broker.
- `dapr-placement` provides the Dapr placement service used by sidecar-based apps.
- `apicurio-registry` provides the schema registry placeholder for v3.
- `eventcatalog` provides the architecture catalog placeholder for v3.

## Notes

- This scaffold does not depend on the legacy RabbitMQ-based v2 services.
- The files in this directory use placeholder-safe local values only.
- The Dapr component manifests are intentionally minimal until app services are added.
- Future app services should mount `./components` into the Dapr component search path.

## Static validation

Run:

```bash
docker compose -f compose/v3/docker-compose.yml config
```

That check should only parse the file. It should not require any current v2 runtime services.
