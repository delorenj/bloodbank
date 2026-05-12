FROM python:3.12-alpine

WORKDIR /app

# nats-py + httpx are the only deps. Pin loosely so security fixes flow in.
RUN pip install --no-cache-dir \
        "nats-py>=2.7,<3" \
        "httpx>=0.27,<1"

COPY main.py /app/main.py

RUN adduser -D -u 10001 -h /app appuser \
 && chown -R appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-u", "/app/main.py"]
