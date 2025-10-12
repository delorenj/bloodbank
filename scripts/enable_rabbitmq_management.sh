#!/bin/bash
# Enable RabbitMQ Management Plugin

echo "Enabling RabbitMQ management plugin..."
sudo rabbitmq-plugins enable rabbitmq_management

echo "Restarting RabbitMQ service..."
sudo systemctl restart rabbitmq-server

echo "Waiting for RabbitMQ to start..."
sleep 5

echo "Checking if management interface is available..."
if curl -s http://localhost:15672/ > /dev/null; then
    echo "✓ RabbitMQ management interface is now available on port 15672"
else
    echo "✗ Management interface not responding yet. Check with: sudo systemctl status rabbitmq-server"
fi

echo ""
echo "You should now be able to access:"
echo "  External: https://rabbit.delo.sh"
echo "  Local: http://localhost:15672"
