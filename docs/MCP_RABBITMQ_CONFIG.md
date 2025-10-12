# MCP RabbitMQ Server Configuration

Complete configuration for accessing your Bloodbank RabbitMQ instance via MCP.

## ✅ In-Cluster Configuration (Recommended)

Use this when your MCP server runs inside the Kubernetes cluster:

```json
"rabbitmq": {
  "command": "uvx",
  "args": [
    "mcp-server-rabbitmq@latest",
    "--rabbitmq-host",
    "bloodbank.messaging.svc",
    "--port",
    "5672",
    "--username",
    "default_user_zzIpiEjlmSTwglmTYfs",
    "--password",
    "KwA0UCPXeCt4ey6H4sskm1YNObGSsyJE",
    "--api-port",
    "15672",
    "--use-tls",
    "false"
  ]
}
```

## 🏠 Local Development Configuration

Use this when running MCP locally with port-forwarding:

```json
"rabbitmq": {
  "command": "uvx",
  "args": [
    "mcp-server-rabbitmq@latest",
    "--rabbitmq-host",
    "localhost",
    "--port",
    "5673",
    "--username",
    "default_user_zzIpiEjlmSTwglmTYfs",
    "--password",
    "KwA0UCPXeCt4ey6H4sskm1YNObGSsyJE",
    "--api-port",
    "15672",
    "--use-tls",
    "false"
  ]
}
```

**Required port-forward command**:
```bash
kubectl -n messaging port-forward svc/bloodbank 15672:15672 5673:5672
```

## 📝 Configuration Details

| Field | Value | Notes |
|-------|-------|-------|
| **Host (In-cluster)** | `bloodbank.messaging.svc` | Kubernetes service DNS |
| **Host (Local)** | `localhost` | Via port-forward |
| **AMQP Port (In-cluster)** | `5672` | Standard AMQP protocol port |
| **AMQP Port (Local)** | `5673` | Port-forwarded |
| **Username** | `default_user_zzIpiEjlmSTwglmTYfs` | From K8s secret `bloodbank-default-user` |
| **Password** | `KwA0UCPXeCt4ey6H4sskm1YNObGSsyJE` | From K8s secret |
| **API Port** | `15672` | Management API port (same for both) |
| **TLS** | `false` | Using plain AMQP (not AMQPS) |

## 🔒 Credentials Source

Credentials are stored in Kubernetes secret:

```bash
# Get username
kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.username}' | base64 -d

# Get password
kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.password}' | base64 -d

# Or use the helper script
./n8n/get-rabbitmq-creds.sh
```

## 🔐 Security Best Practices

### Option 1: Environment Variables (Recommended)

```json
"rabbitmq": {
  "command": "uvx",
  "args": [
    "mcp-server-rabbitmq@latest",
    "--rabbitmq-host",
    "bloodbank.messaging.svc",
    "--port",
    "5672",
    "--username",
    "${RABBITMQ_USERNAME}",
    "--password",
    "${RABBITMQ_PASSWORD}",
    "--api-port",
    "15672",
    "--use-tls",
    "false"
  ]
}
```

**Set environment variables**:
```bash
export RABBITMQ_USERNAME="default_user_zzIpiEjlmSTwglmTYfs"
export RABBITMQ_PASSWORD="KwA0UCPXeCt4ey6H4sskm1YNObGSsyJE"
```

### Option 2: K8s Secret Mount

If your MCP runs in Kubernetes, mount the secret:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: mcp-server
spec:
  containers:
  - name: mcp
    env:
    - name: RABBITMQ_USERNAME
      valueFrom:
        secretKeyRef:
          name: bloodbank-default-user
          key: username
    - name: RABBITMQ_PASSWORD
      valueFrom:
        secretKeyRef:
          name: bloodbank-default-user
          key: password
```

### Option 3: Credential Rotation

Periodically rotate credentials:

```bash
# Delete existing secret
kubectl -n messaging delete secret bloodbank-default-user

# RabbitMQ operator will recreate with new credentials
kubectl -n messaging get secret bloodbank-default-user --watch

# Update your MCP configuration with new credentials
```

## ✅ Verify Connection

### Check RabbitMQ Status

```bash
# Check RabbitMQ pods are running
kubectl -n messaging get pods -l app.kubernetes.io/name=bloodbank

# Check service endpoints
kubectl -n messaging get svc bloodbank

