#!/bin/zsh
# Bloodbank RabbitMQ Credentials Helper

echo "🩸 Bloodbank RabbitMQ Credentials"
echo "=================================="
echo ""

# Check if kubectl is available
if ! command -v kubectl &> /dev/null; then
    echo "❌ kubectl not found"
    exit 1
fi

# Get credentials
echo "Username:"
kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.username}' | base64 -d
echo ""

echo ""
echo "Password:"
kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.password}' | base64 -d
echo ""

echo ""
echo "=================================="
echo ""
echo "📝 Connection strings:"
echo ""
echo "Local (with port-forward):"
USER=$(kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.username}' | base64 -d)
PASS=$(kubectl -n messaging get secret bloodbank-default-user -o jsonpath='{.data.password}' | base64 -d)
echo "  amqp://${USER}:${PASS}@localhost:5673/"
echo ""
echo "In-cluster:"
echo "  amqp://${USER}:${PASS}@bloodbank.messaging.svc:5672/"
echo ""
echo "=================================="
echo ""
echo "💡 Quick actions:"
echo "  Port-forward: kubectl -n messaging port-forward svc/bloodbank 15672:15672 5673:5672"
echo "  RabbitMQ UI: http://localhost:15672"
echo "  Test script: python test-rabbitmq.py"
