FROM python:3.12-alpine

WORKDIR /app

# nats-py + httpx are the only deps. Pin loosely so security fixes flow in.
RUN pip install --no-cache-dir \
        "nats-py>=2.7,<3" \
        "httpx>=0.27,<1"

# Build context is services/ (see compose.yml). main.py resolves the
# envelope builder at <dirname(__file__)>/../agent-hooks, so keep the
# services/ layout inside the image: /app/turn-enricher and /app/agent-hooks.
COPY turn-enricher/main.py /app/turn-enricher/main.py
COPY agent-hooks/core /app/agent-hooks/core

RUN adduser -D -u 10001 -h /app appuser \
 && chown -R appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-u", "/app/turn-enricher/main.py"]
