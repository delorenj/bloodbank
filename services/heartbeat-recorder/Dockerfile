FROM python:3.11-alpine

WORKDIR /app
COPY main.py /app/main.py

RUN adduser -D -u 10002 -h /app appuser \
 && chown -R appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1

EXPOSE 3001

ENTRYPOINT ["python", "-u", "/app/main.py"]
