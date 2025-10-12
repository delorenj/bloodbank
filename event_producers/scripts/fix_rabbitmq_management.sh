#!/bin/bash
# Fix RabbitMQ Management Plugin Configuration

echo "Creating enabled_plugins file with management plugin..."
sudo tee /etc/rabbitmq/enabled_plugins > /dev/null <<'EOF'
[rabbitmq_management].
EOF

echo "Setting proper permissions..."
sudo chown rabbitmq:rabbitmq /etc/rabbitmq/enabled_plugins
sudo chmod 644 /etc/rabbitmq/enabled_plugins

echo "Restarting RabbitMQ service..."
sudo systemctl restart rabbitmq-server

echo "Waiting for RabbitMQ to start..."
sleep 5

echo "Checking management interface..."
for i in {1..10}; do
    if curl -s http://localhost:15672/ > /dev/null 2>&1; then
        echo "✓ RabbitMQ management interface is now available!"
        echo ""
        echo "Access via:"
        echo "  External: https://rabbit.delo.sh"
        echo "  Local: http://localhost:15672"
        echo ""
        echo "Credentials: delorenj / REDACTED_CREDENTIAL"
        exit 0
    fi
    echo "Attempt $i/10: Waiting for management interface..."
    sleep 2
done

echo "✗ Management interface not responding. Check logs with:"
echo "  sudo journalctl -u rabbitmq-server -n 50"
exit 1
