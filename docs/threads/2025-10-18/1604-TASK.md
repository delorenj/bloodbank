# Bloodbank + RabbitMQ + K8s: QuickStart (for impatient, highly capable humans)

This is the “get your bearings fast” guide so you can ship events **today** and learn the theory on the way. We’ll keep it punchy, layered, and slightly cheeky.

---

## 0) TL;DR mental model (what’s what)

- **RabbitMQ** = the _mailroom_. You publish messages to an **exchange**; consumers bind **queues** to that exchange using routing keys.
- **AMQP** = the _protocol_ your code uses to talk to RabbitMQ.
- **Exchange (topic)** = the _router_. You publish to `exchange=bloodbank.events.v1` with a routing key like `llm.prompt`. Routing rules send copies into queues that asked for them.
- **Queue** = the _inbox_ for a consumer (e.g., “trello-sync”). Consumers read from queues, not exchanges.
- **Kubernetes (K8s)** = the _city_. Pods run containers; Services give stable DNS names; Namespaces separate neighborhoods.
- **In-cluster** = code running inside K8s, reaching services by DNS like `bloodbank.messaging.svc`.

Think:
`bloodbank (your app) → [AMQP] → exchange (router) → queues (inboxes) → consumers (workers)`

---

## 1) What you already have

- RabbitMQ **broker** running via the official Cluster Operator:
  - Namespace: `messaging`
  - Service: `bloodbank.messaging.svc`
  - Ports: `5672` (AMQP), `15672` (management UI)

- Default user + pass (you printed them).
- Working port-forward to your laptop:
  - UI at `http://localhost:15672`
  - AMQP tunneled on `localhost:5673` (your choice)

---

## 2) 10-minute smoke test: publish a message end-to-end

### A) Open the management UI

```bash
kubectl -n messaging port-forward svc/bloodbank 15672:15672
# then visit http://localhost:15672 and log in
```

Peek under **Exchanges**—you’ll see `amq.*` built-ins. Ours will appear after first declare.

### B) Publish a message (local via port-forward)

```bash
python - <<'PY'
import os, pika
U="default_user_…"; P="…password…"
url=f"amqp://{U}:{P}@localhost:5673/%2F"
ch=pika.BlockingConnection(pika.URLParameters(url)).channel()
ch.exchange_declare(exchange="bloodbank.events.v1", exchange_type="topic", durable=True)
ch.basic_publish(exchange="bloodbank.events.v1", routing_key="llm.prompt", body=b'hello-bloodbank')
print("published ok")
PY
```

If it prints `published ok`, you just declared the exchange and published.

### C) See it land (bind a test queue in the UI)

- In UI: **Queues** → **Add a new queue** → name: `debug` (durable).
- In UI: **Exchanges** → `bloodbank.events.v1` → **Bindings**:
  - Destination: `debug`, Routing key: `llm.*` → **Bind**.

- Back in **Queues** → `debug` → **Get messages** → **Get Message(s)**.
  You’ll see your payload. Magic trick complete.

> In real life you’d create queues/bindings with IaC, not clicks. But this shows the pipes are alive.

---

## 3) Run the same test **inside** the cluster (no tunnels)

```bash
kubectl -n messaging run amqp-test --rm -it --image=python:3.11 -- bash -lc '
pip -q install pika && python - <<PY
import os, pika
U="default_user_…"; P="…password…"
url=f"amqp://{U}:{P}@bloodbank.messaging.svc:5672/%2F"
ch=pika.BlockingConnection(pika.URLParameters(url)).channel()
ch.exchange_declare(exchange="bloodbank.events.v1", exchange_type="topic", durable=True)
ch.basic_publish(exchange="bloodbank.events.v1", routing_key="llm.response", body=b"in-cluster-ok")
print("in-cluster published ok")
PY'
```

If you get `in-cluster published ok`, DNS + Service wiring are good.

---

## 4) Configure Bloodbank app (env you actually use)

**In-cluster (Deployment/Pod):**

```
RABBIT_URL=amqp://<user>:<pass>@bloodbank.messaging.svc:5672/
EXCHANGE_NAME=bloodbank.events.v1
```

**Local dev (using your 5673 tunnel):**

```
RABBIT_URL=amqp://<user>:<pass>@localhost:5673/
EXCHANGE_NAME=bloodbank.events.v1
```

Best practice: store the full URL in a Secret and mount as env var.

```bash
kubectl -n messaging create secret generic bb-amqp \
  --from-literal=url="amqp://<user>:<pass>@bloodbank.messaging.svc:5672/"
```

Then in your Deployment:

```yaml
env:
  - name: RABBIT_URL
    valueFrom:
      secretKeyRef:
        name: bb-amqp
        key: url
```

---

## 5) Minimal RabbitMQ theory (90 seconds)

- **Exchange types**:
  - `direct`: exact routing key match
  - **`topic`**: wildcard routing (`*` one token, `#` many) ← **we use this**
  - `fanout`: send to everyone, no keys

- **Durable** exchange/queue + **persistent** messages = survive broker restarts.
- **Publisher confirms** (enable in client) = broker acks your publish → fewer “it vanished” mysteries.
- **Dead-letter queues** (DLQ) + TTL = retry and quarantine bad messages like a responsible adult.