# View service ports
kubectl -n messaging get svc bloodbank -o json | jq -r '.spec.ports[] | "\(.name):\(.port)"'
```

### Test Connection Locally

```bash
# Port-forward RabbitMQ
kubectl -n messaging port-forward svc/bloodbank 15672:15672 5673:5672

# Open Management UI
open http://localhost:15672

# Login with:
# Username: default_user_zzIpiEjlmSTwglmTYfs
# Password: KwA0UCPXeCt4ey6H4sskm1YNObGSsyJE
```

### Test AMQP Connection

```python
# test_connection.py
import pika

credentials = pika.PlainCredentials(
    'default_user_zzIpiEjlmSTwglmTYfs',
    'KwA0UCPXeCt4ey6H4sskm1YNObGSsyJE'
)

# For local (with port-forward)
parameters = pika.ConnectionParameters(
    host='localhost',
    port=5673,
    credentials=credentials
)

# For in-cluster
# parameters = pika.ConnectionParameters(
#     host='bloodbank.messaging.svc',
#     port=5672,
#     credentials=credentials
# )

try:
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    print("✅ Connected successfully!")
    connection.close()
except Exception as e:
    print(f"❌ Connection failed: {e}")
```

## 🎯 Available Exchanges & Queues

Your Bloodbank RabbitMQ instance has these configured exchanges:

### Bloodbank Events
- **Exchange**: `bloodbank.events.v1`
- **Type**: Topic
- **Durable**: Yes
- **Routing patterns**: `llm.*`, `artifact.*`, `webhook.*`

### Fireflies Events (from new implementation)
- **Exchange**: `fireflies.events`
- **Type**: Topic
- **Durable**: Yes
- **Routing patterns**: `fireflies.transcript.*`

### Queues
- `transcripts.rag.ingestion` - Fireflies transcript processing
- `transcripts.failed` - Dead letter queue for failed messages
- Your n8n workflow queues

## 📚 Related Documentation

- **General RabbitMQ Setup**: `n8n/README.md`
- **Fireflies Events**: `n8n/FIREFLIES_EVENTS.md`
- **Quick Reference**: `n8n/bloodbank-quickref.md`
- **Credential Helper**: `n8n/get-rabbitmq-creds.sh`
- **Test Script**: `n8n/test-rabbitmq.py`

## 🐛 Troubleshooting

### MCP Can't Connect

```bash
# Check if RabbitMQ is accessible
kubectl -n messaging port-forward svc/bloodbank 5673:5672 &
telnet localhost 5673

# Should see: Connected to localhost
```

### Authentication Failed

```bash
# Verify credentials are current
kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.username}' | base64 -d
kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.password}' | base64 -d

# Check RabbitMQ logs
kubectl -n messaging logs -l app.kubernetes.io/name=bloodbank --tail=50
```

### API Port Not Accessible

```bash
# Verify management plugin is enabled
kubectl -n messaging exec bloodbank-server-0 -- rabbitmq-plugins list

# Should show: [E*] rabbitmq_management

# Check management port is exposed
kubectl -n messaging get svc bloodbank -o jsonpath='{.spec.ports[?(@.name=="management")].port}'
# Should return: 15672
```

### TLS Issues

If you need TLS (not currently configured):

```json
"--use-tls",
"true"
```

**Note**: You'll need to:
1. Configure RabbitMQ with TLS certificates
2. Update service to expose port 5671 (AMQPS)
3. Mount CA certificate for MCP client

## 🚀 Next Steps

1. **Add MCP configuration** to your MCP server config file
2. **Test connection** using the verification steps above
3. **Start using** RabbitMQ via MCP tools:
   - List exchanges
   - List queues
   - Publish messages
   - Consume messages
   - Monitor queue depth

## 💡 Usage Examples

Once MCP is configured, you can interact with RabbitMQ:

```
# List all exchanges
List RabbitMQ exchanges

# Publish a test event
Publish message to bloodbank.events.v1 exchange with routing key "llm.prompt"

# Monitor queue depth
Check queue depth for transcripts.rag.ingestion

# Get queue bindings
Show bindings for fireflies.events exchange
```

---

**Last Updated**: 2025-10-11
**Credentials Retrieved**: From `bloodbank-default-user` K8s secret in `messaging` namespace
**Service**: `bloodbank.messaging.svc:5672` (AMQP), `:15672` (Management)
