FROM python:3.12-slim
RUN pip install --no-cache-dir 'nats-py==2.12.0' 'jsonschema==4.25.1'
WORKDIR /app
COPY schemas /app/schemas
COPY services/agent-hooks/core /app/services/agent-hooks/core
COPY services/cost-bridge/main.py /app/services/cost-bridge/main.py
ENV BLOODBANK_SCHEMAS_DIR=/app/schemas PYTHONUNBUFFERED=1
RUN useradd -u 10001 --create-home bridge && mkdir /data && chown bridge /data
USER bridge
ENTRYPOINT ["python", "/app/services/cost-bridge/main.py"]
CMD ["serve"]
