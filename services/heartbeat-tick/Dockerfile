FROM python:3.11-alpine

# heartbeat-tick is stdlib-only (urllib + json); no extra deps needed.
# Keeps the image tiny (~50MB total).

WORKDIR /app
COPY main.py /app/main.py

# Run as a non-root user. python:alpine ships with no useradd, so use
# adduser in BusyBox style.
RUN adduser -D -u 10001 -h /app appuser \
 && chown -R appuser /app
USER appuser

# Unbuffered stdout for real-time CI logs.
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-u", "/app/main.py"]
