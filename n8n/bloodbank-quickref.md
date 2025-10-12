# Bloodbank RabbitMQ + n8n Quick Reference

## 🚀 Getting Started (30 seconds)

```bash
# 1. Port-forward RabbitMQ
kubectl -n messaging port-forward svc/bloodbank 15672:15672 5673:5672

# 2. Get credentials
kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.username}' | base64 -d; echo
kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.password}' | base64 -d; echo

# 3. Import workflow to n8n
# n8n UI → Workflows → Import → bloodbank-rabbitmq-workflow.json

# 4. Configure RabbitMQ credential in n8n
# localhost:5673 (or bloodbank.messaging.svc:5672 if in-cluster)
```

## 📨 Common Publishing Patterns

### From Python (Bloodbank app)
```python
import pika, json

# Connect
conn = pika.BlockingConnection(pika.URLParameters(RABBIT_URL))
ch = conn.channel()

# Declare exchange (idempotent)
ch.exchange_declare(
    exchange="bloodbank.events.v1",
    exchange_type="topic",
    durable=True
)

# Publish
ch.basic_publish(
    exchange="bloodbank.events.v1",
    routing_key="llm.prompt",  # or artifact.created, etc.
    body=json.dumps({"your": "data"}),
    properties=pika.BasicProperties(delivery_mode=2)  # persistent
)
```

### From n8n
Just trigger the "Manual Trigger - Publish" node!

### From CLI (test)
```bash
python - <<'PY'
import pika
url = "amqp://user:pass@localhost:5673/"
ch = pika.BlockingConnection(pika.URLParameters(url)).channel()
ch.exchange_declare(exchange="bloodbank.events.v1", exchange_type="topic", durable=True)
ch.basic_publish(exchange="bloodbank.events.v1", routing_key="test.event", body=b'hello')
print("✓ published")
PY
```

## 🎯 Routing Key Conventions

```
llm.prompt           → LLM interaction started
llm.response         → LLM responded
llm.error            → LLM interaction failed

artifact.created     → New artifact generated
artifact.updated     → Artifact modified
artifact.deleted     → Artifact removed

webhook.fireflies    → Fireflies webhook received
webhook.trello       → Trello webhook received

system.startup       → App started
system.shutdown      → App shutting down
```

### Queue Binding Examples
```
llm.#                → All LLM events
artifact.*           → Direct artifact events (not nested)
webhook.#            → All webhook events
#                    → Everything (debug queue)
*.created            → All created events
```

## 🔍 Debugging in RabbitMQ UI

```bash
# Open UI
kubectl -n messaging port-forward svc/bloodbank 15672:15672
# Visit: http://localhost:15672

# Then:
# 1. Exchanges → bloodbank.events.v1 → Check bindings
# 2. Queues → your-queue → Get Messages
# 3. Connections → Verify n8n is connected
```

## 📊 n8n Workflow Operations

### Test Publishing
1. Open workflow
2. Click "Test Workflow"
3. Click "Manual Trigger - Publish"
4. Check execution log

### Monitor Consuming
1. Activate workflow (toggle switch)
2. Publish some events
3. View "All Executions" → see auto-triggered runs

### Add New Event Type
1. Duplicate "Build Test Event" node
2. Change routing_key
3. Connect to "Merge Events"

### Add New Consumer
1. Duplicate "Process LLM Event" node
2. Update condition in "Route by Type"
3. Add your logic

## 🏗️ Common n8n Patterns

### Log to Multiple Places
```
Consumer → Route → Process → Split
                              ├→ Sheets
                              ├→ Webhook
                              └→ Database
```

### Filter + Transform
```
Consumer → Filter (IF) → Transform (Code) → Action
           ├─ Skip
           └─ Process → Next step
```

### Batch Processing
```
Consumer → Wait (batch 10 msgs) → Process All → Bulk Insert
```

## 🐛 Troubleshooting Checklist

### Can't publish from n8n
- [ ] RabbitMQ credential configured?
- [ ] Port-forward active? (5673:5672)
- [ ] Exchange name correct? (`bloodbank.events.v1`)
- [ ] Test workflow activated?

### Not receiving messages
- [ ] Workflow activated (not just testing)?
- [ ] Queue bound to exchange in UI?
- [ ] Routing key pattern matches?
- [ ] Check queue in RabbitMQ UI for messages

### Connection refused
- [ ] Port-forward running?
- [ ] Correct host/port in credential?
- [ ] RabbitMQ pod healthy?
```bash
kubectl -n messaging get pods
kubectl -n messaging logs statefulset/bloodbank-server
```

## 🎛️ Environment Variables (for Bloodbank app)

### Local (with port-forward)
```bash
export RABBIT_URL="amqp://user:pass@localhost:5673/"
export EXCHANGE_NAME="bloodbank.events.v1"
```

### In-cluster (K8s deployment)
```yaml
env:
  - name: RABBIT_URL
    valueFrom:
      secretKeyRef:
        name: bb-amqp
        key: url
  - name: EXCHANGE_NAME
    value: "bloodbank.events.v1"
```

## 📈 Monitoring Commands

```bash
# Queue stats
kubectl -n messaging exec statefulset/bloodbank-server -- \
  rabbitmqctl list_queues name messages consumers

# Connection count
kubectl -n messaging exec statefulset/bloodbank-server -- \
  rabbitmqctl list_connections name state

# Exchange bindings
kubectl -n messaging exec statefulset/bloodbank-server -- \
  rabbitmqctl list_bindings
```

## 🔒 Production Checklist

- [ ] Use K8s Secret for RabbitMQ credentials
- [ ] Enable publisher confirms in Bloodbank app
- [ ] Set up DLX (dead letter exchange) for failed messages
- [ ] Configure message TTL
- [ ] Add monitoring/alerting
- [ ] Use NetworkPolicies to restrict access
- [ ] Enable RabbitMQ Prometheus metrics
- [ ] Set resource limits on n8n pods
- [ ] Implement retry logic in consumers

---

**Remember**: Exchange → Routes → Queue → Consumer

The exchange is a router, not storage. Messages go into queues, and queues are bound to exchanges with routing patterns!
