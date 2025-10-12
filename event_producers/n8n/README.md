# 🩸 Bloodbank RabbitMQ + n8n Integration

Complete n8n workflow and tooling for publishing/consuming events from your Bloodbank RabbitMQ setup.

## 📦 What's in the Box

```
bloodbank-rabbitmq-workflow.json  ← Import this into n8n
bloodbank-rabbitmq-setup.md       ← Detailed setup guide
bloodbank-quickref.md             ← Quick reference card
test-rabbitmq.py                  ← Test script (Python)
get-rabbitmq-creds.sh             ← Credential helper (zsh)
```

## ⚡ Quick Start (5 minutes)

### 1. Get Your Credentials

```bash
./get-rabbitmq-creds.sh
```

Or manually:
```bash
kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.username}' | base64 -d; echo
kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.password}' | base64 -d; echo
```

### 2. Port-Forward RabbitMQ (if testing locally)

```bash
kubectl -n messaging port-forward svc/bloodbank 15672:15672 5673:5672
```

### 3. Import Workflow to n8n

1. Open n8n UI
2. Click **Workflows** → **Import from File**
3. Select `bloodbank-rabbitmq-workflow.json`
4. Workflow appears!

### 4. Configure RabbitMQ Credential

1. Go to **Settings** → **Credentials**
2. Click **Add Credential** → Select **RabbitMQ**
3. Name it: `Bloodbank RabbitMQ`
4. Fill in:
   - **Host**: `localhost` (or `bloodbank.messaging.svc` if n8n is in-cluster)
   - **Port**: `5673` (or `5672` if in-cluster)
   - **User**: from step 1
   - **Password**: from step 1
   - **Vhost**: `/`
5. **Save**

### 5. Test It!

```bash
# Run the test script
python test-rabbitmq.py
```

Or manually in the n8n workflow:
1. Open the imported workflow
2. Click **Test Workflow**
3. Click the **Manual Trigger - Publish** node
4. Watch events flow! 🎉

## 🎯 What the Workflow Does

### Publisher Branch (Top Half)
- Manual trigger to publish test events
- Creates two event types:
  - `llm.prompt` - LLM interaction event
  - `artifact.created` - Artifact creation event
- Publishes to `bloodbank.events.v1` exchange
- Messages are **persistent** (survive broker restarts)

### Consumer Branch (Bottom Half)
- **Always listening** when workflow is active
- Auto-consumes messages from RabbitMQ queue
- Routes by event type (LLM vs others)
- Parses and enriches messages
- Optional outputs (disabled by default):
  - Log to Google Sheets
  - Forward to webhooks

## 🔧 Configuration

### Queue Setup
The consumer creates a durable queue with:
- **TTL**: 24 hours
- **Binding**: All events (`#` pattern)
- **Auto-delete**: No (queue survives restarts)

### Routing Keys
The workflow uses these conventions:
```
llm.prompt           → LLM interaction started
llm.response         → LLM response received
artifact.created     → New artifact generated
artifact.updated     → Artifact modified
webhook.*            → Webhook events
```

### Customizing Bindings
Edit the **RabbitMQ Trigger** node to filter events:
- `llm.#` - Only LLM events
- `artifact.*` - Only direct artifact events
- `webhook.fireflies` - Only Fireflies webhooks
- `#` - Everything (default)

## 📊 Monitoring

### Check RabbitMQ UI
```bash
# Port-forward if needed
kubectl -n messaging port-forward svc/bloodbank 15672:15672

# Visit http://localhost:15672
```

In the UI:
1. **Exchanges** → `bloodbank.events.v1` → Check bindings
2. **Queues** → Your queue → See message count
3. **Connections** → Verify n8n is connected

### Check n8n Executions
1. In n8n, go to **Executions**
2. See all workflow runs
3. Click any execution to see full data flow

## 🐛 Troubleshooting

### Can't connect to RabbitMQ
```bash
# Check RabbitMQ is running
kubectl -n messaging get pods

# Check service
kubectl -n messaging get svc bloodbank

# Test connectivity
kubectl -n messaging port-forward svc/bloodbank 5673:5672
telnet localhost 5673
```

### Messages not being consumed
- Is the workflow **activated**? (not just testing)
- Check queue bindings in RabbitMQ UI
- Verify routing key patterns match
- Check n8n execution logs for errors

### Published messages disappear
- Queue might not be bound to exchange
- Check routing key matches binding pattern
- Verify exchange type is `topic` (not `direct`)

## 🚀 Production Tips

### Security
```yaml
# Store credentials in K8s secret
apiVersion: v1
kind: Secret
metadata:
  name: bb-amqp-url
  namespace: default
type: Opaque
stringData:
  url: amqp://user:pass@bloodbank.messaging.svc:5672/
```

### Reliability
- Enable **publisher confirms** in Bloodbank app
- Set up **dead letter exchange** for failed messages
- Configure **message TTL** appropriately
- Use **durable queues** and **persistent messages**

### Scaling
- Create multiple consumer workflows
- Use different queues per consumer type
- Set `prefetch_count` to batch messages
- Monitor queue depth and adjust consumers

## 📚 Documentation

- **Setup Guide**: `bloodbank-rabbitmq-setup.md` (detailed instructions)
- **Quick Reference**: `bloodbank-quickref.md` (command cheat sheet)
- **Test Script**: `test-rabbitmq.py` (smoke test)
- **Creds Helper**: `get-rabbitmq-creds.sh` (quick credential retrieval)

## 🎓 Learning Resources

### Understanding the Flow
```
[Bloodbank App] --publish--> [Exchange: bloodbank.events.v1]
                                    |
                                    | (routing)
                                    |
                            +-------+-------+
                            |               |
                      [Queue: n8n]    [Queue: others]
                            |               |
                      [n8n Consumer]   [Other Consumers]
```

### Key Concepts
- **Exchange**: Router (doesn't store messages)
- **Queue**: Inbox (stores messages for consumers)
- **Binding**: Rule connecting queue to exchange
- **Routing Key**: Message metadata used for routing
- **Topic Exchange**: Wildcard-based routing (`*` = one word, `#` = many words)

## 🤝 Integration Examples

### Trello Card Creation
```javascript
// In n8n "Process LLM Event" node
if (message.routing_key === 'llm.prompt') {
  return {
    json: {
      name: message.summary,
      desc: message.full_content,
      idList: 'YOUR_TRELLO_LIST_ID'
    }
  };
}
// Connect to Trello node
```

### Google Sheets Logging
1. Enable "Log to Sheets" node
2. Add Google Sheets credential
3. Configure sheet ID
4. All events auto-log!

### Webhook Forwarding
1. Enable "Forward to Webhook" node
2. Set webhook URL in config
3. Events forward to external systems

## 💡 Pro Tips

1. **Start simple**: Test with manual trigger first
2. **Monitor execution logs**: Watch data flow through n8n
3. **Use RabbitMQ UI**: Visualize queues and bindings
4. **Test end-to-end**: Publish from app → consume in n8n
5. **Read the docs**: Check `bloodbank-rabbitmq-setup.md` for deep dive

## 🎉 You're Ready!

You now have:
- ✅ Working n8n workflow (import ready)
- ✅ Complete setup documentation
- ✅ Test scripts and helpers
- ✅ Quick reference for daily use
- ✅ Production best practices

Import the workflow, run the test script, and start automating! 🚀

---

**Questions?** Check the setup guide or quick reference. Still stuck? Verify:
1. RabbitMQ is running (`kubectl -n messaging get pods`)
2. Port-forward is active (if testing locally)
3. Credentials are correct
4. Workflow is activated (not just testing)
