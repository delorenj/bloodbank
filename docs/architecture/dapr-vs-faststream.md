# Dapr vs FastStream for Bloodbank vNext

This document explains how to choose between Dapr and FastStream for the
Bloodbank overhaul. It also records the recommendation for 33GOD as of
April 9, 2026.

## Short answer

Choose Dapr when you want a self-hosted platform layer that standardizes
pub/sub, service invocation, secrets, workflows, and runtime wiring across
multiple languages. Choose FastStream when you want a Python-first messaging
framework, direct broker semantics, and the smallest possible operational
surface.

For 33GOD, the better fit is Dapr.

## The real decision

The question is not "which event library is better?" The real question is
"do we want Bloodbank to be an application library or a platform?"

FastStream is a strong application library. Dapr is a runtime platform.

If you choose FastStream, each service still owns more transport details,
more broker setup knowledge, and more cross-language inconsistency. If you
choose Dapr, you accept more moving parts in exchange for much less bespoke
infrastructure inside each service.

## Ask yourself these questions

Ask these questions in order. The first hard "yes" often decides the outcome.

### Is the ecosystem polyglot?

If your estate includes Python, TypeScript, Rust, and more over time, Dapr is
the cleaner choice. It gives every service the same sidecar contract and keeps
Bloodbank from becoming a Python-shaped platform.

If nearly everything is Python and likely to stay that way, FastStream stays
very attractive.

### Do we want more than pub/sub?

If you only need typed publish and consume, FastStream is enough. If you also
want service invocation, workflow orchestration, state stores, retries, and
secret handling to follow one platform model, Dapr is the better fit.

### Do we want the broker to leak into application code?

FastStream is pleasant, but it is still a broker-facing application framework.
That is good when you want broker-native control. It is bad when you want teams
to think in contracts and handlers rather than exchanges, subjects, bindings,
and transport quirks.

Dapr keeps more of that concern in the platform layer.

### Are we willing to run sidecars and a control plane?

Dapr only makes sense if the answer is yes. You must be comfortable with
sidecars, component manifests, and a self-hosted control plane in Docker
Compose.

If that sounds like drag instead of leverage, choose FastStream.

### Do we want CloudEvents and HTTP or gRPC portability at the edge?

Dapr natively centers a consistent runtime API and commonly wraps pub/sub
traffic as CloudEvents. That is useful when you want consistent ingress,
egress, tracing, and tooling regardless of service language.

FastStream can absolutely implement CloudEvents, but it will be your job to
enforce that contract in every service and helper.

### Where do we want complexity to live?

This is the most important question.

With FastStream, complexity stays in service code, helper libraries, and local
conventions. With Dapr, complexity moves into platform operations and manifests.

For Bloodbank, platform complexity is the better trade. The current repo shows
what happens when transport conventions, schema lookup, ingress, and command
plumbing are reimplemented inside the app layer.

## Comparison matrix

| Question | Dapr | FastStream |
| --- | --- | --- |
| Best for polyglot services | Strong | Weak |
| Best for Python-only services | Good | Strong |
| Pub/sub only | Good | Strong |
| Pub/sub plus workflows and service invocation | Strong | Weak |
| Lowest operational overhead | Weak | Strong |
| Strongest direct broker control in app code | Weak | Strong |
| Best fit for sidecar platform model | Strong | Weak |
| Best fit for Docker Compose self-hosted stack | Good | Good |
| Best fit for generated contracts as the center of the system | Strong | Good |

## Why Dapr wins for 33GOD

Bloodbank is no longer just a Python event publisher. It is the transport and
coordination backbone for a mixed ecosystem of agents, bridges, daemons, and
automation services. That pushes the design toward a platform.

Dapr wins for these reasons:

- The ecosystem is already polyglot.
- You want to stop rebuilding transport conventions in every producer and
  bridge.
- You want a bigger rethink, not a cleaner version of the same Python stack.
- You are comfortable self-hosting infrastructure in Docker Compose.
- You want Bloodbank to become thinner at the application layer and stronger at
  the platform layer.

## When to reject Dapr anyway

Dapr is the wrong choice if any of these become true:

- You decide Bloodbank should stay a Python-only system.
- You only want typed messaging and do not want sidecars.
- Your team is not willing to own a platform control plane.
- You want every handler to directly use broker-native semantics.

If those conditions become dominant, FastStream is the correct fallback.

## Decision

Bloodbank vNext chooses Dapr as the runtime platform.

FastStream remains useful as a reference consumer framework for legacy services,
small Python-only utilities, or transitional adapters. It is not the primary
runtime for the new platform.

## Sources

- CloudEvents specification:
  <https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md>
- AsyncAPI document structure:
  <https://www.asyncapi.com/docs/concepts/asyncapi-document/structure>
- FastStream AsyncAPI and Pydantic documentation:
  <https://faststream.ag2.ai/latest/getting-started/asyncapi/custom/>
- Dapr pub/sub overview:
  <https://docs.dapr.io/developing-applications/building-blocks/pubsub/pubsub-overview/>
- Dapr CloudEvents behavior:
  <https://docs.dapr.io/developing-applications/building-blocks/pubsub/pubsub-cloudevents/>