For Bloodbank v0: declare `bloodbank.events.v1` as **topic**, durable, and publish with `delivery_mode=persistent`.

---

## 6) Common commands (bookmark this)

```bash
# What’s running
kubectl -n messaging get pods -o wide
kubectl -n messaging get svc

# Logs (operator and server)
kubectl -n rabbitmq-system logs deploy/cluster-operator
kubectl -n messaging logs statefulset/bloodbank-server

# Port-forward UI + AMQP (alt local port if 5672 busy)
kubectl -n messaging port-forward svc/bloodbank 15672:15672 5673:5672
```

---

## 7) First consumer (quick pattern to prove bindings)

Spin a tiny consumer that listens to `llm.#`:

```bash
python - <<'PY'
import os, pika
U="default_user_…"; P="…password…"
url=f"amqp://{U}:{P}@localhost:5673/%2F"
params=pika.URLParameters(url)
conn=pika.BlockingConnection(params)
ch=conn.channel()
ch.exchange_declare(exchange="bloodbank.events.v1", exchange_type="topic", durable=True)

# durable queue that survives restarts
q = ch.queue_declare(queue="dev-llm", durable=True)
ch.queue_bind(queue="dev-llm", exchange="bloodbank.events.v1", routing_key="llm.#")

def handle(ch_, method, props, body):
    print(f"[{method.routing_key}] {body.decode()}")
    ch_.basic_ack(method.delivery_tag)

ch.basic_qos(prefetch_count=10)
ch.basic_consume(queue="dev-llm", on_message_callback=handle)
print("listening on dev-llm (llm.#)…")
ch.start_consuming()
PY
```

Publish something (`llm.prompt`, `llm.response`), watch it print.

---

## 8) Troubleshooting cheats

- **UI loads but login fails** → wrong creds; re-print:

  ```bash
  kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.username}' | base64 -d; echo
  kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.password}' | base64 -d; echo
  ```

- **Port-forward “address already in use”** → something on 5672; use `5673:5672` locally.
- **Publish hangs/fails in cluster** → verify Service endpoints:

  ```bash
  kubectl -n messaging get endpoints bloodbank
  ```

- **No messages in queue** → check:
  - Did you **bind** the queue to the exchange?
  - Does the **routing key pattern** match (e.g., queue expects `llm.*` and you published `artifact.created`)?

---

## 9) Production-ish knobs (when ready)

- Add **DLX** (dead-letter exchange) + per-queue **TTL** for retries.
- Turn on **publisher confirms** in your Bloodbank publisher.
- Use **NetworkPolicies** so only your namespace talks to RabbitMQ.
- Export broker metrics via **Prometheus** (the Operator exposes 15692).

---

## 10) Where Bloodbank slots in

- **Producers**: `bloodbank.http` (webhooks & REST), `bloodbank.mcp_server` (tools), `bb wrap` (CLI siphon) → all **publish** to `bloodbank.events.v1`.
- **Consumers**: Trello sync, n8n flows, artifact archiver, weekly analytics → each creates its own **queue** and **binds** what it cares about:
  - `trello-sync` binds `llm.prompt`
  - `artifact-writer` binds `artifact.#`
  - `metrics` binds `llm.#` and `artifact.#`

ASCII vibes:

```
[Producers] --> [Exchange: bloodbank.events.v1 (topic)] --> [queue: trello-sync] -> Trello
                                          \--> [queue: artifact-writer] -> Vault
                                          \--> [queue: metrics] -> Timeseries
```

---

## 11) Next 15 minutes to make it “real”

1. Put `RABBIT_URL` in a Secret and mount it in the Bloodbank Deployment.
   - `kubernetes/deploy.yaml` now ships a `bloodbank-amqp` Secret; patch it with your real creds.
   - Both API + MCP containers read `RABBIT_URL` from that Secret via `valueFrom`.
2. On Bloodbank startup, **declare** the exchange and **fail fast** if it can’t connect.
   - `rabbit.Publisher.start()` guards against double-starts and raises immediately with a redacted endpoint if RabbitMQ is unreachable.
3. Create one consumer (n8n or tiny Python) that binds `artifact.#` and logs payloads.
   - Run `python scripts/artifact_consumer.py` (optionally set `ARTIFACT_QUEUE` / `ARTIFACT_ROUTING_KEY`).
   - It declares the queue, binds to `artifact.#`, and pretty-prints JSON bodies.
   - Prefer n8n? Import `n8n/bloodbank-bus-workflow.json` for a prewired AMQP trigger + publisher pair.
4. Point a real webhook (e.g., Fireflies) at `/webhooks/fireflies` and watch artifacts flow.
   - Fireflies POSTs will emit `artifact.created`; keep the consumer running to see them land.
   - Customize the webhook handler if your provider uses different payload fields.

You now have a living bus, a clear mental model, and the muscle memory to drive it. The BB API is just the sugar on top—let’s wire your first real producer and a Trello consumer next so your graph of life starts automating itself like the layered wonderland it is.
