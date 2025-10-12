# Bloodbank RabbitMQ n8n Workflow Setup

## Quick Import

1. **Import the workflow**:
   ```bash
   # In n8n UI: Workflows → Import from File
   # Select: bloodbank-rabbitmq-workflow.json
   ```

2. **Configure RabbitMQ Credentials**:
   - Go to: Settings → Credentials → Add Credential
   - Type: `RabbitMQ`
   - Name: `Bloodbank RabbitMQ`

### For Local Development (with port-forward):
```
Host: localhost
Port: 5673
User: <your-default-user>
Password: <your-password>
Vhost: /
```

### For In-Cluster (if n8n runs in K8s):
```
Host: bloodbank.messaging.svc
Port: 5672
User: <your-default-user>
Password: <your-password>
Vhost: /
```

Get credentials:
```bash
kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.username}' | base64 -d; echo
kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.password}' | base64 -d; echo
```

## What's in the Workflow

### Publisher Branch (Top)
- **Manual Trigger**: Click to publish test events
- **Two event types**: LLM Prompt and Artifact Created
- **Publishes to exchange**: `bloodbank.events.v1` with appropriate routing keys
- **Messages are persistent**: Survive broker restarts

### Consumer Branch (Bottom)
- **RabbitMQ Trigger**: Auto-consumes all events from the queue
- **Routes by type**: LLM events vs other events
- **Processing nodes**: Parse and enrich the messages
- **Optional outputs**:
  - Log to Google Sheets (disabled by default)
  - Forward to webhooks (disabled by default)

## Usage

### Publishing Events

1. **Open the workflow** in n8n
2. **Click "Test Workflow"** or activate it
3. **Click the "Manual Trigger - Publish" node** to send test events
4. Both events publish simultaneously (LLM and Artifact)

### Consuming Events

The consumer is **always listening** when the workflow is active:

1. **Activate the workflow**
2. Events arriving at the queue auto-trigger processing
3. Check execution logs to see what's being consumed

### Testing End-to-End

1. **Port-forward RabbitMQ** (if testing locally):
   ```bash
   kubectl -n messaging port-forward svc/bloodbank 15672:15672 5673:5672
   ```

2. **Activate the workflow** in n8n

3. **Publish via Manual Trigger** or from Bloodbank app

4. **Watch executions** in n8n to see messages flow through

## Customization Points

### Change Queue Name
Edit the `RabbitMQ Trigger - Consumer` node:
```
Queue: your-custom-queue-name
```

### Filter Specific Routing Keys
Add a binding pattern in the RabbitMQ Trigger options:
```
Routing Key: llm.#  (only LLM events)
Routing Key: artifact.*  (only direct artifact events)
```

### Enable Google Sheets Logging
1. Enable the "Log to Sheets" node
2. Add Google Sheets credentials
3. Set your sheet ID in the node config

### Enable Webhook Forwarding
1. Enable the "Forward to Webhook" node
2. Add your webhook URL in the message data
3. Customize the HTTP request as needed

## Real-World Integration Examples

### Trello Sync
```javascript
// In "Process LLM Event" node
if (message.routing_key === 'llm.prompt') {
  // Extract data
  const card_data = {
    name: message.prompt_summary,
    desc: message.full_prompt,
    idList: 'your-trello-list-id'
  };
  // Connect to Trello node
  return { json: card_data };
}
```

### Artifact Archiver
```javascript
// In "Process Other Event" node
if (message.routing_key.startsWith('artifact.')) {
  // Save to storage
  return {
    json: {
      filepath: `/vault/artifacts/${message.artifact_id}.md`,
      content: message.content,
      metadata: message.metadata
    }
  };
}
```

## Advanced Queue Configuration

For production, you'll want to configure the consumer queue properly:

### Dead Letter Exchange (DLX)
In the RabbitMQ Trigger node options:
```json
{
  "arguments": {
    "argument": [
      {
        "key": "x-dead-letter-exchange",
        "value": "bloodbank.dlx"
      },
      {
        "key": "x-message-ttl",
        "value": "86400000"
      }
    ]
  }
}
```

### Message Priority
```json
{
  "arguments": {
    "argument": [
      {
        "key": "x-max-priority",
        "value": "10"
      }
    ]
  }
}
```

## Troubleshooting

### Consumer not receiving messages
1. Check queue is bound to exchange in RabbitMQ UI
2. Verify routing key pattern matches published keys
3. Check workflow is activated (not just testing)

### Publisher fails
1. Verify credentials are correct
2. Check RabbitMQ is reachable (test with `rabbitmq-plugins list`)
3. Ensure exchange exists and is type `topic`

### Performance tuning
- Set `prefetch_count` in RabbitMQ Trigger options
- Use multiple consumer workflows for parallel processing
- Consider batching for high-volume scenarios

## Next Steps

1. **Test the flow**: Publish → Consume → Verify
2. **Add real consumers**: Connect to Trello, Google Drive, etc.
3. **Set up monitoring**: Enable execution history and error notifications
4. **Scale**: Create dedicated queues per consumer type
5. **Secure**: Use K8s secrets for credentials in production

## Integration with Bloodbank App

Your Bloodbank app publishes to the same exchange:
```python
# In bloodbank/publisher.py
ch.exchange_declare(
    exchange="bloodbank.events.v1",
    exchange_type="topic",
    durable=True
)
ch.basic_publish(
    exchange="bloodbank.events.v1",
    routing_key="llm.prompt",  # or artifact.created, etc.
    body=json.dumps(event_data),
    properties=pika.BasicProperties(delivery_mode=2)  # persistent
)
```

n8n automatically picks it up! 🎉

---

**Pro tip**: Start with the manual trigger to test publishing, then activate the workflow to test consuming. Once both work, you've got a live event bus!
