FROM python:3.12-alpine

WORKDIR /app

# jsonschema is required for the canonical family validator; never skip it.
RUN pip install --no-cache-dir \
        "nats-py>=2.7,<3" \
        "httpx>=0.27,<1" \
        "jsonschema==4.25.1"

# Build from the repository root, with Dockerfile.dockerignore's allowlist.
COPY services/ntfy-router/main.py services/ntfy-router/tolt_family.py /app/
COPY services/agent-hooks/core/ /opt/bloodbank/services/agent-hooks/core/
COPY schemas/ /opt/bloodbank/schemas/
COPY docs/event-naming.md /opt/bloodbank/docs/event-naming.md

RUN adduser -D -u 10001 -h /app appuser \
 && mkdir -p /var/lib/ntfy-router \
 && chown -R appuser /app /var/lib/ntfy-router \
 && chmod 0700 /var/lib/ntfy-router

USER appuser

ENV PYTHONUNBUFFERED=1
ENV NTFY_FAMILY_VALIDATOR_ROOT=/opt/bloodbank
ENV BLOODBANK_SCHEMAS_DIR=/opt/bloodbank/schemas

ENTRYPOINT ["python", "-u", "/app/main.py"]
