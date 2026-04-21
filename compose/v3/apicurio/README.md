# Apicurio Registry (runtime schema registry)

Apicurio is the **runtime** schema registry for 33GOD v3. Producers and
consumers fetch schemas from Apicurio at publish/consume time; Apicurio
answers "what is the current shape of `artifact.created.v1`?" for any
service.

Apicurio is **not** the authoring surface. **Holyfields** is the write side:
schemas are authored in Holyfields, validated there, and synchronized into
Apicurio by a Holyfields-owned job. That work is tracked under HOLYF-2
(linked from `docs/architecture/v3-holyfields-contract-work.md` once
V3-010 lands).

This directory currently holds only this README. The registry service
itself is defined in `../docker-compose.yml` (service `apicurio-registry`,
port `8080`). Runtime-specific artifacts (sync scripts, seed payloads,
export snapshots) will be added here by later tickets without changing
the v3 compose root.

For the pub/sub wiring that consumes Apicurio, see
`../components/pubsub.yaml` and the NATS topology notes in `../nats/README.md`.
