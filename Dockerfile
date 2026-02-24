# Bloodbank Dockerfile
# FastAPI service with RabbitMQ publisher/consumer

FROM python:3.12-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Copy holyfields source (path dependency)
COPY holyfields/ /holyfields/

# Copy dependency files
COPY bloodbank/pyproject.toml bloodbank/uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-install-project

# Production stage
FROM python:3.12-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy uv from builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code
COPY bloodbank/event_producers/ ./event_producers/
COPY bloodbank/heartbeat_tick/ ./heartbeat_tick/
COPY bloodbank/heartbeat/ ./heartbeat/
COPY bloodbank/consumer_template/ ./consumer_template/
COPY bloodbank/command_adapter/ ./command_adapter/
COPY bloodbank/command_fsm/ ./command_fsm/

# Activate virtual environment
ENV PATH="/app/.venv/bin:$PATH"

# Set Python path
ENV PYTHONPATH=/app

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8682/healthz || exit 1

# Expose port
EXPOSE 8682

# Run the FastAPI server
CMD ["uvicorn", "event_producers.http:app", "--host", "0.0.0.0", "--port", "8682"]
