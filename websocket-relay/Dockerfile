FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy relay script
COPY relay.py .

# Make executable
RUN chmod +x relay.py

# Health check
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.connect(('localhost', 8683)); s.close()" || exit 1

EXPOSE 8683

CMD ["python", "relay.py"]
